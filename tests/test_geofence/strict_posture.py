"""Strict-posture tests — opt-in compliance stance (ITEM-021).

One bundled switch: GEOFENCE_STRICT_POSTURE (global) with a per-group
tri-state override at Group.metadata["geofence_strict"]. Strict = fail-closed
on lookup failure + deny private IPs + deny when no rules are configured.

Parallel-safety:
  - engine-behavior tests drive strict via the X-Mojo-Test-Geofence-Strict
    header and ALWAYS pin system rules via header (config_plane writes real
    DB system-rule rows in parallel);
  - DB-backed strict is only ever exercised through a per-group override —
    a global GEOFENCE_STRICT_POSTURE=true row would 403 every unheadered
    request from other modules (no rules → no_rules_strict). The /api/settings
    validation test therefore writes "false", never "true";
  - evidence tests use the no_rules_strict reason — unique to this module, so
    the hourly (ip, reason) dedupe can't race other modules.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_geofence._helpers import headers, GEO_US, GEO_RU, GEO_PRIVATE

IP = "127.0.0.1"
US_RULE = {"country": {"in": ["US"]}}


def _cleanup_strict_setting():
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    Setting.remove("GEOFENCE_STRICT_POSTURE")
    gf_cache.invalidate_all()


def _login_attempt(opts, **header_kwargs):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    muid = opts.client.session.cookies.get("_muid")
    if muid:
        clear_rate_limits(key="login", muid=muid)
    return opts.client.post(
        "/api/auth/login",
        {"username": opts.test_email, "password": opts.test_password},
        headers=headers(**header_kwargs))


def _geo_check(opts, group=None, **header_kwargs):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="geo_check")
    url = "/api/geo/check"
    if group is not None:
        url += f"?group_uuid={group.uuid}"
    return opts.client.get(url, headers=headers(**header_kwargs))


def _make_group(name_prefix, metadata=None):
    from mojo.apps.account.models.group import Group
    grp = Group.objects.create(
        name=f"{name_prefix} {_uuid.uuid4().hex[:8]}",
        is_active=True, metadata=metadata or {})
    grp.get_uuid()
    return grp


@th.django_unit_setup()
def setup_strict_posture(opts):
    from mojo.apps.account.models import User

    _cleanup_strict_setting()

    suffix = _uuid.uuid4().hex[:8]
    opts.test_email = f"geofence_strict_{suffix}@geofence.test"
    opts.test_password = "Geo##strict99"
    user = User.objects.create_user(
        username=opts.test_email, email=opts.test_email, password=opts.test_password)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()

    opts.admin_email = f"geofence_strictadm_{suffix}@geofence.test"
    opts.admin_password = "Geo##stradm99"
    admin = User.objects.create_user(
        username=opts.admin_email, email=opts.admin_email, password=opts.admin_password)
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.add_permission(
        ["manage_geofence", "manage_groups", "manage_settings"])
    admin.save()


def _admin_login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    ok = opts.client.login(opts.admin_email, opts.admin_password)
    assert ok, f"admin login failed: {opts.client.last_response.body}"


@th.django_unit_test("strict: no rules configured denies (opt-in — default still allows)")
def test_strict_no_rules_denies(opts):
    resp = _login_attempt(opts, strict=True, system_rules={})
    assert resp.status_code == 403, \
        f"strict + no rules must deny, got {resp.status_code}: {opts.client.last_response.body}"
    assert resp.response.reason == "no_rules_strict", \
        f"reason must be no_rules_strict, got {resp.response.reason!r}"

    resp = _login_attempt(opts, strict=False, system_rules={})
    assert resp.status_code == 200, \
        f"non-strict + no rules must keep allowing (opt-in!), got {resp.status_code}"


@th.django_unit_test("strict: lookup failure fails closed (default stays fail-open)")
def test_strict_lookup_failure_fails_closed(opts):
    resp = _login_attempt(opts, strict=True, geo="fail", system_rules=US_RULE)
    assert resp.status_code == 403, \
        f"strict lookup failure must fail closed, got {resp.status_code}"
    assert resp.response.reason == "lookup_failed", \
        f"reason must be lookup_failed, got {resp.response.reason!r}"

    resp = _login_attempt(opts, strict=False, geo="fail", system_rules=US_RULE)
    assert resp.status_code == 200, \
        f"default posture must stay fail-open on lookup failure, got {resp.status_code}"


@th.django_unit_test("strict: private IP denied (default stays allowed)")
def test_strict_denies_private_ip(opts):
    resp = _login_attempt(opts, strict=True, geo=GEO_PRIVATE, system_rules=US_RULE)
    assert resp.status_code == 403, \
        f"strict must deny private IPs, got {resp.status_code}"
    assert resp.response.reason == "private_ip", \
        f"reason must be private_ip, got {resp.response.reason!r}"

    resp = _login_attempt(opts, strict=False, geo=GEO_PRIVATE, system_rules=US_RULE)
    assert resp.status_code == 200, \
        f"default posture must keep allowing private IPs, got {resp.status_code}"


@th.django_unit_test("strict: allowlisted IP still exempt; shadow records no_rules_strict")
def test_strict_allowlist_still_exempts(opts):
    resp = _geo_check(opts, strict=True, system_rules={}, allowlist=[IP])
    assert resp.status_code == 200, f"geo/check failed: {resp.status_code}"
    d = resp.response.data
    assert d.allowed is True and d.reason == "ip_allowlisted", \
        f"allowlisted IP must stay exempt under strict, got {dict(d)}"
    assert d.would_block is True and d.would_block_reason == "no_rules_strict", \
        f"shadow must record the strict would-block outcome, got {dict(d)}"


@th.django_unit_test("strict: decision carries strict_posture flag")
def test_decision_carries_strict_flag(opts):
    resp = _geo_check(opts, strict=True, system_rules=US_RULE, geo=GEO_US)
    d = resp.response.data
    assert d.allowed is True and d.reason == "passed", f"US must pass, got {dict(d)}"
    assert d.strict_posture is True, \
        f"decision must carry strict_posture=true, got {dict(d)}"

    resp = _geo_check(opts, strict=False, system_rules=US_RULE, geo=GEO_US)
    assert resp.response.data.strict_posture is False, \
        "non-strict decision must carry strict_posture=false"


@th.django_unit_test("strict: per-group override tightens (global off)")
def test_group_override_strict(opts):
    from mojo.apps.account.services.geofence import cache as gf_cache
    strict_grp = _make_group("GF StrictOn", {"geofence_strict": True})
    plain_grp = _make_group("GF StrictOff")
    try:
        # No rules anywhere (header-pinned); strict group must deny.
        resp = _geo_check(opts, group=strict_grp, system_rules={})
        d = resp.response.data
        assert d.allowed is False and d.reason == "no_rules_strict", \
            f"group override strict=true must deny no-rules, got {dict(d)}"

        resp = _geo_check(opts, group=plain_grp, system_rules={})
        d = resp.response.data
        assert d.allowed is True and d.reason == "no_rules", \
            f"group without override must inherit permissive global, got {dict(d)}"
    finally:
        gf_cache.invalidate_group(strict_grp.pk)
        gf_cache.invalidate_group(plain_grp.pk)
        strict_grp.delete()
        plain_grp.delete()


@th.django_unit_test("strict: per-group override false loosens a strict global")
def test_group_override_loosens(opts):
    from mojo.apps.account.services.geofence import cache as gf_cache
    loose_grp = _make_group("GF StrictExempt", {"geofence_strict": False})
    try:
        # Global strict via header; the group's explicit false must win.
        resp = _geo_check(opts, group=loose_grp, strict=True, system_rules={})
        d = resp.response.data
        assert d.allowed is True and d.reason == "no_rules", \
            f"group geofence_strict=false must override a strict global, got {dict(d)}"
    finally:
        gf_cache.invalidate_group(loose_grp.pk)
        loose_grp.delete()


@th.django_unit_test("strict: block evidence escalates to level 5")
def test_strict_block_level5_event(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.incident.models import Event
    get_connection().delete(f"geofence:evt:{IP}:no_rules_strict")
    qs = Event.objects.filter(
        category="geofence_block", metadata__reason="no_rules_strict")
    before = qs.count()

    resp = _login_attempt(opts, strict=True, system_rules={})
    assert resp.status_code == 403, f"strict block expected, got {resp.status_code}"

    assert qs.count() == before + 1, \
        "strict block must emit a geofence_block event"
    ev = qs.order_by("-id").first()
    assert ev.level == 5, \
        f"a block under strict posture is compliance-grade (level 5), got {ev.level}"


@th.django_unit_test("strict: group metadata.geofence_strict is validated on REST write")
def test_group_strict_write_validation(opts):
    grp = _make_group("GF StrictValidate")
    _admin_login(opts)
    try:
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence_strict": "yes"}})
        assert resp.status_code == 400, \
            f"non-bool geofence_strict must 400, got {resp.status_code}"
        assert "geofence_strict" in str(opts.client.last_response.body), \
            f"error must name the field: {opts.client.last_response.body}"
        grp.refresh_from_db()
        assert (grp.metadata or {}).get("geofence_strict") is None, \
            "rejected write must not persist"

        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence_strict": True}})
        assert resp.status_code == 200, \
            f"boolean geofence_strict must save, got {resp.status_code}: {opts.client.last_response.body}"
        grp.refresh_from_db()
        assert grp.metadata.get("geofence_strict") is True, "true must persist"

        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence_strict": None}})
        assert resp.status_code == 200, \
            f"null (inherit) must be accepted, got {resp.status_code}"
        grp.refresh_from_db()
        assert grp.metadata.get("geofence_strict") is None, \
            "null must clear the override back to inherit"
    finally:
        opts.client.logout()
        grp.delete()


@th.django_unit_test("strict: group-scoped admin cannot flip geofence_strict (global perm required)")
def test_group_strict_requires_global_perm(opts):
    """SECURITY: a tenant admin who can edit the group must NOT be able to opt
    their group out of (or into) a platform compliance posture — changing
    geofence_strict needs the global manage_geofence/security trust level."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.decorators.limits import clear_rate_limits
    suffix = _uuid.uuid4().hex[:8]
    email = f"gf_tenant_{suffix}@geofence.test"
    password = "Geo##tenant99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    grp = Group.objects.create(name=f"GF TenantStrict {suffix}", is_active=True)
    grp.add_member(user)
    member = GroupMember.objects.get(group=grp, user=user)
    member.add_permission("manage_group")
    member.save()
    try:
        clear_rate_limits(ip=IP, key="login")
        ok = opts.client.login(email, password)
        assert ok, f"tenant login failed: {opts.client.last_response.body}"

        # sanity: the member CAN edit ordinary group metadata
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"motto": "we ship"}})
        assert resp.status_code == 200, \
            f"tenant admin must still edit normal metadata, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"

        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence_strict": False}})
        assert resp.status_code == 403, \
            f"tenant admin must NOT flip geofence_strict, got {resp.status_code}"
        grp.refresh_from_db()
        assert (grp.metadata or {}).get("geofence_strict") is None, \
            "denied flip must not persist"

        # no-op writes that leave geofence_strict untouched stay allowed
        grp.metadata["geofence_strict"] = True
        grp.save()
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"motto": "still shipping"}})
        assert resp.status_code == 200, \
            f"unrelated metadata edit must not trip the gate, got {resp.status_code}: " \
            f"{opts.client.last_response.body}"
    finally:
        opts.client.logout()
        member.delete()
        grp.delete()
        user.delete()


@th.django_unit_test("strict: geofence_strict flip emits geofence_config evidence")
def test_group_strict_flip_audited(opts):
    from mojo.apps.incident.models import Event
    grp = _make_group("GF StrictAudit")
    _admin_login(opts)
    try:
        qs = Event.objects.filter(
            category="geofence_config", metadata__target=f"group:{grp.pk}")
        before = qs.count()
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence_strict": True}})
        assert resp.status_code == 200, f"flip failed: {resp.status_code}"
        assert qs.count() == before + 1, \
            "geofence_strict flip must emit a geofence_config event"
        ev = qs.order_by("-id").first()
        assert ev.metadata.get("new") is True and ev.metadata.get("old") is None, \
            f"event must carry old/new, got {dict(ev.metadata)}"
        assert ev.metadata.get("changed_by") == opts.admin_email, \
            f"event must attribute the actor, got {ev.metadata.get('changed_by')!r}"

        # unchanged writes must NOT spam the evidence stream
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence_strict": True}})
        assert resp.status_code == 200, f"no-op write failed: {resp.status_code}"
        assert qs.count() == before + 1, "no-change write must not emit an event"
    finally:
        opts.client.logout()
        grp.delete()


@th.django_unit_test("strict: /api/settings write path validates GEOFENCE_STRICT_POSTURE")
def test_setting_write_validation(opts):
    from mojo.apps.account.models.setting import Setting
    _admin_login(opts)
    try:
        # NOTE: only "false" is ever persisted here — a global strict=true row
        # would deny unheadered requests from parallel test modules.
        resp = opts.client.post(
            "/api/settings", {"key": "GEOFENCE_STRICT_POSTURE", "value": "maybe"})
        assert resp.status_code == 400, \
            f"non-JSON value must 400, got {resp.status_code}"
        resp = opts.client.post(
            "/api/settings", {"key": "GEOFENCE_STRICT_POSTURE", "value": "1"})
        assert resp.status_code == 400, \
            f"non-boolean JSON must 400 (kind=bool coerces garbage truthy), got {resp.status_code}"
        assert Setting.objects.filter(
            key="GEOFENCE_STRICT_POSTURE", group=None).first() is None, \
            "rejected writes must not persist"

        resp = opts.client.post(
            "/api/settings", {"key": "GEOFENCE_STRICT_POSTURE", "value": "false"})
        assert resp.status_code == 200, \
            f"boolean value must save, got {resp.status_code}: {opts.client.last_response.body}"

        # group-scoped rows are dead config for this key — reject loudly
        grp = _make_group("GF StrictScope")
        try:
            resp = opts.client.post(
                "/api/settings",
                {"key": "GEOFENCE_STRICT_POSTURE", "value": "false", "group": grp.pk})
            assert resp.status_code == 400, \
                f"group-scoped strict setting must 400, got {resp.status_code}"
            assert "global-only" in str(opts.client.last_response.body), \
                f"rejection must explain why: {opts.client.last_response.body}"
        finally:
            grp.delete()
    finally:
        _cleanup_strict_setting()
        opts.client.logout()


@th.django_unit_test("strict: group posture flip invalidates cached decisions")
def test_group_strict_cache_invalidation(opts):
    from mojo.apps.account.services.geofence import cache as gf_cache
    _admin_login(opts)
    grp = _make_group("GF StrictCacheInv", {"geofence": dict(US_RULE)})
    try:
        # Prime a cached private-ip ALLOW under the group-scoped key (rules
        # present so the no-rules fast path doesn't skip the cache).
        resp = _geo_check(opts, group=grp, geo=GEO_PRIVATE, cache_ttl=300)
        d = resp.response.data
        assert d.allowed is True and d.reason == "private_ip", \
            f"default posture must allow private IP, got {dict(d)}"

        # Sanity: decision now comes from cache (geo header says US but the
        # cached private_ip allow is returned).
        resp = _geo_check(opts, group=grp, geo=GEO_RU, cache_ttl=300)
        assert resp.response.data.reason == "private_ip", \
            "sanity: decision must come from cache before invalidation"

        # Flip the group strict override via REST — must invalidate the cache.
        resp = opts.client.post(
            f"/api/group/{grp.pk}", {"metadata": {"geofence_strict": True}})
        assert resp.status_code == 200, f"override write failed: {resp.status_code}"

        resp = _geo_check(opts, group=grp, geo=GEO_PRIVATE, cache_ttl=300)
        d = resp.response.data
        assert d.allowed is False and d.reason == "private_ip", \
            f"stale cached allow must be gone; strict must deny private IP, got {dict(d)}"
    finally:
        gf_cache.invalidate_group(grp.pk)
        grp.delete()
        opts.client.logout()


@th.django_unit_test("strict: geo/rules exposes global + per-group posture")
def test_geo_rules_posture_fields(opts):
    _admin_login(opts)
    strict_grp = _make_group("GF StrictRules", {"geofence_strict": True})
    plain_grp = _make_group("GF PlainRules")
    try:
        resp = opts.client.get("/api/geo/rules")
        assert resp.status_code == 200, f"GET geo/rules got {resp.status_code}"
        assert resp.response.data.posture.strict_posture is False, \
            "global strict_posture must default false"

        resp = opts.client.get(f"/api/geo/rules?group_uuid={strict_grp.uuid}")
        g = resp.response.data.group
        assert g.strict_posture is True, f"override must surface raw, got {dict(g)}"
        assert g.strict_posture_effective is True, \
            f"effective posture must resolve true, got {dict(g)}"

        resp = opts.client.get(f"/api/geo/rules?group_uuid={plain_grp.uuid}")
        g = resp.response.data.group
        assert g.get("strict_posture") is None, \
            f"no override must surface null (inherit), got {dict(g)}"
        assert g.strict_posture_effective is False, \
            f"effective posture must inherit the global false, got {dict(g)}"
    finally:
        opts.client.logout()
        strict_grp.delete()
        plain_grp.delete()


@th.django_unit_test("strict: simulate honors strict posture")
def test_simulate_strict(opts):
    _admin_login(opts)
    try:
        resp = opts.client.post("/api/geo/simulate", {"geo": dict(GEO_US)},
                                headers=headers(strict=True, system_rules={}))
        assert resp.status_code == 200, f"simulate failed: {opts.client.last_response.body}"
        d = resp.response.data
        assert d.allowed is False and d.reason == "no_rules_strict", \
            f"simulate must apply strict no-rules deny, got {dict(d)}"
        assert d.strict_posture is True, "simulate decision must carry strict_posture"
    finally:
        opts.client.logout()
