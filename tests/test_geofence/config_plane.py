"""Config-plane tests — editable system rules + allowlist via /api/geo/*,
write-time validation, and decision-cache invalidation on every change path.

These tests write REAL global Setting rows (GEOFENCE_SYSTEM_RULES /
GEOFENCE_ALLOWLIST). Parallel-safety rules:
  - rules used here only bite requests that carry an X-Mojo-Test-Geo header
    (127.0.0.1 traffic from other modules resolves as private_ip → allowed,
    and header-driven geofence tests override system rules per-request);
  - the DB allowlist must NEVER cover 127.0.0.1 (it would flip other modules'
    expected 403s into ip_allowlisted allows) — use TEST-NET-3
    (203.0.113.0/24) entries only;
  - every mutating test restores state in `finally`.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_geofence._helpers import headers, GEO_RU, GEO_US

SYSTEM_KEY = "GEOFENCE_SYSTEM_RULES"
ALLOW_KEY = "GEOFENCE_ALLOWLIST"
FUTURE = "2999-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


def _cleanup_settings():
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    Setting.remove(SYSTEM_KEY)
    Setting.remove(ALLOW_KEY)
    gf_cache.invalidate_all()


def _system_row():
    from mojo.apps.account.models.setting import Setting
    return Setting.objects.filter(key=SYSTEM_KEY, group=None).first()


def _login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


def _clear_geo_check_limit():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="geo_check")


@th.django_unit_setup()
def setup_config_plane(opts):
    from mojo.apps.account.models import User

    # Long-lived DB: clean anything a previous run left behind BEFORE creating.
    _cleanup_settings()
    _clear_geo_check_limit()

    suffix = _uuid.uuid4().hex[:8]
    opts.admin_email = f"geofence_admin_{suffix}@geofence.test"
    opts.admin_password = "Geo##admin99"
    admin = User.objects.create_user(
        username=opts.admin_email, email=opts.admin_email, password=opts.admin_password)
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.add_permission(
        ["manage_geofence", "manage_groups", "manage_settings", "manage_security"])
    admin.save()

    opts.viewer_email = f"geofence_viewer_{suffix}@geofence.test"
    opts.viewer_password = "Geo##viewer99"
    viewer = User.objects.create_user(
        username=opts.viewer_email, email=opts.viewer_email, password=opts.viewer_password)
    viewer.is_email_verified = True
    viewer.requires_mfa = False
    viewer.add_permission("view_geofence")
    viewer.save()

    opts.plain_email = f"geofence_plain_{suffix}@geofence.test"
    opts.plain_password = "Geo##plain99"
    plain = User.objects.create_user(
        username=opts.plain_email, email=opts.plain_email, password=opts.plain_password)
    plain.is_email_verified = True
    plain.requires_mfa = False
    plain.save()


@th.django_unit_test("config: POST geo/rules persists + GET returns effective config")
def test_rules_post_and_get(opts):
    from mojo.apps.incident.models import Event
    _login(opts, opts.admin_email, opts.admin_password)
    try:
        events_before = Event.objects.filter(category="geofence_config").count()
        rule = {"country": {"in": ["US", "CA"]}}
        resp = opts.client.post("/api/geo/rules", {"rule": rule})
        assert resp.status_code == 200, \
            f"POST geo/rules must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        assert resp.response.data.source == "setting", "saved rule must report source=setting"
        assert _system_row() is not None, "Setting row must exist after POST"

        resp = opts.client.get("/api/geo/rules")
        assert resp.status_code == 200, f"GET geo/rules got {resp.status_code}"
        d = resp.response.data
        assert d.system.rule.country["in"] == ["US", "CA"], \
            f"effective system rule mismatch: {dict(d.system.rule)}"
        assert d.system.source == "setting", f"source must be setting, got {d.system.source!r}"
        assert d.system.modified, "modified stamp must be present for a DB-backed rule"
        assert d.posture.enabled is True, "posture.enabled default must be True"
        assert d.posture.cache_ttl == 300, f"default cache_ttl 300, got {d.posture.cache_ttl}"
        assert list(d.evaluation_order) == ["system", "group"], \
            f"evaluation_order wrong: {d.evaluation_order}"
        assert len(d.enforced_endpoints) > 0, \
            "enforced_endpoints must list the @requires_geofence auth endpoints"
        assert "allowlist_summary" in d, "response must include allowlist_summary"

        # Config-change evidence: the POST must land in the geofence_config stream
        events_after = Event.objects.filter(category="geofence_config").count()
        assert events_after > events_before, "POST geo/rules must emit a geofence_config event"
        ev = Event.objects.filter(category="geofence_config").order_by("-id").first()
        assert ev.metadata.get("target") == "system", f"event target: {ev.metadata.get('target')!r}"
        assert ev.metadata.get("changed_by") == opts.admin_email, \
            f"event must attribute the change, got {ev.metadata.get('changed_by')!r}"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: POST geo/rules rejects malformed rules with a readable 400")
def test_rules_post_invalid_rejected(opts):
    _login(opts, opts.admin_email, opts.admin_password)
    try:
        resp = opts.client.post("/api/geo/rules", {"rule": {"country": {"bogus": ["US"]}}})
        assert resp.status_code == 400, f"bad operator must 400, got {resp.status_code}"
        assert "bogus" in str(opts.client.last_response.body), \
            f"error must name the bad operator: {opts.client.last_response.body}"
        assert _system_row() is None, "invalid rule must not be persisted"

        resp = opts.client.post("/api/geo/rules", {"rule": "not-a-dict"})
        assert resp.status_code == 400, f"non-dict rule must 400, got {resp.status_code}"

        resp = opts.client.post("/api/geo/rules", {})
        assert resp.status_code == 400, f"missing rule must 400, got {resp.status_code}"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: geo/rules + simulate are perm-gated (view vs manage)")
def test_rules_perms(opts):
    # viewer: GET allowed, POST denied
    _login(opts, opts.viewer_email, opts.viewer_password)
    resp = opts.client.get("/api/geo/rules")
    assert resp.status_code == 200, f"view_geofence must allow GET, got {resp.status_code}"
    resp = opts.client.post("/api/geo/rules", {"rule": {}})
    assert resp.status_code == 403, f"view_geofence must NOT allow POST, got {resp.status_code}"
    assert _system_row() is None, "denied POST must not write"
    resp = opts.client.post("/api/geo/simulate", {"geo": dict(GEO_RU)})
    assert resp.status_code == 200, f"view_geofence must allow simulate, got {resp.status_code}"
    opts.client.logout()

    # plain user: everything denied
    _login(opts, opts.plain_email, opts.plain_password)
    resp = opts.client.get("/api/geo/rules")
    assert resp.status_code == 403, f"no-perm GET must 403, got {resp.status_code}"
    resp = opts.client.post("/api/geo/simulate", {"geo": dict(GEO_RU)})
    assert resp.status_code == 403, f"no-perm simulate must 403, got {resp.status_code}"
    resp = opts.client.get("/api/geo/bypass_holders")
    assert resp.status_code == 403, f"no-perm bypass_holders must 403, got {resp.status_code}"
    opts.client.logout()


@th.django_unit_test("config: generic /api/settings write path is validated too")
def test_settings_rest_backdoor_validated(opts):
    _login(opts, opts.admin_email, opts.admin_password)
    try:
        resp = opts.client.post(
            "/api/settings", {"key": SYSTEM_KEY, "value": '{"country": {"zap": []}}'})
        assert resp.status_code == 400, \
            f"generic settings REST must reject a bad geofence rule, got {resp.status_code}"
        assert _system_row() is None, "rejected settings write must not persist"

        resp = opts.client.post(
            "/api/settings", {"key": SYSTEM_KEY, "value": '{"country": {"in": ["US"]}}'})
        assert resp.status_code == 200, \
            f"valid rule via settings REST must save, got {resp.status_code}: {opts.client.last_response.body}"
        assert _system_row() is not None, "valid settings write must persist"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: system rule edit invalidates cached decisions immediately")
def test_rules_cache_invalidation(opts):
    # All cached denies here are primed under a group-scoped cache key
    # ((ip, group_id) via group_uuid) — NEVER under the shared (ip, no-group)
    # key: parallel modules issue unheadered auth requests with the default
    # cache TTL and would be served our poisoned deny.
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.services.geofence import cache as gf_cache
    _login(opts, opts.admin_email, opts.admin_password)
    _clear_geo_check_limit()
    grp = Group.objects.create(
        name=f"Geofence CacheInv {_uuid.uuid4().hex[:8]}", is_active=True)
    grp.get_uuid()
    try:
        resp = opts.client.post("/api/geo/rules", {"rule": {"country": {"in": ["US"]}}})
        assert resp.status_code == 200, f"seed rule failed: {resp.status_code}"

        # Prime a cached DENY: geo header says RU, rules come from the DB row.
        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_RU, cache_ttl=300))
        d = resp.response.data
        assert d.allowed is False and d.reason == "country_not_allowed", \
            f"RU must be denied by the DB rule, got {dict(d)}"

        # Prove the cache is live: a US request now returns the STALE cached deny.
        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_US, cache_ttl=300))
        assert resp.response.data.allowed is False, \
            "sanity: decision must come from cache (stale deny) before invalidation"

        # Emergency edit: allow RU. Setting.save() must invalidate the cache.
        resp = opts.client.post("/api/geo/rules", {"rule": {"country": {"in": ["US", "RU"]}}})
        assert resp.status_code == 200, f"rule update failed: {resp.status_code}"

        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_RU, cache_ttl=300))
        d = resp.response.data
        assert d.allowed is True and d.reason == "passed", \
            f"stale cached deny must be gone after the rule edit, got {dict(d)}"
    finally:
        gf_cache.invalidate_group(grp.pk)
        grp.delete()
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: group rule validated on save + group cache invalidated")
def test_group_rule_validation_and_invalidation(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.services.geofence import cache as gf_cache
    _login(opts, opts.admin_email, opts.admin_password)
    _clear_geo_check_limit()
    suffix = _uuid.uuid4().hex[:8]
    grp = Group.objects.create(name=f"Geofence Config {suffix}", is_active=True)
    grp.get_uuid()
    try:
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence": {"country": {"nope": []}}}})
        assert resp.status_code == 400, \
            f"invalid group rule must 400 at write time, got {resp.status_code}"
        assert "nope" in str(opts.client.last_response.body), \
            f"error must be human-readable: {opts.client.last_response.body}"
        grp.refresh_from_db()
        assert not (grp.metadata or {}).get("geofence"), "rejected rule must not persist"

        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence": {"country": {"in": ["US"]}}}})
        assert resp.status_code == 200, \
            f"valid group rule must save, got {resp.status_code}: {opts.client.last_response.body}"

        # Prime a cached DENY against the group, then prove staleness.
        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_RU, cache_ttl=300))
        d = resp.response.data
        assert d.allowed is False and d.rule_level == "group", \
            f"RU must be denied by the group rule, got {dict(d)}"
        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_US, cache_ttl=300))
        assert resp.response.data.allowed is False, \
            "sanity: group decision must come from cache before invalidation"

        # Rule edit via group REST → invalidate_group must clear it.
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence": {"country": {"in": ["US", "RU"]}}}})
        assert resp.status_code == 200, f"group rule update failed: {resp.status_code}"
        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_RU, cache_ttl=300))
        d = resp.response.data
        assert d.allowed is True, \
            f"stale group deny must be gone after the metadata edit, got {dict(d)}"
    finally:
        gf_cache.invalidate_group(grp.pk)
        grp.delete()
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: allowlist POST/GET round-trip, validation, expiry flags")
def test_allowlist_post_get(opts):
    _login(opts, opts.admin_email, opts.admin_password)
    try:
        entries = [
            "203.0.113.0/24",
            {"cidr": "203.0.113.77", "reason": "qa box", "until": FUTURE},
            {"cidr": "203.0.113.99", "reason": "old contractor", "until": PAST},
        ]
        resp = opts.client.post("/api/geo/allowlist", {"entries": entries})
        assert resp.status_code == 200, \
            f"valid allowlist must save, got {resp.status_code}: {opts.client.last_response.body}"

        resp = opts.client.get("/api/geo/allowlist")
        assert resp.status_code == 200, f"GET allowlist got {resp.status_code}"
        d = resp.response.data
        assert len(d.setting) == 3, f"3 setting entries expected, got {len(d.setting)}"
        by_cidr = {e.cidr: e for e in d.setting}
        assert by_cidr["203.0.113.0/24"].active is True, "plain CIDR entry must be active"
        assert by_cidr["203.0.113.77"].active is True, "future-until entry must be active"
        assert by_cidr["203.0.113.77"].reason == "qa box", "reason must round-trip"
        assert by_cidr["203.0.113.99"].active is False, \
            "expired entry must be listed with active=false (not hidden)"
        assert "geoip" in d, "response must include the geoip whitelist section"

        # validation failures
        resp = opts.client.post("/api/geo/allowlist", {"entries": ["not-a-cidr"]})
        assert resp.status_code == 400, f"bad CIDR must 400, got {resp.status_code}"
        resp = opts.client.post(
            "/api/geo/allowlist", {"entries": [{"cidr": "203.0.113.5", "until": "garbage"}]})
        assert resp.status_code == 400, f"bad until must 400, got {resp.status_code}"
        resp = opts.client.post("/api/geo/allowlist", {})
        assert resp.status_code == 400, f"missing entries must 400, got {resp.status_code}"

        # full replace with empty list clears it
        resp = opts.client.post("/api/geo/allowlist", {"entries": []})
        assert resp.status_code == 200, f"empty replace must succeed, got {resp.status_code}"
        resp = opts.client.get("/api/geo/allowlist")
        assert len(resp.response.data.setting) == 0, "empty replace must clear entries"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: allowlist edit invalidates cached decisions")
def test_allowlist_cache_invalidation(opts):
    # Group-scoped cache key only — see test_rules_cache_invalidation.
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.services.geofence import cache as gf_cache
    _login(opts, opts.admin_email, opts.admin_password)
    _clear_geo_check_limit()
    grp = Group.objects.create(
        name=f"Geofence AllowInv {_uuid.uuid4().hex[:8]}", is_active=True)
    grp.get_uuid()
    try:
        resp = opts.client.post("/api/geo/rules", {"rule": {"country": {"in": ["US"]}}})
        assert resp.status_code == 200, f"seed rule failed: {resp.status_code}"
        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_RU, cache_ttl=300))
        assert resp.response.data.allowed is False, "RU must be denied by the DB rule"
        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_US, cache_ttl=300))
        assert resp.response.data.allowed is False, "sanity: stale cached deny expected"

        # Any allowlist change must clear the decision cache (TEST-NET only —
        # never 127.0.0.1, see module docstring).
        resp = opts.client.post("/api/geo/allowlist", {"entries": ["203.0.113.0/24"]})
        assert resp.status_code == 200, f"allowlist save failed: {resp.status_code}"

        resp = opts.client.get(f"/api/geo/check?group_uuid={grp.uuid}",
                               headers=headers(geo=GEO_US, cache_ttl=300))
        d = resp.response.data
        assert d.allowed is True and d.reason == "passed", \
            f"allowlist edit must invalidate cached decisions, got {dict(d)}"
    finally:
        gf_cache.invalidate_group(grp.pk)
        grp.delete()
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: geoip whitelist action (until/ttl) + simulate + invalidate_ip")
def test_geoip_whitelist_action_and_simulate(opts):
    from datetime import timedelta
    from mojo.helpers import dates
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.account.services.geofence import cache as gf_cache
    _login(opts, opts.admin_email, opts.admin_password)
    ip = f"203.0.113.{(int(_uuid.uuid4().hex[:4], 16) % 200) + 10}"
    GeoLocatedIP.objects.filter(ip_address=ip).delete()
    row = GeoLocatedIP.objects.create(ip_address=ip, subnet="203.0.113")
    try:
        # whitelist via the existing REST action, dict form with ttl
        resp = opts.client.post(f"/api/system/geoip/{row.pk}",
                                {"whitelist": {"reason": "dev box", "ttl": 3600}})
        assert resp.status_code == 200, \
            f"whitelist action failed: {resp.status_code}: {opts.client.last_response.body}"
        row.refresh_from_db()
        assert row.is_whitelisted, "action must set is_whitelisted"
        assert row.whitelisted_until is not None, "ttl must set whitelisted_until"
        assert row.whitelist_active, "fresh ttl whitelist must be active"

        # bad until must 400
        resp = opts.client.post(f"/api/system/geoip/{row.pk}",
                                {"whitelist": {"reason": "x", "until": "garbage"}})
        assert resp.status_code == 400, f"bad until must 400, got {resp.status_code}"

        # simulate: DB rule blocks RU; the geoip whitelist exempts this ip
        resp = opts.client.post("/api/geo/rules", {"rule": {"country": {"in": ["US"]}}})
        assert resp.status_code == 200, f"seed rule failed: {resp.status_code}"
        resp = opts.client.post("/api/geo/simulate", {"ip": ip},
                                headers=headers(geo=GEO_RU))
        assert resp.status_code == 200, f"simulate failed: {opts.client.last_response.body}"
        d = resp.response.data
        assert d.allowed is True and d.reason == "ip_allowlisted", \
            f"whitelisted ip must simulate as exempt, got {dict(d)}"
        assert d.allowlist_source == "geoip", f"source must be geoip, got {d.allowlist_source!r}"
        assert d.would_block is True and d.would_block_reason == "country_not_allowed", \
            f"simulate must expose the would-block outcome, got {dict(d)}"

        # invalidate_ip: whitelist changes clear that IP's cached decisions
        gf_cache.set(ip, None, {"allowed": True, "reason": "passed"}, 300)
        gf_cache.set(ip, 999999, {"allowed": True, "reason": "passed"}, 300)
        row.refresh_from_db()
        row.whitelist(reason="again")
        assert gf_cache.get(ip, None) is None, "whitelist() must invalidate the ip's cache"
        assert gf_cache.get(ip, 999999) is None, "whitelist() must invalidate ALL group scopes"

        # expiry: a past until stops the exemption and shows active=false
        row.whitelist(reason="expired dev box", until=dates.utcnow() - timedelta(hours=1))
        assert row.whitelist_active is False, "past until must deactivate the whitelist"
        resp = opts.client.post("/api/geo/simulate", {"ip": ip},
                                headers=headers(geo=GEO_RU))
        d = resp.response.data
        assert d.allowed is False and d.reason == "country_not_allowed", \
            f"expired whitelist must no longer exempt, got {dict(d)}"
        resp = opts.client.get("/api/geo/allowlist")
        mine = [e for e in resp.response.data.geoip if e.ip == ip]
        assert mine and mine[0].active is False, \
            f"expired geoip entry must be listed active=false, got {mine}"
    finally:
        GeoLocatedIP.objects.filter(ip_address=ip).delete()
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: DELETE geo/rules removes the DB override")
def test_rules_delete(opts):
    _login(opts, opts.admin_email, opts.admin_password)
    try:
        resp = opts.client.post("/api/geo/rules", {"rule": {"country": {"in": ["US"]}}})
        assert resp.status_code == 200 and _system_row() is not None, "seed rule must persist"
        resp = opts.client.delete("/api/geo/rules")
        assert resp.status_code == 200, f"DELETE geo/rules got {resp.status_code}"
        assert resp.response.data.removed is True, "delete must report removed=true"
        assert _system_row() is None, "Setting row must be gone after DELETE"
        resp = opts.client.get("/api/geo/rules")
        d = resp.response.data
        assert d.system.source == "none", \
            f"no conf value in testproject → source none, got {d.system.source!r}"
        assert not d.system.rule, f"rule must be empty after delete, got {dict(d.system.rule)}"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("config: bypass_holders lists truthy grants, skips falsy, flags superusers")
def test_bypass_holders(opts):
    from mojo.apps.account.models import User
    _login(opts, opts.admin_email, opts.admin_password)
    suffix = _uuid.uuid4().hex[:8]
    holder = User.objects.create_user(
        username=f"gf_holder_{suffix}@geofence.test",
        email=f"gf_holder_{suffix}@geofence.test", password="Geo##hold99")
    holder.add_permission("bypass_geofence")
    holder.save()
    falsy = User.objects.create_user(
        username=f"gf_falsy_{suffix}@geofence.test",
        email=f"gf_falsy_{suffix}@geofence.test", password="Geo##falsy99")
    falsy.permissions["bypass_geofence"] = False
    falsy.save()
    try:
        resp = opts.client.get("/api/geo/bypass_holders")
        assert resp.status_code == 200, f"bypass_holders got {resp.status_code}"
        d = resp.response.data
        by_id = {h.id: h for h in d.holders}
        assert holder.pk in by_id, "explicit truthy grant must be listed"
        assert by_id[holder.pk].source == "permission", \
            f"holder source must be permission, got {by_id[holder.pk].source!r}"
        assert falsy.pk not in by_id, \
            "falsy grant must NOT be listed (has_permission would deny it)"
        assert d.count == len(d.holders), "count must match the returned list"
    finally:
        holder.delete()
        falsy.delete()
        opts.client.logout()
