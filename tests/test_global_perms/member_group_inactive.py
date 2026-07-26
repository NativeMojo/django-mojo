"""Maestro item 56 — a member grant in a deactivated (effectively-inactive)
group must not authorize anything.

DM-048 already closed three of the item's five surfaces by gating
`Group.get_member_for_user()` on `is_effectively_active()` (metrics
group-account gate, WS group:<id> subscribe, and the RestMeta detail
instance re-bind USER branch all funnel through it) — those are PINNED here.

The remaining hole (THE regression): the member-side group derivations —
`User.get_groups()`, `User.get_group_ids()`, `User.get_groups_with_permission()`
— filtered only GroupMember.is_active, never group activeness, so the RestMeta
LIST fallback (mojo/models/rest.py on_rest_handle_list) and
Group.on_rest_handle_list still authorized rows from deactivated tenants.

Fixture shape mirrors tests/test_middleware/group_param_is_active.py (DM-025);
file style mirrors tests/test_global_perms/apikey_group_inactive.py.
"""
from testit import helpers as th

MEMBER_USERNAME = "m56_member@example.com"
MEMBER_PASSWORD = "m56!Member#99"

ACTIVE_GROUP = "m56-active"
INACTIVE_GROUP = "m56-inactive"
INACTIVE_PARENT = "m56-inactive-parent"
ACTIVE_CHILD = "m56-active-child"
ALL_GROUPS = [ACTIVE_GROUP, INACTIVE_GROUP, INACTIVE_PARENT, ACTIVE_CHILD]


def _login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(MEMBER_USERNAME, MEMBER_PASSWORD)
    assert ok, f"login failed for {MEMBER_USERNAME}: {opts.client.last_response.body}"


def _group_id_of(row):
    """Extract the group id from a serialized GroupMember row (int or graph dict)."""
    group = row.get("group")
    if isinstance(group, dict):
        return group.get("id")
    return group


@th.django_unit_setup()
def setup_member_group_inactive(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    # Long-lived DB: clean before creating.
    User.objects.filter(email=MEMBER_USERNAME).delete()
    Group.objects.filter(name__in=ALL_GROUPS).delete()

    member = User.objects.create_user(
        username=MEMBER_USERNAME, email=MEMBER_USERNAME, password=MEMBER_PASSWORD)
    member.is_active = True
    member.is_email_verified = True
    member.requires_mfa = False
    member.save()
    opts.member_id = member.pk

    active_group = Group.objects.create(name=ACTIVE_GROUP, kind="organization")
    opts.active_group_id = active_group.pk

    inactive_group = Group.objects.create(name=INACTIVE_GROUP, kind="organization")
    inactive_group.is_active = False
    inactive_group.save()
    opts.inactive_group_id = inactive_group.pk

    inactive_parent = Group.objects.create(name=INACTIVE_PARENT, kind="organization")
    inactive_parent.is_active = False
    inactive_parent.save()
    active_child = Group.objects.create(
        name=ACTIVE_CHILD, kind="team", parent=inactive_parent)
    opts.active_child_id = active_child.pk

    # Member-level grants ONLY (no system perms): view_members feeds the
    # GroupMember VIEW_PERMS list fallback; view_metrics feeds the metrics
    # group-account gate.
    for group in (active_group, inactive_group, active_child):
        ms = GroupMember(user=member, group=group)
        ms.save()
        ms.add_permission("view_members")
        ms.add_permission("view_metrics")
        if group.pk == inactive_group.pk:
            opts.inactive_member_row_id = ms.pk
        if group.pk == active_group.pk:
            opts.active_member_row_id = ms.pk


# ---------------------------------------------------------------------------
# Regression: the member-side derivations (fail pre-fix)
# ---------------------------------------------------------------------------

@th.django_unit_test("get_groups excludes an inactive group (both include_children modes)")
def test_get_groups_excludes_inactive_group(opts):
    from mojo.apps.account.models import User

    member = User.objects.get(pk=opts.member_id)
    for include_children in (True, False):
        ids = set(g.id for g in member.get_groups(include_children=include_children))
        assert opts.active_group_id in ids, \
            f"active-group membership must survive (include_children={include_children}), got {ids}"
        assert opts.inactive_group_id not in ids, \
            f"an inactive group must not appear in get_groups (include_children={include_children}), got {ids}"


@th.django_unit_test("get_groups excludes an active child under a deactivated parent (DM-048 subtree)")
def test_get_groups_excludes_child_under_inactive_parent(opts):
    from mojo.apps.account.models import User

    member = User.objects.get(pk=opts.member_id)
    ids = set(g.id for g in member.get_groups())
    assert opts.active_child_id not in ids, \
        f"a direct membership in an active child of a deactivated parent must not resolve, got {ids}"


@th.django_unit_test("get_group_ids excludes inactive groups (both include_children modes)")
def test_get_group_ids_excludes_inactive(opts):
    from mojo.apps.account.models import User

    member = User.objects.get(pk=opts.member_id)
    for include_children in (True, False):
        ids = set(member.get_group_ids(include_children=include_children))
        assert opts.active_group_id in ids, \
            f"active-group id must survive (include_children={include_children}), got {ids}"
        assert opts.inactive_group_id not in ids, \
            f"an inactive group id must not appear (include_children={include_children}), got {ids}"
        assert opts.active_child_id not in ids, \
            f"an ancestor-darkened child id must not appear (include_children={include_children}), got {ids}"


@th.django_unit_test("get_groups_with_permission excludes inactive groups, keeps active grants")
def test_get_groups_with_permission_excludes_inactive(opts):
    from mojo.apps.account.models import User

    member = User.objects.get(pk=opts.member_id)
    qs = member.get_groups_with_permission(["view_members"])
    ids = set(qs.values_list("id", flat=True))
    assert opts.active_group_id in ids, \
        f"a grant in an ACTIVE group must keep resolving, got {ids}"
    assert opts.inactive_group_id not in ids, \
        f"a grant in an INACTIVE group must not resolve, got {ids}"
    assert opts.active_child_id not in ids, \
        f"a grant in an ancestor-darkened child must not resolve, got {ids}"


@th.django_unit_test("get_groups is_active=None keeps the raw introspection behavior")
def test_get_groups_is_active_none_keeps_raw(opts):
    from mojo.apps.account.models import User

    member = User.objects.get(pk=opts.member_id)
    ids = set(g.id for g in member.get_groups(is_active=None))
    assert opts.inactive_group_id in ids, \
        f"is_active=None is the admin/introspection escape hatch — inactive groups stay visible, got {ids}"


@th.django_unit_test("RestMeta list fallback: no rows from a deactivated tenant (THE item repro)")
def test_list_fallback_inactive_group_denied(opts):
    _login(opts)
    # No group= param — the fallback derives permitted groups from member
    # grants alone (request.group is None, no narrowing afterwards).
    resp = opts.client.get("/api/group/member", params={"size": 200})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"member with active-group grants should still list, got {resp.status_code}: {resp.body}"
    rows = resp.response.data
    group_ids = set(_group_id_of(dict(r)) for r in rows)
    assert opts.active_group_id in group_ids, \
        f"the ACTIVE group's member rows must still return, got groups {group_ids}"
    assert opts.inactive_group_id not in group_ids, \
        f"a deactivated tenant's member rows leaked through the list fallback, got groups {group_ids}"
    assert opts.active_child_id not in group_ids, \
        f"an ancestor-darkened child's member rows leaked through the list fallback, got groups {group_ids}"


# ---------------------------------------------------------------------------
# Pinning: surfaces DM-048 already closed (must pass before AND after)
# ---------------------------------------------------------------------------

@th.django_unit_test("detail re-bind: GET by pk on an inactive group's row is denied for a member grant")
def test_detail_rebind_inactive_group_denied(opts):
    _login(opts)
    resp = opts.client.get(f"/api/group/member/{opts.inactive_member_row_id}")
    denied_status = resp.status_code
    # Active control: same member, same perm, active group -> allowed.
    resp_active = opts.client.get(f"/api/group/member/{opts.active_member_row_id}")
    opts.client.logout()

    assert denied_status == 403, \
        f"detail GET by pk on a deactivated tenant's row must fail closed (DM-048 pin), got {denied_status}"
    assert resp_active.status_code == 200, \
        f"detail GET on the ACTIVE group's row must keep working, got {resp_active.status_code}: {resp_active.body}"


@th.django_unit_test("WS subscribe: group:<id> topic denied for an inactive group, allowed for an active one")
def test_ws_subscribe_inactive_group_denied(opts):
    from mojo.apps.account.models import User

    member = User.objects.get(pk=opts.member_id)
    assert member.on_realtime_can_subscribe(f"group:{opts.active_group_id}") is True, \
        "membership in an ACTIVE group must allow subscribing to its topic"
    assert member.on_realtime_can_subscribe(f"group:{opts.inactive_group_id}") is False, \
        "membership in an INACTIVE group must not allow subscribing to its topic (DM-048 pin)"
    assert member.on_realtime_can_subscribe(f"group:{opts.active_child_id}") is False, \
        "an ancestor-darkened child's topic must not be subscribable (DM-048 pin)"


@th.django_unit_test("metrics gate: group-<id> account denied for an inactive group, allowed for an active one")
def test_metrics_gate_inactive_group_denied(opts):
    import mojo.errors
    from mojo.apps.account.models import User
    from mojo.apps.metrics.rest import helpers as metrics_helpers
    from testit.helpers import get_mock_request

    member = User.objects.get(pk=opts.member_id)

    # Active control: the member view_metrics grant authorizes the account.
    req = get_mock_request(user=member)
    metrics_helpers.check_view_permissions(req, account=f"group-{opts.active_group_id}")

    # Inactive: same grant, deactivated tenant -> the one PermissionDenied
    # shape (anti-oracle: same denial as nonexistent/unauthorized).
    try:
        metrics_helpers.check_view_permissions(req, account=f"group-{opts.inactive_group_id}")
        assert False, \
            "a member view_metrics grant in a deactivated group must not authorize its metrics account (DM-048 pin)"
    except mojo.errors.PermissionDeniedException:
        pass  # fail-closed deny is the correct outcome
