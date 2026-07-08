"""Shared test-mode header helpers for geofence tests.

All tests run in parallel — per-request headers replace th.server_settings()
so there are no server reloads and no cross-test state. Each test uses unique
emails / group names / IPs so multiple tests can run concurrently.

Filename starts with `_` so testit skips it during test discovery.
"""
import json
import uuid as _uuid


# Common geo dicts (sent as JSON in X-Mojo-Test-Geo)
GEO_US = {"country_code": "US", "region_code": "US-CA",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_US_FL = {"country_code": "US", "region_code": "US-FL",
             "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_RU = {"country_code": "RU", "region_code": "RU-MOW",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_TOR = {"country_code": "US", "region_code": "US-CA",
           "is_tor": True, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_VPN = {"country_code": "US", "region_code": "US-CA",
           "is_tor": False, "is_vpn": True, "is_proxy": False, "is_datacenter": False}
GEO_DATACENTER = {"country_code": "US", "region_code": "US-CA",
                  "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": True}
GEO_PRIVATE = {"country_code": None, "region_code": None,
               "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}


def headers(*, geo=None, system_rules=None, enabled=None, fail_closed=None,
            allow_private=None, cache_ttl=0, allowlist=None,
            fail_closed_scopes=None):
    """Build the test-mode header dict for a geofence request.

    Defaults `cache_ttl=0` so tests never see stale cached decisions from
    other tests. Pass cache_ttl>0 only when testing cache behavior.

    geo accepts a dict (JSON geo override) or the literal string "fail"
    (forces the lookup_failed path). allowlist is a JSON list replacing
    GEOFENCE_ALLOWLIST; fail_closed_scopes is a list or comma string
    replacing GEOFENCE_FAIL_CLOSED_SCOPES.
    """
    h = {}
    if geo is not None:
        h["X-Mojo-Test-Geo"] = geo if isinstance(geo, str) else json.dumps(geo)
    if system_rules is not None:
        h["X-Mojo-Test-Geofence-System"] = json.dumps(system_rules)
    if allowlist is not None:
        h["X-Mojo-Test-Geofence-Allowlist"] = json.dumps(allowlist)
    if fail_closed_scopes is not None:
        if isinstance(fail_closed_scopes, (list, tuple)):
            fail_closed_scopes = ",".join(fail_closed_scopes)
        h["X-Mojo-Test-Geofence-Fail-Closed-Scopes"] = fail_closed_scopes
    if enabled is not None:
        h["X-Mojo-Test-Geofence-Enabled"] = "1" if enabled else "0"
    if fail_closed is not None:
        h["X-Mojo-Test-Geofence-Fail-Closed"] = "1" if fail_closed else "0"
    if allow_private is not None:
        h["X-Mojo-Test-Geofence-Allow-Private"] = "1" if allow_private else "0"
    h["X-Mojo-Test-Geofence-Cache-Ttl"] = str(cache_ttl)
    return h


def unique_email(prefix):
    """Per-test unique email so parallel tests never collide."""
    return f"{prefix}_{_uuid.uuid4().hex[:8]}@geofence.test"
