"""
Manual fleet deploy of a named commit (maestro item #1458).

``requires_global_perms``, never ``requires_perms`` — the whole security story
of this file, same as ``node.py``'s: ``requires_perms`` falls back to the
caller's member-level permission for a client-supplied ``?group=``, and the
GroupMember side has no protection map, so any tenant admin could self-assign
``manage_deploy`` inside their own group and move the WHOLE FLEET to a new
release. The global gate resolves on ``User.permissions``/superuser only.
``allow_api_keys`` stays default-False: moving the fleet is an operator
action, not a machine-federation surface.

``manage_deploy`` is deliberately new rather than reusing ``manage_dns`` —
deploying code to every node is not the same authority as editing a vhost.
"""
import redis as redis_lib

import mojo.decorators as md
from mojo import errors as me
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse

logger = logit.get_logger("edge", "edge.log")


@md.POST("deploy")
@md.requires_global_perms("manage_deploy")
def on_deploy(request):
    from mojo.apps.edge.services import deploy
    from mojo.apps.account.models import User

    sha = (request.DATA.get("sha") or request.DATA.get("commit") or "")
    sha = str(sha).strip().lower()
    if not deploy.is_valid_sha(sha):
        raise me.ValueException("a commit sha (7-40 hex chars) is required")

    actor = f"manual:{getattr(request.user, 'username', None) or 'operator'}"
    try:
        started = deploy.request_deploy(
            sha, actor=actor, source="external_api",
            created_by=request.user if isinstance(request.user, User) else None,
            idempotency_key=request.META.get("HTTP_IDEMPOTENCY_KEY"))
    except (redis_lib.RedisError, OSError, deploy.DeploymentCoordinationError) as err:
        logger.error(
            "manual deploy: coordination unavailable "
            f"(error={type(err).__name__[:80]})")
        return JsonResponse(
            dict(status=False, error="deploy coordination unavailable"),
            status=503)
    return JsonResponse(dict(status=True, queued=started, sha=sha), status=202)
