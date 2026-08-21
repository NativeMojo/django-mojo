"""Split out of tests/test_edge/16_deploy_webhook.py (maestro #1839).

The Redis-down 503 is asserted in-process against the imported view, and the
in-process settings fallback assigns django.conf.settings.GITHUB_WEBHOOK_SECRET
— process-global, so unsafe under the parallel default tier.
"""
import hashlib
import hmac
import json as jsonlib
from unittest import mock

from testit import helpers as th


SECRET = "testit-deploy-hook-3f9c"


SHA_1 = "d" * 40


def _push(sha, ref="refs/heads/main", deleted=False):
    return dict(ref=ref, after=sha, deleted=deleted, pusher=dict(name="tester"))


@th.django_unit_test("webhook: Redis down means 503 and NO deploy state")
def test_webhook_redis_down(opts):
    import redis as redis_lib
    from django.conf import settings as dj_settings
    from objict import objict

    from mojo.apps.edge.services import deploy
    from mojo.apps.github.rest.deploy import on_deploy_webhook

    payload = _push(SHA_1)
    body = jsonlib.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(
        SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()

    class FakeRequest:
        pass

    request = FakeRequest()
    request.body = body
    request.DATA = objict.fromdict(payload)
    request.ip = "127.0.0.1"
    request.META = {
        "HTTP_X_GITHUB_EVENT": "push",
        "HTTP_X_HUB_SIGNATURE_256": signature,
    }

    # The in-process settings fallback (no DB row for the secret) lets the
    # real HMAC decorator run; only Redis is stubbed to be down.
    sentinel = object()
    saved = getattr(dj_settings, "GITHUB_WEBHOOK_SECRET", sentinel)
    dj_settings.GITHUB_WEBHOOK_SECRET = SECRET
    try:
        with mock.patch.object(
                deploy, "get_client",
                side_effect=redis_lib.ConnectionError("redis down")):
            response = on_deploy_webhook(request)
    finally:
        if saved is sentinel:
            delattr(dj_settings, "GITHUB_WEBHOOK_SECRET")
        else:
            dj_settings.GITHUB_WEBHOOK_SECRET = saved

    th.assert_eq(response.status_code, 503,
                 f"no coordination state means no deploy — expected 503, "
                 f"got {response.status_code}")

