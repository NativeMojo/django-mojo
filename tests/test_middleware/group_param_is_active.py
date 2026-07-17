"""
Regression tests for ITEM-025 — a client-supplied numeric `group=<id>` must
resolve `request.group` for ACTIVE groups only, at every resolution site.

Before the fix, the dispatcher's numeric branch (mojo/decorators/http.py) and
the requires_perms/requires_group_perms fallbacks (mojo/decorators/auth.py)
resolved groups without an `is_active` filter — an anonymous caller could make
an inactive (deactivated) group the request group by integer id: it got
`touch()`ed (a full save — `last_activity` AND `modified` bump: an existence
oracle), its geofence rules participated in decisions, and member-scoped
grants in a deactivated tenant still authorized `requires_perms` endpoints.
The sibling `group_uuid` branch already filtered `is_active=True` with a
security comment stating exactly this contract.

After the fix an inactive id behaves identically to a nonexistent one:
`request.group` stays None, nothing is written, no error distinguishes the two.
"""
from testit import helpers as th
from objict import objict


SELF_USERNAME = "item025_self@test.com"
SELF_PASSWORD = "item025_self_pw_99"
MEMBER_USERNAME = "item025_member@test.com"
MEMBER_PASSWORD = "item025_member_pw_99"
ACTIVE_GROUP = "item025-active-group"
INACTIVE_GROUP = "item025-inactive-group"
# DM-048: an ACTIVE child under a DEACTIVATED parent is effectively inactive —
# the dispatcher must not resolve (or touch) it either.
INACTIVE_PARENT = "dm048-mw-inactive-parent"
ACTIVE_CHILD = "dm048-mw-active-child"


def _login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


@th.django_unit_setup()
def setup_group_param_is_active(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    User.objects.filter(email__in=[SELF_USERNAME, MEMBER_USERNAME]).delete()
    Group.objects.filter(name__in=[
        ACTIVE_GROUP, INACTIVE_GROUP, INACTIVE_PARENT, ACTIVE_CHILD]).delete()

    self_user = User.objects.create_user(
        username=SELF_USERNAME, email=SELF_USERNAME, password=SELF_PASSWORD)
    self_user.is_active = True
    self_user.is_email_verified = True
    self_user.requires_mfa = False
    self_user.save()
    opts.self_user_id = self_user.pk

    member_user = User.objects.create_user(
        username=MEMBER_USERNAME, email=MEMBER_USERNAME, password=MEMBER_PASSWORD)
    member_user.is_active = True
    member_user.is_email_verified = True
    member_user.requires_mfa = False
    member_user.save()
    opts.member_user_id = member_user.pk

    # last_activity=None defeats Group.touch()'s 300s throttle: the very first
    # resolution of either group would write (last_activity AND modified).
    active_group = Group.objects.create(name=ACTIVE_GROUP, kind="organization")
    active_group.last_activity = None
    active_group.save()
    opts.active_group_id = active_group.pk

    inactive_group = Group.objects.create(name=INACTIVE_GROUP, kind="organization")
    inactive_group.is_active = False
    inactive_group.last_activity = None
    inactive_group.save()
    opts.inactive_group_id = inactive_group.pk

    # Member-level grants only (no global perms): view_security in BOTH groups
    # for the geo/policy fallback tests; an arbitrary member perm in the
    # inactive group for the in-process requires_group_perms test.
    ms_active = GroupMember(user=member_user, group=active_group)
    ms_active.save()
    ms_active.add_permission("view_security")
    ms_inactive = GroupMember(user=member_user, group=inactive_group)
    ms_inactive.save()
    ms_inactive.add_permission("view_security")
    ms_inactive.add_permission("item025_member_perm")

    # DM-048: deactivated parent -> active child (last_activity=None on the
    # child so any dispatcher touch() would be visible).
    inactive_parent = Group.objects.create(name=INACTIVE_PARENT, kind="organization")
    inactive_parent.is_active = False
    inactive_parent.last_activity = None
    inactive_parent.save()
    active_child = Group.objects.create(
        name=ACTIVE_CHILD, kind="team", parent=inactive_parent)
    active_child.last_activity = None
    active_child.save()
    opts.active_child_id = active_child.pk


@th.django_unit_test("dispatcher: numeric group= with an INACTIVE id resolves nothing — no touch, no modified bump (THE regression)")
def test_inactive_group_id_not_resolved_not_touched(opts):
    from mojo.apps.account.models import Group

    before = Group.objects.get(pk=opts.inactive_group_id)
    assert before.last_activity is None, \
        f"fixture must start untouched (last_activity None), got {before.last_activity!r}"
    modified_before = before.modified

    _login(opts, SELF_USERNAME, SELF_PASSWORD)
    resp = opts.client.post(
        f"/api/user/{opts.self_user_id}?group={opts.inactive_group_id}",
        {"display_name": "Item025 Inactive"},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"an inactive group id must behave like a nonexistent one (request proceeds), got {resp.status_code}: {opts.client.last_response.body}"
    after = Group.objects.get(pk=opts.inactive_group_id)
    assert after.last_activity is None, \
        f"an inactive group must NOT be touch()ed by numeric group= resolution, but last_activity was set: {after.last_activity!r}"
    assert after.modified == modified_before, \
        f"an inactive group's modified must not bump (existence oracle): {modified_before!r} -> {after.modified!r}"


@th.django_unit_test("dispatcher: numeric group= with an ACTIVE id still resolves and touches (no regression)")
def test_active_group_id_still_resolves_and_touches(opts):
    from mojo.apps.account.models import Group

    before = Group.objects.get(pk=opts.active_group_id)
    assert before.last_activity is None, \
        f"fixture must start untouched (last_activity None), got {before.last_activity!r}"

    _login(opts, SELF_USERNAME, SELF_PASSWORD)
    resp = opts.client.post(
        f"/api/user/{opts.self_user_id}?group={opts.active_group_id}",
        {"display_name": "Item025 Active"},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"an active group id must keep resolving normally, got {resp.status_code}: {opts.client.last_response.body}"
    after = Group.objects.get(pk=opts.active_group_id)
    assert after.last_activity is not None, \
        "an active group must still be touch()ed on resolution (activity tracking unchanged)"


@th.django_unit_test("dispatcher: a nonexistent group id is silently no-group — the oracle-equivalence control")
def test_nonexistent_group_id_silently_ignored(opts):
    _login(opts, SELF_USERNAME, SELF_PASSWORD)
    resp = opts.client.post(
        f"/api/user/{opts.self_user_id}?group=999999999",
        {"display_name": "Item025 Nonexistent"},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"a nonexistent group id resolves to no group context, not an error, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("DM-048 dispatcher: numeric group= with an ACTIVE child of a DEACTIVATED parent resolves nothing — no touch")
def test_child_of_inactive_parent_not_resolved_not_touched(opts):
    from mojo.apps.account.models import Group

    before = Group.objects.get(pk=opts.active_child_id)
    assert before.last_activity is None, \
        f"fixture must start untouched (last_activity None), got {before.last_activity!r}"
    modified_before = before.modified

    _login(opts, SELF_USERNAME, SELF_PASSWORD)
    resp = opts.client.post(
        f"/api/user/{opts.self_user_id}?group={opts.active_child_id}",
        {"display_name": "DM048 Child"},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"a child of a deactivated parent must behave like a nonexistent id (request proceeds), got {resp.status_code}: {opts.client.last_response.body}"
    after = Group.objects.get(pk=opts.active_child_id)
    assert after.last_activity is None, \
        f"an effectively-inactive child must NOT be touch()ed by numeric group= resolution, but last_activity was set: {after.last_activity!r}"
    assert after.modified == modified_before, \
        f"an effectively-inactive child's modified must not bump (existence oracle): {modified_before!r} -> {after.modified!r}"


@th.django_unit_test("requires_perms fallback: a member grant in an INACTIVE group must not authorize (403), an ACTIVE one must (200)")
def test_member_grant_in_inactive_group_denied(opts):
    _login(opts, MEMBER_USERNAME, MEMBER_PASSWORD)

    # Active control first: the numeric group= member fallback keeps working.
    resp = opts.client.get("/api/geo/policy", params={"group": opts.active_group_id})
    assert resp.status_code == 200, \
        f"member view_security grant in an ACTIVE group must authorize via numeric group=, got {resp.status_code}: {resp.body}"
    assert resp.response.data.group.id == opts.active_group_id, \
        f"policy payload must be the requested group, got {dict(resp.response.data.group)}"

    # The leak: pre-fix the dispatcher resolved the inactive group and the
    # member grant authorized a deactivated tenant's policy read (200).
    resp = opts.client.get("/api/geo/policy", params={"group": opts.inactive_group_id})
    opts.client.logout()
    assert resp.status_code == 403, \
        f"a member grant in an INACTIVE group must not authorize (inactive never resolves), got {resp.status_code}: {resp.body}"


@th.django_unit_test("requires_group_perms: an inactive group id in the fallback fails closed to PermissionDenied")
def test_requires_group_perms_inactive_group_fails_closed(opts):
    import mojo.errors
    from mojo.apps.account.models import User
    from mojo.decorators.auth import requires_group_perms

    @requires_group_perms("item025_member_perm")
    def dummy_view(request):
        return "must never be reached"

    # Real user with a real member grant of this perm in the INACTIVE group —
    # pre-fix the fallback resolves the inactive group, the grant authorizes,
    # and the view executes; post-fix the group never resolves -> deny.
    user = User.objects.get(pk=opts.member_user_id)
    req = objict(user=user, DATA=objict(group=str(opts.inactive_group_id)), group=None)

    try:
        result = dummy_view(req)
        assert False, \
            f"a member grant in an inactive group must not authorize, but the view ran and returned {result!r}"
    except mojo.errors.PermissionDeniedException:
        pass  # fail-closed deny is the correct outcome
