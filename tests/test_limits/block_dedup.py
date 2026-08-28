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
    # Populated so the request-derived branch has something to omit — an empty
    # META would let the "no request metadata" assertion pass vacuously.
    req.META = {
        "REMOTE_ADDR": ip,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "QUERY_STRING": "token=ec%3Asecret-single-use-token",
        "HTTP_USER_AGENT": "dm042-block-dedup-agent",
        "HTTP_HOST": "limits.test",
    }
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
    get_connection().delete(f"rlb:{key}:ip:{req.ip}")
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
    get_connection().delete(f"rlb:{key}:ip:{req.ip}")
    Event.objects.filter(category=category).delete()


@th.django_unit_test("block incident: request metadata is carried by default")
def test_block_incident_includes_request_metadata_by_default(opts):
    """The default is unchanged — a throttled ordinary endpoint still files the
    full request-stamped diagnostic operators triage from."""
    from mojo.decorators.limits import _block
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    key = f"dm3257inc_{_uuid.uuid4().hex[:8]}"
    category = f"rate_limit:{key}"
    req = _fake_request(opts.user)
    get_connection().delete(f"rlb:{key}:ip:{req.ip}")
    Event.objects.filter(category=category).delete()

    resp = _block(key, req, 30, "hours")
    assert resp.status_code == 429, f"_block must return 429, got {resp.status_code}"
    assert resp.headers.get("Retry-After") == "30", (
        f"the default path must still carry Retry-After, got {resp.headers.get('Retry-After')!r}"
    )

    events = list(Event.objects.filter(category=category))
    assert len(events) == 1, f"expected exactly 1 incident event, got {len(events)}"
    meta = events[0].metadata or {}
    assert meta.get("http_path") == req.path, (
        f"the default path must record http_path, got {meta.get('http_path')!r}"
    )
    assert meta.get("http_query_string") == req.META["QUERY_STRING"], (
        f"the default path must record http_query_string, got {meta.get('http_query_string')!r}"
    )
    assert meta.get("http_user_agent") == req.META["HTTP_USER_AGENT"], (
        f"the default path must record http_user_agent, got {meta.get('http_user_agent')!r}"
    )
    assert events[0].source_ip == req.ip, (
        f"source_ip must be recorded, got {events[0].source_ip!r}"
    )

    get_connection().delete(f"rlb:{key}:ip:{req.ip}")
    Event.objects.filter(category=category).delete()


@th.django_unit_test("block incident: include_request_in_incident=False drops request metadata")
def test_block_incident_omits_request_when_flag_false(opts):
    """#3257: the confirmation landings carry a single-use token in the query
    string, and reporter.py stamps http_query_string onto every request-backed
    Event. Opting out must keep the 429, the Retry-After, the source IP and the
    once-per-window dedup, and drop everything derived from the request."""
    from mojo.decorators.limits import _block
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    key = f"dm3257omit_{_uuid.uuid4().hex[:8]}"
    category = f"rate_limit:{key}"
    req = _fake_request(opts.user)
    get_connection().delete(f"rlb:{key}:ip:{req.ip}")
    Event.objects.filter(category=category).delete()

    for i in range(3):
        resp = _block(key, req, 30, "hours", False)
        assert resp.status_code == 429, (
            f"_block call {i + 1} must still return 429, got {resp.status_code}"
        )
        assert resp.headers.get("Retry-After") == "30", (
            f"opting out must not change Retry-After, got {resp.headers.get('Retry-After')!r}"
        )

    events = list(Event.objects.filter(category=category))
    assert len(events) == 1, (
        f"the once-per-window dedup must be unchanged, got {len(events)} events"
    )
    event = events[0]
    assert event.source_ip == req.ip, (
        f"the resolved source IP must survive the opt-out, got {event.source_ip!r}"
    )
    meta = event.metadata or {}
    for field in ("http_path", "http_query_string", "http_user_agent",
                  "http_method", "http_host", "request_ip"):
        assert field not in meta, (
            f"{field} must not be recorded when include_request_in_incident=False; "
            f"metadata was {meta!r}"
        )
    blob = f"{event.details}{event.title}{meta!r}"
    assert "ec:secret-single-use-token" not in blob, (
        "the throttled request's token must never reach the Event"
    )
    assert "ec%3Asecret-single-use-token" not in blob, (
        "the throttled request's urlencoded token must never reach the Event"
    )
    assert key in event.details, (
        f"the diagnostic must still name the bucket that engaged, got {event.details!r}"
    )

    get_connection().delete(f"rlb:{key}:ip:{req.ip}")
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
