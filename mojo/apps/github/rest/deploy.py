"""
The fleet-deploy trigger: a GitHub push webhook (maestro item #1458, D2).

Auth is the existing HMAC machinery — ``@md.requires_github_webhook()``
verifies ``X-Hub-Signature-256`` against ``GITHUB_WEBHOOK_SECRET`` and 403s
anything unsigned. Nothing new.

The receiver applies D3's uniform rule via ``deploy.request_deploy``: the
target is always recorded (last writer wins), and a deploy is started only
when nothing is already armed — a push landing mid-deploy is consumed later by
the orchestrator's chain check, never by a second concurrent deploy. The
commit SHA comes from the push payload, never from resolving a branch name
later: two nodes resolving ``origin/main`` at different moments landing on
different commits is one of the defects this item exists to remove.
"""
import redis as redis_lib

import mojo.decorators as md
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse

logger = logit.get_logger("github_webhook", "github.log")


@md.POST("deploy/webhook")
@md.public_endpoint("GitHub push webhook; authenticated by X-Hub-Signature-256 HMAC")
@md.requires_github_webhook()
def on_deploy_webhook(request):
    from mojo.apps.edge.services import deploy

    event = request.META.get("HTTP_X_GITHUB_EVENT", "")
    if event != "push":
        # GitHub sends `ping` on webhook creation, and other events when the
        # hook is configured broadly. Only a push can deploy.
        return dict(status=True, ignored=True,
                    reason=f"event {event or 'unknown'} is not a push")

    ref = request.DATA.get("ref", "") or ""
    branch = deploy.deploy_branch()
    if ref != f"refs/heads/{branch}":
        logger.info(f"deploy webhook: ignoring push to {ref or 'unknown ref'}")
        return dict(status=True, ignored=True,
                    reason=f"not the deploy branch ({branch})")

    sha = (request.DATA.get("after", "") or "").strip().lower()
    if request.DATA.get("deleted") or not deploy.is_valid_sha(sha):
        # Branch deletions push the all-zero SHA; anything else non-hex is a
        # payload this receiver does not deploy from.
        logger.info(f"deploy webhook: ignoring push with unusable sha {sha!r}")
        return dict(status=True, ignored=True, reason="no deployable commit")

    pusher = request.DATA.get("pusher") or {}
    actor = f"github:{pusher.get('name') or 'unknown'}"
    try:
        started = deploy.request_deploy(sha, actor=actor)
    except (redis_lib.RedisError, OSError) as err:
        # No coordination state means no deploy — deploying blind is the
        # concurrent-migrate scenario this item exists to remove.
        logger.error(f"deploy webhook: coordination unavailable: {err}")
        return JsonResponse(
            dict(status=False, error="deploy coordination unavailable"),
            status=503)
    return JsonResponse(dict(status=True, queued=started, sha=sha), status=202)
