"""Evidence-plane tests — every enforcement outcome lands in the incident
Event stream with the right category/level, per-(ip, reason) hourly dedupe,
and block metrics that count deduped occurrences too.

All geofence state here is header-driven (system rules, allowlist, geo, fail
scopes), so nothing leaks to parallel modules. Dedupe keys live in shared
Redis for an hour — every test clears its own key before acting, and event
assertions are count-deltas (robust against events left by previous runs).
Reasons are chosen to avoid cross-module dedupe races: decorator.py blocks on
country_not_allowed/tor_detected, so these tests use region_not_allowed,
vpn_detected, rule_invalid, lookup_failed, and ip_allowlisted.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_geofence._helpers import headers, GEO_US, GEO_RU, GEO_VPN

IP = "127.0.0.1"


def _clear_evt(reason, ip=IP):
    from mojo.helpers.redis import get_connection
    get_connection().delete(f"geofence:evt:{ip}:{reason}")


def _events(category, **filters):
    from mojo.apps.incident.models import Event
    return Event.objects.filter(category=category, **filters)


def _login_attempt(opts, **header_kwargs):
    # Clear ip AND muid login buckets (mirrors testit client.login()) — this
    # module issues many direct login posts, and the muid tier allows only 10
    # per 300s per _muid session cookie.
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="login", muid=muid)
    return opts.client.post(
        "/api/auth/login",
        {"username": opts.test_email, "password": opts.test_password},
        headers=headers(**header_kwargs))


def _blocks_metric():
    """Current-hour geofence:blocks counter via the metrics reader."""
    from mojo.apps import metrics
    resp = metrics.fetch_values(["geofence:blocks"], granularity="hours")
    try:
        return int((resp.get("data") or {}).get("geofence:blocks") or 0)
    except (TypeError, ValueError):
        return 0


@th.django_unit_setup()
def setup_evidence_plane(opts):
    from mojo.apps.account.models import User
    suffix = _uuid.uuid4().hex[:8]
    opts.test_email = f"geofence_evidence_{suffix}@geofence.test"
    opts.test_password = "Geo##evid99"
    user = User.objects.create_user(
        username=opts.test_email, email=opts.test_email, password=opts.test_password)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()


@th.django_unit_test("evidence: auth block emits level-3 geofence_block, deduped hourly")
def test_block_event_level3_and_dedupe(opts):
    _clear_evt("region_not_allowed")
    qs = _events("geofence_block", metadata__reason="region_not_allowed")
    before = qs.count()

    rule = {"country": {"in": ["US"]}, "region": {"in": ["US-FL"]}}
    resp = _login_attempt(opts, geo=GEO_US, system_rules=rule)  # US-CA → region block
    assert resp.status_code == 403, f"region block must 403, got {resp.status_code}"

    assert qs.count() == before + 1, "block must emit exactly one geofence_block event"
    ev = qs.order_by("-id").first()
    assert ev.level == 3, f"plain auth block must be level 3, got {ev.level}"
    assert ev.metadata.get("rule_level") == "system", \
        f"event must carry rule_level, got {ev.metadata.get('rule_level')!r}"
    assert ev.metadata.get("geofence_scope") == "auth", \
        f"event must carry the endpoint scope, got {ev.metadata.get('geofence_scope')!r}"
    assert ev.source_ip == IP, f"event must carry the source ip, got {ev.source_ip!r}"

    # Same (ip, reason) inside the hour → still 403, but NO second event.
    resp = _login_attempt(opts, geo=GEO_US, system_rules=rule)
    assert resp.status_code == 403, "repeat block must still 403"
    assert qs.count() == before + 1, "repeat block within the hour must be deduped"


@th.django_unit_test("evidence: abuse-flag block (vpn) is level 5")
def test_abuse_block_level5(opts):
    _clear_evt("vpn_detected")
    qs = _events("geofence_block", metadata__reason="vpn_detected")
    before = qs.count()
    resp = _login_attempt(opts, geo=GEO_VPN, system_rules={"abuse": {"vpn": False}})
    assert resp.status_code == 403, f"VPN block must 403, got {resp.status_code}"
    assert qs.count() == before + 1, "vpn block must emit an event"
    ev = qs.order_by("-id").first()
    assert ev.level == 5, f"abuse-flag block must be level 5, got {ev.level}"


@th.django_unit_test("evidence: invalid rule at evaluation is level 7 (pages)")
def test_rule_invalid_level7(opts):
    _clear_evt("rule_invalid")
    qs = _events("geofence_block", metadata__reason="rule_invalid")
    before = qs.count()
    # header rules bypass write-time validation on purpose — this is the
    # eval-time backstop path
    resp = _login_attempt(opts, geo=GEO_US, system_rules={"country": {"zap": []}})
    assert resp.status_code == 403, f"invalid rule must fail closed, got {resp.status_code}"
    assert qs.count() == before + 1, "rule_invalid must emit an event"
    ev = qs.order_by("-id").first()
    assert ev.level == 7, f"rule_invalid must be level 7 (pages), got {ev.level}"


@th.django_unit_test("evidence: lookup failure while fail-open allows + emits level 6")
def test_lookup_failed_fail_open_level6(opts):
    _clear_evt("lookup_failed")
    qs = _events("geofence_block", metadata__reason="lookup_failed")
    before = qs.count()
    resp = _login_attempt(opts, geo="fail", system_rules={"country": {"in": ["US"]}})
    assert resp.status_code == 200, \
        f"fail-open lookup failure must allow the login, got {resp.status_code}"
    assert qs.count() == before + 1, "fail-open pass-through must still emit evidence"
    ev = qs.order_by("-id").first()
    assert ev.level == 6, f"fail-open lookup failure must be level 6, got {ev.level}"


@th.django_unit_test("evidence: scope in GEOFENCE_FAIL_CLOSED_SCOPES fails closed, level 5")
def test_scope_fail_closed_level5(opts):
    _clear_evt("lookup_failed")
    qs = _events("geofence_block", metadata__reason="lookup_failed")
    before = qs.count()
    resp = _login_attempt(opts, geo="fail",
                          system_rules={"country": {"in": ["US"]}},
                          fail_closed_scopes=["auth"])
    assert resp.status_code == 403, \
        f"auth scope listed in fail-closed scopes must 403 on lookup failure, got {resp.status_code}"
    assert qs.count() == before + 1, "fail-closed block must emit an event"
    ev = qs.order_by("-id").first()
    assert ev.level == 5, f"block on a fail-closed scope must be level 5, got {ev.level}"


@th.django_unit_test("evidence: allowlisted pass that would block emits geofence_exempt")
def test_exempt_event_and_dedupe(opts):
    _clear_evt("ip_allowlisted")
    qs = _events("geofence_exempt")
    before = qs.count()
    allow = [f"{IP}/32"]
    rule = {"country": {"in": ["US"]}}

    resp = _login_attempt(opts, geo=GEO_RU, system_rules=rule, allowlist=allow)
    assert resp.status_code == 200, \
        f"allowlisted RU login must succeed, got {resp.status_code}: {opts.client.last_response.body}"
    assert qs.count() == before + 1, "exercised exemption must emit a geofence_exempt event"
    ev = qs.order_by("-id").first()
    assert ev.level == 3, f"exempt event must be level 3, got {ev.level}"
    assert ev.metadata.get("would_block_reason") == "country_not_allowed", \
        f"event must record what WOULD have blocked, got {ev.metadata.get('would_block_reason')!r}"
    assert ev.metadata.get("allowlist_source") == "setting", \
        f"event must record the allowlist source, got {ev.metadata.get('allowlist_source')!r}"

    # dedupe: same ip+reason inside the hour → no second event
    resp = _login_attempt(opts, geo=GEO_RU, system_rules=rule, allowlist=allow)
    assert resp.status_code == 200, "repeat exempt login must still succeed"
    assert qs.count() == before + 1, "repeat exemption within the hour must be deduped"

    # a pass that would NOT have blocked emits nothing
    _clear_evt("ip_allowlisted")
    count = qs.count()
    resp = _login_attempt(opts, geo=GEO_US, system_rules=rule, allowlist=allow)
    assert resp.status_code == 200, "non-blocking allowlisted login must succeed"
    assert qs.count() == count, \
        "an exemption that changed nothing must NOT emit an event"


@th.django_unit_test("evidence: geo/check exposes the full ip_allowlisted decision shape")
def test_check_decision_shape_allowlisted(opts):
    resp = opts.client.get("/api/geo/check", headers=headers(
        geo=GEO_RU, system_rules={"country": {"in": ["US"]}}, allowlist=[f"{IP}/32"]))
    assert resp.status_code == 200, f"geo/check got {resp.status_code}"
    d = resp.response.data
    assert d.allowed is True and d.reason == "ip_allowlisted", \
        f"allowlisted decision expected, got {dict(d)}"
    assert d.would_block is True, "decision must carry would_block"
    assert d.would_block_reason == "country_not_allowed", \
        f"decision must carry would_block_reason, got {d.would_block_reason!r}"
    assert d.allowlist_source == "setting", f"got {d.allowlist_source!r}"
    assert d.country_code == "RU", "shadow geo signals must be carried on the decision"

    # expired allowlist entry no longer exempts
    expired = [{"cidr": f"{IP}/32", "until": "2020-01-01T00:00:00Z"}]
    resp = opts.client.get("/api/geo/check", headers=headers(
        geo=GEO_RU, system_rules={"country": {"in": ["US"]}}, allowlist=expired))
    d = resp.response.data
    assert d.allowed is False and d.reason == "country_not_allowed", \
        f"expired allowlist entry must not exempt, got {dict(d)}"


@th.django_unit_test("evidence: block metrics count every block, including deduped ones")
def test_block_metrics_count_deduped(opts):
    _clear_evt("region_not_allowed")
    rule = {"country": {"in": ["US"]}, "region": {"in": ["US-FL"]}}
    before = _blocks_metric()
    resp = _login_attempt(opts, geo=GEO_US, system_rules=rule)
    assert resp.status_code == 403, \
        f"first block expected, got {resp.status_code}: {opts.client.last_response.body}"
    resp = _login_attempt(opts, geo=GEO_US, system_rules=rule)  # deduped event, counted metric
    assert resp.status_code == 403, \
        f"second block expected, got {resp.status_code}: {opts.client.last_response.body}"
    after = _blocks_metric()
    assert after >= before + 2, \
        f"geofence:blocks must count BOTH blocks (deduped too): before={before} after={after}"


@th.django_unit_test("evidence: expired whitelist stops suppressing firewall blocks")
def test_expired_whitelist_block_regression(opts):
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    ip = f"203.0.113.{(int(_uuid.uuid4().hex[:4], 16) % 40) + 210}"
    GeoLocatedIP.objects.filter(ip_address=ip).delete()
    row = GeoLocatedIP.objects.create(ip_address=ip, subnet="203.0.113")
    try:
        # permanent whitelist still suppresses blocking (unchanged behavior)
        row.whitelist(reason="perm dev box")
        assert row.whitelist_active, "permanent whitelist must be active"
        assert row.block(reason="test", ttl=60, broadcast=False) is False, \
            "an active whitelist must suppress block()"
        assert row.block_active is False, "no block may activate under an active whitelist"

        # expired whitelist must NOT suppress blocking anymore
        row.whitelist(reason="expired dev box", until=dates.utcnow() - timedelta(hours=1))
        assert row.whitelist_active is False, "past until must deactivate the whitelist"
        assert row.block(reason="test", ttl=60, broadcast=False) is True, \
            "an EXPIRED whitelist must no longer suppress block()"
        row.refresh_from_db()
        assert row.is_blocked, "block must persist"
        assert row.block_active is True, \
            "block_active must honor whitelist expiry (expired → block wins)"

        # unwhitelist clears the expiry too
        row.unwhitelist()
        row.refresh_from_db()
        assert not row.is_whitelisted and row.whitelisted_until is None, \
            "unwhitelist must clear is_whitelisted AND whitelisted_until"
    finally:
        GeoLocatedIP.objects.filter(ip_address=ip).delete()
