"""Member visibility (ITEM-022) — group-scoped geofence policy + events.

GET /api/geo/policy is the ONE member-scoped geofence read: a member grant
(view_security/security) on the requested group returns a deliberately narrow
payload (baseline + that group's rule + strict posture) — never the config
plane's operational detail. Events: a member's view_security grant already
scopes /api/incident/event to their own group via the framework fallback;
these tests lock that for the geofence_* categories.

Parallel-safety: this module never calls geo/check (no decision-cache writes),
never persists geofence Settings, and never touches 127.0.0.1 allowlists.
Assertions avoid global-state values other modules may legitimately change
mid-run (e.g. GEOFENCE_SYSTEM_RULES content); group-owned state is asserted
exactly.
"""
import uuid as _uuid
from testit import helpers as th

GROUP_A = "gfmv-grp-a"
GROUP_B = "gfmv-grp-b"
GROUP_A_RULE = {"country": {"in": ["US"]}}
EVENT_MARK = "gfmv-member-visibility"


def _login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


def _make_user(opts, label, suffix):
    from mojo.apps.account.models import User
    email = f"gfmv_{label}_{suffix}@geofence.test"
    password = f"Gfmv##{label}99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    setattr(opts, f"{label}_email", email)
    setattr(opts, f"{label}_password", password)
    return user


@th.django_unit_setup()
def setup_member_visibility(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps.incident.models import Event

    # Long-lived DB: remove everything a previous run created BEFORE creating.
    # Deleting the groups SET_NULLs their events' group FK, so match this
    # module's leftovers by the details marker, not by group.
    Event.objects.filter(category="geofence_block", details__startswith=EVENT_MARK).delete()
    Group.objects.filter(name__in=[GROUP_A, GROUP_B]).delete()

    group_a = Group(name=GROUP_A, kind="default")
    group_a.metadata = {"geofence": dict(GROUP_A_RULE)}
    group_a.save()
    group_b = Group(name=GROUP_B, kind="default")
    group_b.save()
    # uuid is lazily assigned — materialize before driving group_uuid params.
    opts.group_a_uuid = group_a.get_uuid()
    opts.group_b_uuid = group_b.get_uuid()
    opts.group_a_id = group_a.pk
    opts.group_b_id = group_b.pk

    suffix = _uuid.uuid4().hex[:8]
    member_a = _make_user(opts, "member_a", suffix)      # view_security in A only
    member_b = _make_user(opts, "member_b", suffix)      # view_security in B only
    plain_a = _make_user(opts, "plain_a", suffix)        # member of A, no grant
    global_viewer = _make_user(opts, "global_viewer", suffix)  # global view_security
    geo_viewer = _make_user(opts, "geo_viewer", suffix)  # global view_geofence only

    ms_a = GroupMember(user=member_a, group=group_a)
    ms_a.save()
    ms_a.add_permission("view_security")
    ms_b = GroupMember(user=member_b, group=group_b)
    ms_b.save()
    ms_b.add_permission("view_security")
    GroupMember(user=plain_a, group=group_a).save()
    global_viewer.add_permission("view_security")
    global_viewer.save()
    geo_viewer.add_permission("view_geofence")
    geo_viewer.save()

    # Seed geofence_block events: 2 in A, 3 in B, 1 groupless (invisible to members).
    for _ in range(2):
        Event.objects.create(category="geofence_block", source_ip="203.0.113.10",
                             group=group_a, details=f"{EVENT_MARK} group-a")
    for _ in range(3):
        Event.objects.create(category="geofence_block", source_ip="203.0.113.11",
                             group=group_b, details=f"{EVENT_MARK} group-b")
    groupless = Event.objects.create(category="geofence_block", source_ip="203.0.113.12",
                                     details=f"{EVENT_MARK} groupless")
    opts.groupless_event_id = groupless.pk


@th.django_unit_test("member: geo/policy returns own group's narrow payload")
def test_policy_member_own_group(opts):
    _login(opts, opts.member_a_email, opts.member_a_password)
    resp = opts.client.get("/api/geo/policy", params={"group_uuid": opts.group_a_uuid})
    assert resp.status_code == 200, \
        f"member view_security grant must read own group's policy, got {resp.status_code}: {resp.body}"
    d = resp.response.data
    assert d.group.id == opts.group_a_id, f"payload must be the requested group, got {dict(d.group)}"
    assert dict(d.group_rule) == GROUP_A_RULE, \
        f"group_rule must be the group's own rule, got {dict(d.group_rule)}"
    assert isinstance(dict(d.system_rule), dict), \
        f"system_rule (baseline) must be present as a dict, got {d.system_rule!r}"
    assert list(d.evaluation_order) == ["system", "group"], \
        f"evaluation_order wrong: {d.evaluation_order}"
    assert "enabled" in d, "payload must carry the enabled flag"
    assert d.strict_posture is None, \
        f"group A sets no strict override; tri-state must be null, got {d.strict_posture!r}"
    assert d.strict_posture_effective is False, (
        "strict_posture_effective must resolve to the global default False — "
        f"got {d.strict_posture_effective!r} (a persisted global strict Setting row "
        "in tests violates the ITEM-021 hygiene rule)"
    )
    # The anti-leak contract: platform operational detail must never appear.
    for forbidden in ("enforced_endpoints", "allowlist_summary", "cache_ttl",
                      "fail_closed_scopes", "posture", "source", "modified"):
        assert forbidden not in d, \
            f"member payload must never include {forbidden!r} (config-plane detail): {list(d.keys())}"


@th.django_unit_test("member: geo/policy denies another group's policy (both directions)")
def test_policy_cross_tenant_denied(opts):
    _login(opts, opts.member_a_email, opts.member_a_password)
    resp = opts.client.get("/api/geo/policy", params={"group_uuid": opts.group_b_uuid})
    assert resp.status_code == 403, \
        f"A-member must not read B's policy, got {resp.status_code}: {resp.body}"
    _login(opts, opts.member_b_email, opts.member_b_password)
    resp = opts.client.get("/api/geo/policy", params={"group_uuid": opts.group_a_uuid})
    assert resp.status_code == 403, \
        f"B-member must not read A's policy, got {resp.status_code}: {resp.body}"
    # Anti-enumeration: an unknown group_uuid must look exactly like a
    # known-but-unauthorized one — same default 403, no existence oracle.
    resp = opts.client.get("/api/geo/policy", params={"group_uuid": _uuid.uuid4().hex})
    assert resp.status_code == 403, \
        f"unknown group_uuid must 403 identically to an unauthorized one, got {resp.status_code}: {resp.body}"


@th.django_unit_test("member: geo/policy denies members without the grant and geofence-only globals")
def test_policy_no_grant_denied(opts):
    # Member of A with no member permissions at all.
    _login(opts, opts.plain_a_email, opts.plain_a_password)
    resp = opts.client.get("/api/geo/policy", params={"group_uuid": opts.group_a_uuid})
    assert resp.status_code == 403, \
        f"membership without view_security must 403, got {resp.status_code}: {resp.body}"
    # Member with a grant but no group param: no global perm + no request.group -> 403.
    _login(opts, opts.member_a_email, opts.member_a_password)
    resp = opts.client.get("/api/geo/policy")
    assert resp.status_code == 403, \
        f"member without a group param must 403 at the decorator, got {resp.status_code}"
    # Global view_geofence is a config-plane key, deliberately NOT accepted here.
    _login(opts, opts.geo_viewer_email, opts.geo_viewer_password)
    resp = opts.client.get("/api/geo/policy", params={"group_uuid": opts.group_a_uuid})
    assert resp.status_code == 403, \
        f"global view_geofence must not open geo/policy (use geo/rules), got {resp.status_code}"


@th.django_unit_test("member: geo/policy requires a group param for global holders (400)")
def test_policy_group_required(opts):
    _login(opts, opts.global_viewer_email, opts.global_viewer_password)
    resp = opts.client.get("/api/geo/policy")
    assert resp.status_code == 400, \
        f"global holder without a group param must get 400 'group required', got {resp.status_code}: {resp.body}"


@th.django_unit_test("member: global view_security reads any group via geo/policy, same narrow shape")
def test_policy_global_grant_ok(opts):
    _login(opts, opts.global_viewer_email, opts.global_viewer_password)
    for uuid, gid in ((opts.group_a_uuid, opts.group_a_id),
                      (opts.group_b_uuid, opts.group_b_id)):
        resp = opts.client.get("/api/geo/policy", params={"group_uuid": uuid})
        assert resp.status_code == 200, \
            f"global view_security must read group {gid} policy, got {resp.status_code}: {resp.body}"
        d = resp.response.data
        assert d.group.id == gid, f"payload group mismatch: {dict(d.group)}"
        assert "enforced_endpoints" not in d, \
            "narrow payload applies to global holders too — no config-plane detail"


@th.django_unit_test("member: the config plane stays global-only (member grant gets 403 on geo/rules)")
def test_config_plane_still_global_only(opts):
    _login(opts, opts.member_a_email, opts.member_a_password)
    resp = opts.client.get("/api/geo/rules", params={"group_uuid": opts.group_a_uuid})
    assert resp.status_code == 403, \
        f"member view_security must NOT open the config plane, got {resp.status_code}: {resp.body}"


@th.django_unit_test("member: incident event feed is scoped to the member's own group")
def test_events_member_scoped(opts):
    # Member sees exactly their group's geofence_block events.
    _login(opts, opts.member_a_email, opts.member_a_password)
    resp = opts.client.get("/api/incident/event",
                           params={"_mode": "count", "category": "geofence_block"})
    assert resp.status_code == 200, \
        f"member event count failed: {resp.status_code}: {resp.body}"
    assert resp.response["count"] == 2, (
        f"A-member must count exactly group A's 2 geofence_block events "
        f"(not B's or groupless), got {resp.response['count']}"
    )
    resp = opts.client.get("/api/incident/event", params={"category": "geofence_block"})
    assert resp.status_code == 200, f"member event list failed: {resp.status_code}"
    rows = resp.response.data
    ids = [r["id"] for r in rows]
    assert opts.groupless_event_id not in ids, \
        "groupless events must be invisible to a member grant"
    for r in rows:
        assert r.get("group_id") == opts.group_a_id, \
            f"member list leaked a row outside group A: {dict(r)}"

    # Global grant, narrowed by the group param: exact count for group A.
    _login(opts, opts.global_viewer_email, opts.global_viewer_password)
    resp = opts.client.get(
        "/api/incident/event",
        params={"_mode": "count", "category": "geofence_block", "group": opts.group_a_id})
    assert resp.status_code == 200, \
        f"global event count failed: {resp.status_code}: {resp.body}"
    assert resp.response["count"] == 2, \
        f"global grant filtered to group A must count 2, got {resp.response['count']}"

    # No grant anywhere: nothing (0 rows or an explicit deny).
    _login(opts, opts.plain_a_email, opts.plain_a_password)
    resp = opts.client.get("/api/incident/event",
                           params={"_mode": "count", "category": "geofence_block"})
    if resp.status_code == 200:
        assert resp.response["count"] == 0, \
            f"no-grant user must not count events, got {resp.response['count']}"
    else:
        assert resp.status_code in (401, 403), \
            f"expected 401/403 for no-grant count, got {resp.status_code}: {resp.body}"
