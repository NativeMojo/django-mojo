"""Tests for the push_abuse_signals async job handler.

Verifies HTTP POST body shape, retry vs no-retry semantics, and graceful
handling of missing config.
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

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret"), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post", side_effect=fake_post):
        asyncjobs.push_abuse_signals(job)

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

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret"), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post", side_effect=fake_post):
        asyncjobs.push_abuse_signals(job)

    body = captured["json"]
    assert body == {"ip": "203.0.113.51", "threat_level": "high"}, (
        f"job must forward only allowlisted fields, got {body!r}"
    )


@th.unit_test("push_job_4xx_returns_without_retry")
def test_push_job_4xx_returns_without_retry(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.52", "threat_level": "high"})

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret"), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    return_value=_mock_response(status_code=403, text="forbidden")):
        # Must NOT raise — 4xx is permanent, retrying won't help.
        asyncjobs.push_abuse_signals(job)


@th.unit_test("push_job_5xx_raises_for_retry")
def test_push_job_5xx_raises_for_retry(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.53", "threat_level": "high"})

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret"), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    return_value=_mock_response(status_code=502, text="bad gateway")):
        raised = False
        try:
            asyncjobs.push_abuse_signals(job)
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

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret"), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post",
                    side_effect=_req.ConnectionError("connection refused")):
        raised = False
        try:
            asyncjobs.push_abuse_signals(job)
        except RuntimeError:
            raised = True
        assert raised, "network errors must raise so the engine retries"


@th.unit_test("push_job_missing_config_returns")
def test_push_job_missing_config_returns(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.55", "threat_level": "high"})

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", None), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value=None), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        # Must not raise, must not attempt HTTP.
        asyncjobs.push_abuse_signals(job)
        assert not m_post.called, (
            "missing config must short-circuit before any HTTP call"
        )


@th.unit_test("push_job_missing_ip_returns")
def test_push_job_missing_ip_returns(opts):
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"threat_level": "high"})  # no ip

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret"), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        asyncjobs.push_abuse_signals(job)
        assert not m_post.called, "missing ip must short-circuit"


@th.unit_test("push_job_missing_signals_returns")
def test_push_job_missing_signals_returns(opts):
    """Payload with only `ip` and no signal fields short-circuits."""
    from mojo.apps.account import asyncjobs

    job = _FakeJob({"ip": "203.0.113.56"})  # no signals

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret"), \
         mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        asyncjobs.push_abuse_signals(job)
        assert not m_post.called, "payload with no signal fields must short-circuit"
