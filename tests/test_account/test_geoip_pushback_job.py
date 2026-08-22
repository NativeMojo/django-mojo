"""Tests for the push_abuse_signals async job handler.

Verifies HTTP POST body shape, retry vs no-retry semantics, and graceful
handling of missing config.

Parallel-safe (item #2558): upstream URL and api key are injected through the
keyword-only base_url= / api_key= seams on push_abuse_signals instead of
mock.patch on the shared mojo.helpers.geoip.config module.
"""
from unittest import mock
from testit import helpers as th


class _FakeJob:
    """Minimal stand-in for jobs.models.Job — only `payload` is used."""
    def __init__(self, payload):
        self.payload = payload


def _mock_response(status_code=200, text=""):
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Incident coverage (item 1100): the four drop paths in push_abuse_signals now
# file Redis-suppressed incident events instead of file-log warnings. These run
# with Django configured (setup forces django.setup()) and observe Event rows
# directly, since report_event is synchronous.
# ---------------------------------------------------------------------------

_PUSH_CATEGORIES = [
    "geoip:abuse_push_unconfigured",
    "geoip:abuse_push_rejected",
    "geoip:abuse_push_missing_ip",
    "geoip:abuse_push_no_signals",
]


@th.django_unit_setup()
def setup_pushback_incidents(opts):
    """Force django.setup() and sweep the four abuse-push incident categories
    plus their fixed Redis suppression keys, so the incident assertions run
    clean against the long-lived DB and Redis. Per-status 'rejected' keys are
    cleared by the test that uses them."""
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key
    from mojo.helpers.redis import get_connection

    Event.objects.filter(category__in=_PUSH_CATEGORIES).delete()
    try:
        redis = get_connection()
    except Exception:
        return
    fixed = {
        "geoip:abuse_push_unconfigured": ["geoip:abuse_push_alerted:unconfigured"],
        "geoip:abuse_push_missing_ip": ["geoip:abuse_push_alerted:missing_ip"],
        "geoip:abuse_push_no_signals": ["geoip:abuse_push_alerted:no_signals"],
    }
    for category, keys in fixed.items():
        for key in keys:
            try:
                redis.delete(notice_key(category, key))
            except Exception:
                pass


def _clear_push_incident(category, keys):
    """Delete a push category's Event rows and its notice keys (best-effort)."""
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key
    from mojo.helpers.redis import get_connection

    Event.objects.filter(category=category).delete()
    try:
        redis = get_connection()
    except Exception:
        return
    for key in keys:
        try:
            redis.delete(notice_key(category, key))
        except Exception:
            pass


@th.django_unit_test()
def test_push_unconfigured_files_one_suppressed_incident(opts):
    """Missing upstream config files ONE level-5 incident per window, naming the
    settings but never their (secret) values."""
    from mojo.apps.account import asyncjobs
    from mojo.apps.incident.models import Event

    category = "geoip:abuse_push_unconfigured"
    _clear_push_incident(category, ["geoip:abuse_push_alerted:unconfigured"])

    job = _FakeJob({"ip": "203.0.113.60", "threat_level": "high"})
    with mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        asyncjobs.push_abuse_signals(job, base_url=None, api_key="topsecretkey")
        asyncjobs.push_abuse_signals(job, base_url=None, api_key="topsecretkey")
        assert not m_post.called, "unconfigured must short-circuit before any HTTP call"

    events = list(Event.objects.filter(category=category))
    assert len(events) == 1, (
        f"missing config must file exactly one suppressed incident per window, "
        f"got {len(events)}: {[e.title for e in events]}"
    )
    assert events[0].level == 5, f"config-rot is level 5, got {events[0].level}"
    details = events[0].details or ""
    assert "GEOIP_MOJO_PROVIDER_URL" in details and "GEOIP_API_KEY_MOJO" in details, (
        f"the incident must name both settings: {details!r}"
    )
    assert "topsecretkey" not in details, (
        f"the incident must NOT echo the api key value: {details!r}"
    )
    _clear_push_incident(category, ["geoip:abuse_push_alerted:unconfigured"])


@th.django_unit_test()
def test_push_rejected_files_per_status_with_literal_body(opts):
    """A 4xx rejection files one level-4 incident PER status code, with the ip,
    status and response body interpolated (the printf regression guard) and the
    api key never leaked."""
    from mojo.apps.account import asyncjobs
    from mojo.apps.incident.models import Event

    category = "geoip:abuse_push_rejected"
    _clear_push_incident(category, [
        "geoip:abuse_push_alerted:rejected:403",
        "geoip:abuse_push_alerted:rejected:400",
    ])

    with mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    return_value=_mock_response(403, "forbidden-body-403")):
        asyncjobs.push_abuse_signals(
            _FakeJob({"ip": "203.0.113.61", "threat_level": "high"}),
            base_url="https://hub.example.com", api_key="topsecretkey")
    with mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    return_value=_mock_response(400, "bad-request-body-400")):
        asyncjobs.push_abuse_signals(
            _FakeJob({"ip": "203.0.113.62", "threat_level": "high"}),
            base_url="https://hub.example.com", api_key="topsecretkey")

    events = list(Event.objects.filter(category=category))
    assert len(events) == 2, (
        f"a distinct status code must file its own incident (per-status key), "
        f"got {len(events)}: {[e.title for e in events]}"
    )
    for event in events:
        assert event.level == 4, f"an upstream rejection is level 4, got {event.level}"

    by_403 = [e for e in events if "203.0.113.61" in (e.details or "")]
    assert len(by_403) == 1, f"the 403 incident must name its ip: {[e.details for e in events]}"
    d403 = by_403[0].details or ""
    assert "403" in d403, f"the 403 incident must carry the status: {d403!r}"
    assert "forbidden-body-403" in d403, f"the 403 incident must carry the response body: {d403!r}"
    for token in ("%s", "%d", "%r"):
        assert token not in d403, (
            f"printf regression: {token!r} must not appear literally — the body "
            f"must be interpolated, got {d403!r}"
        )
    assert "topsecretkey" not in d403, f"the incident must NOT leak the api key: {d403!r}"
    _clear_push_incident(category, [
        "geoip:abuse_push_alerted:rejected:403",
        "geoip:abuse_push_alerted:rejected:400",
    ])


@th.django_unit_test()
def test_push_missing_ip_files_one_incident_key_names_only(opts):
    """A payload with no ip files ONE level-4 incident naming the payload KEYS,
    never their (abuse-state) values."""
    from mojo.apps.account import asyncjobs
    from mojo.apps.incident.models import Event

    category = "geoip:abuse_push_missing_ip"
    _clear_push_incident(category, ["geoip:abuse_push_alerted:missing_ip"])

    job = _FakeJob({"threat_level": "high"})  # no ip; the value must not leak
    with mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="topsecretkey")
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="topsecretkey")
        assert not m_post.called, "missing ip must short-circuit before any HTTP call"

    events = list(Event.objects.filter(category=category))
    assert len(events) == 1, (
        f"missing ip must file exactly one incident per window, got {len(events)}"
    )
    assert events[0].level == 4, f"a payload defect is level 4, got {events[0].level}"
    details = events[0].details or ""
    assert "threat_level" in details, f"the incident must name the payload keys: {details!r}"
    assert "high" not in details, (
        f"the incident must NOT echo the abuse-state VALUE 'high': {details!r}"
    )
    _clear_push_incident(category, ["geoip:abuse_push_alerted:missing_ip"])


@th.django_unit_test()
def test_push_no_signals_files_one_incident_key_names_only(opts):
    """A payload with an ip but no signal fields files ONE level-4 incident
    naming the payload KEYS, never their values."""
    from mojo.apps.account import asyncjobs
    from mojo.apps.incident.models import Event

    category = "geoip:abuse_push_no_signals"
    _clear_push_incident(category, ["geoip:abuse_push_alerted:no_signals"])

    job = _FakeJob({"ip": "203.0.113.63", "random_extra": "SENSITIVE_VALUE"})
    with mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="topsecretkey")
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="topsecretkey")
        assert not m_post.called, "no signal fields must short-circuit before any HTTP call"

    events = list(Event.objects.filter(category=category))
    assert len(events) == 1, (
        f"no-signals must file exactly one incident per window, got {len(events)}"
    )
    assert events[0].level == 4, f"a payload defect is level 4, got {events[0].level}"
    details = events[0].details or ""
    assert "random_extra" in details, f"the incident must name the payload keys: {details!r}"
    assert "SENSITIVE_VALUE" not in details, (
        f"the incident must NOT echo the payload VALUE: {details!r}"
    )
    _clear_push_incident(category, ["geoip:abuse_push_alerted:no_signals"])


@th.unit_test("push_job_posts_correct_body_and_headers")
def test_push_job_posts_correct_body_and_headers(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.50", "threat_level": "high"})
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _mock_response(status_code=200)

    with mock.patch("mojo.apps.account.asyncjobs.requests.post", side_effect=fake_post):
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="secret")

    assert captured["url"] == "https://hub.example.com/api/system/geoip/sync", (
        f"wrong URL: {captured['url']!r}"
    )
    assert captured["headers"]["Authorization"] == "apikey secret", (
        f"wrong auth header: {captured['headers']!r}"
    )
    assert captured["json"] == {"ip": "203.0.113.50", "threat_level": "high"}, (
        f"wrong body: {captured['json']!r}"
    )
    assert captured["timeout"] == 10, f"timeout must be 10s: {captured['timeout']!r}"


@th.unit_test("push_job_strips_extra_payload_fields")
def test_push_job_strips_extra_payload_fields(opts):
    """Defense in depth: any non-federated field in the payload is discarded."""
    from mojo.apps.account import asyncjobs

    job = _FakeJob({
        "ip": "203.0.113.51",
        "threat_level": "high",
        "is_blocked": True,  # forbidden — must be stripped
        "blocked_reason": "x",  # forbidden
        "random_extra": "y",  # not in the allowlist
    })
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _mock_response(status_code=200)

    with mock.patch("mojo.apps.account.asyncjobs.requests.post", side_effect=fake_post):
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="secret")

    body = captured["json"]
    assert body == {"ip": "203.0.113.51", "threat_level": "high"}, (
        f"job must forward only allowlisted fields, got {body!r}"
    )


@th.unit_test("push_job_4xx_returns_without_retry")
def test_push_job_4xx_returns_without_retry(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.52", "threat_level": "high"})

    with mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    return_value=_mock_response(status_code=403, text="forbidden")):
        # Must NOT raise — 4xx is permanent, retrying won't help.
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="secret")


@th.unit_test("push_job_5xx_raises_for_retry")
def test_push_job_5xx_raises_for_retry(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.53", "threat_level": "high"})

    with mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    return_value=_mock_response(status_code=502, text="bad gateway")):
        raised = False
        try:
            asyncjobs.push_abuse_signals(
                job, base_url="https://hub.example.com", api_key="secret")
        except RuntimeError:
            raised = True
        assert raised, (
            "5xx must raise so the engine can retry with backoff"
        )


@th.unit_test("push_job_network_error_raises_for_retry")
def test_push_job_network_error_raises_for_retry(opts):
    from mojo.apps.account import asyncjobs
    import requests as _req

    job = _FakeJob({"ip": "203.0.113.54", "threat_level": "high"})

    with mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    side_effect=_req.ConnectionError("connection refused")):
        raised = False
        try:
            asyncjobs.push_abuse_signals(
                job, base_url="https://hub.example.com", api_key="secret")
        except RuntimeError:
            raised = True
        assert raised, "network errors must raise so the engine retries"


@th.unit_test("push_job_missing_config_returns")
def test_push_job_missing_config_returns(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.55", "threat_level": "high"})

    with mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        # Must not raise, must not attempt HTTP.
        asyncjobs.push_abuse_signals(job, base_url=None, api_key=None)
        assert not m_post.called, (
            "missing config must short-circuit before any HTTP call"
        )


@th.unit_test("push_job_missing_ip_returns")
def test_push_job_missing_ip_returns(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"threat_level": "high"})  # no ip

    with mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="secret")
        assert not m_post.called, "missing ip must short-circuit"


@th.unit_test("push_job_missing_signals_returns")
def test_push_job_missing_signals_returns(opts):
    """Payload with only `ip` and no signal fields short-circuits."""
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.56"})  # no signals

    with mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        asyncjobs.push_abuse_signals(
            job, base_url="https://hub.example.com", api_key="secret")
        assert not m_post.called, "payload with no signal fields must short-circuit"
