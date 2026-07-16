"""DM-042: 429 paths must not amplify — metric + incident event fire once per
engagement window, never per rejected request.

The pre-DM-042 _block() did a synchronous Event INSERT + rule evaluation on
EVERY 429, making a rejected request cost more than a served one — the exact
self-amplifying failure loop from the doom-loop postmortem.
"""
import uuid as _uuid

from testit import helpers as th


def _fake_request(user, ip="127.0.0.1"):
    class _FakeRequest:
        pass
    req = _FakeRequest()
    req.user = user
    req.api_key = None
    req.bearer = "bearer"
    req.group = None
    req.ip = ip
    req.path = "/api/dm042/test"
    req.method = "GET"
    req.headers = {}
    req.META = {}
    return req


@th.django_unit_setup()
def setup_dedup_user(opts):
    from mojo.apps.account.models import User
    email = f"dm042_dedup_{_uuid.uuid4().hex[:8]}@limits.test"
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password="Dm042##dedup")
    user.is_active = True
    user.save()
    opts.user = user


@th.django_unit_test()
def test_legacy_block_reports_once_per_window(opts):
    from mojo.decorators.limits import _block
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    key = f"dm042d_{_uuid.uuid4().hex[:8]}"
    category = f"rate_limit:{key}"
    req = _fake_request(opts.user)
    get_connection().delete(f"rlb:{key}:{req.ip}")
    Event.objects.filter(category=category).delete()

    for i in range(3):
        resp = _block(key, req, 30, "hours")
        assert resp.status_code == 429, f"_block call {i + 1} must return 429, got {resp.status_code}"
        assert resp.headers.get("Retry-After") == "30", (
            f"_block 429 must carry Retry-After, got {resp.headers.get('Retry-After')!r}"
        )

    count = Event.objects.filter(category=category).count()
    assert count == 1, (
        f"3 consecutive 429s in one window must produce exactly 1 incident event, got {count}"
    )
    get_connection().delete(f"rlb:{key}:{req.ip}")
    Event.objects.filter(category=category).delete()


@th.django_unit_test()
def test_throttle_block_reports_once_per_window(opts):
    import time
    from mojo.decorators.limits import _throttle_block
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    req = _fake_request(opts.user)
    marker = f"user:{opts.user.pk}"
    window = 60
    window_start = int(time.time()) // window * window
    get_connection().delete(f"rl:api:blocked:user:{opts.user.pk}:{window_start}")
    Event.objects.filter(category="rate_limit:api", details__contains=marker).delete()

    for i in range(3):
        resp = _throttle_block(req, "user", opts.user.pk, 5, window_start, window)
        assert resp.status_code == 429, (
            f"_throttle_block call {i + 1} must return 429, got {resp.status_code}"
        )

    count = Event.objects.filter(category="rate_limit:api", details__contains=marker).count()
    assert count == 1, (
        f"3 throttle rejections in one window must produce exactly 1 incident event, got {count}"
    )
    get_connection().delete(f"rl:api:blocked:user:{opts.user.pk}:{window_start}")
    Event.objects.filter(category="rate_limit:api", details__contains=marker).delete()
