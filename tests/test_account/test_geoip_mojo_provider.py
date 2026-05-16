"""Tests for the mojo GeoIP provider (mojo/helpers/geoip/mojo.py).

Covers fetch() success / failure modes, firewall-field stripping at the
boundary, and threat_intel.perform_threat_check(skip_external=True).
"""
from unittest import mock
from testit import helpers as th


def _upstream_payload(ip="203.0.113.10", overrides=None):
    """Build a representative upstream `detailed` graph payload."""
    base = {
        "id": 42,
        "ip_address": ip,
        "country_code": "US",
        "country_name": "United States",
        "region": "California",
        "region_code": "US-CA",
        "city": "San Francisco",
        "postal_code": "94103",
        "latitude": 37.7749,
        "longitude": -122.4194,
        "timezone": "America/Los_Angeles",
        "asn": "AS15169",
        "asn_org": "Google LLC",
        "isp": "Google LLC",
        "connection_type": "Corporate",
        "mobile_carrier": None,
        "is_tor": False,
        "is_vpn": True,
        "is_proxy": False,
        "is_cloud": True,
        "is_datacenter": True,
        "is_mobile": False,
        "is_known_attacker": True,
        "is_known_abuser": False,
        "threat_level": "high",
        # Per-fleet enforcement state — must be stripped at the boundary.
        "is_blocked": True,
        "blocked_at": "2026-05-15T10:00:00Z",
        "blocked_until": None,
        "blocked_reason": "ssh_brute_force",
        "block_count": 3,
        "is_whitelisted": False,
        "whitelisted_reason": None,
        "data": {"raw": "upstream_blob", "is_blocked": True},  # nested firewall field
    }
    if overrides:
        base.update(overrides)
    return {"status": True, "data": base}


def _mock_response(status_code=200, body=None):
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.raise_for_status = mock.MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


@th.unit_test("mojo_fetch_missing_url_returns_none")
def test_mojo_fetch_missing_url_returns_none(opts):
    from mojo.helpers.geoip import mojo as mojo_provider

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", None):
        result = mojo_provider.fetch("1.2.3.4")
    assert result is None, f"expected None when URL unset, got {result!r}"


@th.unit_test("mojo_fetch_missing_api_key_returns_none")
def test_mojo_fetch_missing_api_key_returns_none(opts):
    from mojo.helpers.geoip import mojo as mojo_provider

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value=None):
        result = mojo_provider.fetch("1.2.3.4")
    assert result is None, f"expected None when API key unset, got {result!r}"


@th.unit_test("mojo_fetch_http_error_returns_none")
def test_mojo_fetch_http_error_returns_none(opts):
    from mojo.helpers.geoip import mojo as mojo_provider
    import requests as _req

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="tok"), \
         mock.patch("mojo.helpers.geoip.mojo.requests.get",
                    side_effect=_req.RequestException("boom")):
        result = mojo_provider.fetch("1.2.3.4")
    assert result is None, f"expected None on HTTP error, got {result!r}"


@th.unit_test("mojo_fetch_non_2xx_returns_none")
def test_mojo_fetch_non_2xx_returns_none(opts):
    from mojo.helpers.geoip import mojo as mojo_provider

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="tok"), \
         mock.patch("mojo.helpers.geoip.mojo.requests.get",
                    return_value=_mock_response(status_code=500)):
        result = mojo_provider.fetch("1.2.3.4")
    assert result is None, f"expected None on 5xx, got {result!r}"


@th.unit_test("mojo_fetch_non_success_body_returns_none")
def test_mojo_fetch_non_success_body_returns_none(opts):
    from mojo.helpers.geoip import mojo as mojo_provider

    body = {"status": False, "error": "rate limited"}
    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="tok"), \
         mock.patch("mojo.helpers.geoip.mojo.requests.get",
                    return_value=_mock_response(status_code=200, body=body)):
        result = mojo_provider.fetch("1.2.3.4")
    assert result is None, (
        f"expected None when upstream returns status=False, got {result!r}"
    )


@th.unit_test("mojo_fetch_success_returns_enriched_dict")
def test_mojo_fetch_success_returns_enriched_dict(opts):
    from mojo.helpers.geoip import mojo as mojo_provider

    body = _upstream_payload(ip="203.0.113.10")
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return _mock_response(status_code=200, body=body)

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="secret-token"), \
         mock.patch("mojo.helpers.geoip.mojo.requests.get", side_effect=fake_get):
        result = mojo_provider.fetch("203.0.113.10")

    assert result is not None, "expected a dict on success, got None"
    assert result["provider"] == "mojo", (
        f"provider must be 'mojo', got {result.get('provider')!r}"
    )
    assert result["country_code"] == "US", f"missing country_code: {result!r}"
    assert result["is_vpn"] is True, "upstream is_vpn=True must be preserved"
    assert result["is_known_attacker"] is True, (
        "upstream is_known_attacker=True must be preserved"
    )
    assert result["threat_level"] == "high", (
        f"threat_level not propagated: {result.get('threat_level')!r}"
    )
    # Endpoint called correctly
    assert captured["url"] == "https://hub.example.com/api/system/geoip/lookup", (
        f"unexpected URL: {captured['url']!r}"
    )
    assert captured["headers"]["Authorization"] == "apikey secret-token", (
        f"wrong Authorization header: {captured['headers']!r}"
    )
    assert captured["params"]["graph"] == "detailed", (
        f"must request 'detailed' graph: {captured['params']!r}"
    )
    assert captured["params"]["ip"] == "203.0.113.10", "ip param mismatch"


@th.unit_test("mojo_fetch_strips_firewall_fields")
def test_mojo_fetch_strips_firewall_fields(opts):
    from mojo.helpers.geoip import mojo as mojo_provider

    body = _upstream_payload(ip="203.0.113.11")
    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.get_api_key", return_value="tok"), \
         mock.patch("mojo.helpers.geoip.mojo.requests.get",
                    return_value=_mock_response(status_code=200, body=body)):
        result = mojo_provider.fetch("203.0.113.11")

    forbidden = ("is_blocked", "is_whitelisted", "blocked_at", "blocked_until",
                 "blocked_reason", "block_count", "whitelisted_reason")
    for field in forbidden:
        assert field not in result, (
            f"firewall field {field!r} must be stripped from provider result: "
            f"got {result.get(field)!r}"
        )
    # Nested data dict must also be scrubbed
    nested = result.get("data") or {}
    assert "is_blocked" not in nested, (
        f"nested data must not carry firewall state: {nested!r}"
    )


@th.unit_test("threat_intel_skip_external_skips_blocklists")
def test_threat_intel_skip_external_skips_blocklists(opts):
    from mojo.helpers.geoip import threat_intel

    with mock.patch.object(threat_intel, "check_internal_threats",
                           return_value={"is_known_attacker": True,
                                         "is_known_abuser": False,
                                         "internal_stats": {"total_events": 7}}) as m_int, \
         mock.patch.object(threat_intel, "check_all_blocklists") as m_ext:
        result = threat_intel.perform_threat_check("203.0.113.50", skip_external=True)

    assert m_int.called, "internal threat check must still run with skip_external=True"
    assert not m_ext.called, (
        "external blocklist check must NOT run when skip_external=True"
    )
    assert result["is_known_attacker"] is True, (
        "internal-derived is_known_attacker must surface in result"
    )
    assert result["threat_data"]["blocklists"] == [], (
        "blocklist hits must be empty when external is skipped"
    )


@th.unit_test("threat_intel_default_runs_both")
def test_threat_intel_default_runs_both(opts):
    """Regression: default behavior unchanged for non-mojo providers."""
    from mojo.helpers.geoip import threat_intel

    with mock.patch.object(threat_intel, "check_internal_threats",
                           return_value={"is_known_attacker": False,
                                         "is_known_abuser": False,
                                         "internal_stats": {}}) as m_int, \
         mock.patch.object(threat_intel, "check_all_blocklists",
                           return_value={"blocklist_hits": [], "is_blocklisted": False}) as m_ext:
        threat_intel.perform_threat_check("203.0.113.51")

    assert m_int.called, "internal check must run by default"
    assert m_ext.called, "external check must run by default"
