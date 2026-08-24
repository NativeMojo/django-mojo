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

TESTIT_TIER = "extended"
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


# test_settings_rest_backdoor_validated moved to
# tests/test_geofence_extended_serial/config_plane.py — it writes protected
# GEOFENCE_* settings through the generic /api/settings REST path (maestro
# item #1839).




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
        # PII guard: email/display_name are "users"-category data and must not
        # leak through a geofence-only permission
        row = by_id[holder.pk]
        assert "email" not in row, f"bypass_holders must not expose email: {dict(row)}"
        assert "display_name" not in row, \
            f"bypass_holders must not expose display_name: {dict(row)}"
    finally:
        holder.delete()
        falsy.delete()
        opts.client.logout()


@th.django_unit_test("config: GroupMember-scoped grant must NOT reach global config")
def test_group_scoped_perm_cannot_touch_global_config(opts):
    """SECURITY regression: requires_perms' group fallback lets a per-group
    (GroupMember) permission satisfy endpoint checks when the client passes a
    "group" param. These endpoints act on PLATFORM-GLOBAL config, so a
    tenant-admin-assignable member grant must never authorize them —
    _requires_global_perms checks global User.permissions only."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember
    suffix = _uuid.uuid4().hex[:8]
    email = f"gf_tenant_admin_{suffix}@geofence.test"
    password = "Geo##tenant99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    grp = Group.objects.create(name=f"Geofence Tenant {suffix}", is_active=True)
    grp.add_member(user)
    member = GroupMember.objects.get(group=grp, user=user)
    member.add_permission("manage_geofence")
    member.add_permission("security")
    member.save()
    assert member.has_permission("manage_geofence"), \
        "setup: member-scoped grant must exist for this test to mean anything"
    try:
        _login(opts, email, password)
        resp = opts.client.post(
            "/api/geo/rules",
            {"rule": {"country": {"in": ["US"]}}, "group": grp.pk})
        assert resp.status_code == 403, \
            f"group-scoped grant must NOT rewrite global rules, got {resp.status_code}"
        assert _system_row() is None, "no global write may occur"
        resp = opts.client.get(f"/api/geo/rules?group={grp.pk}")
        assert resp.status_code == 403, \
            f"group-scoped grant must NOT read global config, got {resp.status_code}"
        resp = opts.client.post(
            "/api/geo/allowlist",
            {"entries": ["203.0.113.0/24"], "group": grp.pk})
        assert resp.status_code == 403, \
            f"group-scoped grant must NOT rewrite the allowlist, got {resp.status_code}"
        resp = opts.client.get(f"/api/geo/bypass_holders?group={grp.pk}")
        assert resp.status_code == 403, \
            f"group-scoped grant must NOT read bypass holders, got {resp.status_code}"
    finally:
        opts.client.logout()
        member.delete()
        grp.delete()
        user.delete()
        _cleanup_settings()
