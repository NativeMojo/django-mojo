"""
Regression tests for DM-039 — GET /api/group/<pk>/member must fail closed with
ONE indistinguishable 403 for every caller who is not an active member of an
ACTIVE group.

Before the fix the handler resolved ANY group (`filter(pk=pk).last()`, no
is_active filter), unconditionally `touch()`ed it (a write to deactivated
groups from non-member requests), and split its deny responses: nonexistent
pk -> 403, existing group where the caller is not a member -> 200 with the
`{"id": -1, "permissions": []}` sentinel. The 403-vs-200 split let any
authenticated user probe arbitrary pks and learn which groups exist
(existence oracle).

After the fix: nonexistent pk, inactive group pk, and active-group-non-member
all return the identical PermissionDeniedException response, and nothing is
written until membership in an active group is confirmed. Member self-lookup
on ACTIVE groups is unchanged.

Extended for DM-048 — the subtree contract: a group is EFFECTIVELY active only
if it and every ancestor is active. A deactivated parent darkens its whole
subtree dynamically (no flag cascade): a membership in the deactivated parent
no longer authorizes on an active child, a membership in an active grandparent
above a deactivated middle no longer authorizes, and even a DIRECT member of
the active child is denied. Reactivating the parent instantly restores all of
it (no one-way door).
"""
from testit import helpers as th


MEMBER_USERNAME = "dm039_member@test.com"
MEMBER_PASSWORD = "dm039_member_pw_99"
OUTSIDER_USERNAME = "dm039_outsider@test.com"
OUTSIDER_PASSWORD = "dm039_outsider_pw_99"
ACTIVE_GROUP = "dm039-active-group"
INACTIVE_GROUP = "dm039-inactive-group"
NONEXISTENT_PK = 999999999

# DM-048 fixtures — two disjoint chains:
#   inactive parent P  -> active child C      (P member + C direct member denied)
#   active grandparent -> inactive middle -> active leaf   (GP member denied)
CHAIN_USERNAME = "dm048_chain_member@test.com"
CHAIN_PASSWORD = "dm048_chain_pw_99"
DIRECT_USERNAME = "dm048_direct_member@test.com"
DIRECT_PASSWORD = "dm048_direct_pw_99"
INACTIVE_PARENT = "dm048-inactive-parent"
ACTIVE_CHILD = "dm048-active-child"
ACTIVE_GRANDPARENT = "dm048-active-grandparent"
INACTIVE_MIDDLE = "dm048-inactive-middle"
ACTIVE_LEAF = "dm048-active-leaf"


def _login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


def _deny_shape(resp):
    """The distinguishable parts of a response: wire status + parsed body
    (error text, code, status flag, data payload). Two deny responses are
    indistinguishable iff these tuples are equal."""
    body = resp.response
    return (
        resp.status_code,
        body.get("error"),
        body.get("code"),
        body.get("status"),
        body.get("data"),
    )


@th.django_unit_setup()
def setup_group_me_member_oracle(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    User.objects.filter(email__in=[
        MEMBER_USERNAME, OUTSIDER_USERNAME, CHAIN_USERNAME, DIRECT_USERNAME]).delete()
    Group.objects.filter(name__in=[
        ACTIVE_GROUP, INACTIVE_GROUP, INACTIVE_PARENT, ACTIVE_CHILD,
        ACTIVE_GRANDPARENT, INACTIVE_MIDDLE, ACTIVE_LEAF]).delete()

    member_user = User.objects.create_user(
        username=MEMBER_USERNAME, email=MEMBER_USERNAME, password=MEMBER_PASSWORD)
    member_user.is_active = True
    member_user.is_email_verified = True
    member_user.requires_mfa = False
    member_user.save()
    opts.member_user_id = member_user.pk

    outsider = User.objects.create_user(
        username=OUTSIDER_USERNAME, email=OUTSIDER_USERNAME, password=OUTSIDER_PASSWORD)
    outsider.is_active = True
    outsider.is_email_verified = True
    outsider.requires_mfa = False
    outsider.save()
    opts.outsider_user_id = outsider.pk

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

    # The member user belongs to BOTH groups — the inactive membership proves
    # inactive == nonexistent applies to members too.
    ms_active = GroupMember(user=member_user, group=active_group)
    ms_active.save()
    ms_active.add_permission("view_content")
    ms_inactive = GroupMember(user=member_user, group=inactive_group)
    ms_inactive.save()

    # Snapshots for the no-probe-writes assertions (taken before any request).
    opts.active_modified_before = active_group.modified
    opts.inactive_modified_before = inactive_group.modified

    # ---- DM-048 fixtures --------------------------------------------------
    chain_user = User.objects.create_user(
        username=CHAIN_USERNAME, email=CHAIN_USERNAME, password=CHAIN_PASSWORD)
    chain_user.is_active = True
    chain_user.is_email_verified = True
    chain_user.requires_mfa = False
    chain_user.save()
    opts.chain_user_id = chain_user.pk

    direct_user = User.objects.create_user(
        username=DIRECT_USERNAME, email=DIRECT_USERNAME, password=DIRECT_PASSWORD)
    direct_user.is_active = True
    direct_user.is_email_verified = True
    direct_user.requires_mfa = False
    direct_user.save()
    opts.direct_user_id = direct_user.pk

    # Chain 1: deactivated parent P -> active child C.
    parent = Group.objects.create(name=INACTIVE_PARENT, kind="organization")
    parent.is_active = False
    parent.last_activity = None
    parent.save()
    opts.inactive_parent_id = parent.pk

    child = Group.objects.create(name=ACTIVE_CHILD, kind="team", parent=parent)
    child.last_activity = None
    child.save()
    opts.active_child_id = child.pk

    # Chain 2: active grandparent -> deactivated middle -> active leaf.
    grandparent = Group.objects.create(name=ACTIVE_GRANDPARENT, kind="organization")
    grandparent.last_activity = None
    grandparent.save()
    middle = Group.objects.create(name=INACTIVE_MIDDLE, kind="team", parent=grandparent)
    middle.is_active = False
    middle.last_activity = None
    middle.save()
    leaf = Group.objects.create(name=ACTIVE_LEAF, kind="team", parent=middle)
    leaf.last_activity = None
    leaf.save()
    opts.active_leaf_id = leaf.pk

    # chain_user: ACTIVE membership rows in the deactivated parent and the
    # active grandparent — the rows are live, only the group chain is dark.
    ms_chain_parent = GroupMember(user=chain_user, group=parent)
    ms_chain_parent.save()
    opts.chain_parent_member_id = ms_chain_parent.pk
    GroupMember(user=chain_user, group=grandparent).save()
    # direct_user: ACTIVE membership directly in the active child C.
    ms_direct = GroupMember(user=direct_user, group=child)
    ms_direct.save()
    opts.direct_member_id = ms_direct.pk

    opts.child_modified_before = child.modified


@th.django_unit_test("uniform deny: non-member probing nonexistent / inactive / active pks gets byte-identical responses (THE regression)")
def test_non_member_deny_is_indistinguishable(opts):
    _login(opts, OUTSIDER_USERNAME, OUTSIDER_PASSWORD)
    resp_nonexistent = opts.client.get(f"/api/group/{NONEXISTENT_PK}/member")
    resp_inactive = opts.client.get(f"/api/group/{opts.inactive_group_id}/member")
    resp_active = opts.client.get(f"/api/group/{opts.active_group_id}/member")
    opts.client.logout()

    shape_nonexistent = _deny_shape(resp_nonexistent)
    shape_inactive = _deny_shape(resp_inactive)
    shape_active = _deny_shape(resp_active)

    assert shape_inactive == shape_nonexistent, (
        "an INACTIVE group pk must be indistinguishable from a nonexistent one "
        f"(existence oracle): nonexistent={shape_nonexistent!r} vs inactive={shape_inactive!r}")
    assert shape_active == shape_nonexistent, (
        "an ACTIVE group the caller is not a member of must be indistinguishable "
        f"from a nonexistent pk: nonexistent={shape_nonexistent!r} vs active={shape_active!r}")
    assert resp_nonexistent.response.get("code") == 403, (
        "every non-member outcome must be the standard permission-denied response, "
        f"got body: {resp_nonexistent.response!r}")
    assert resp_nonexistent.response.get("status") is False, (
        f"deny responses must carry status=false, got body: {resp_nonexistent.response!r}")


@th.django_unit_test("no probe writes: non-member probes must not touch() either group (last_activity/modified unchanged)")
def test_probes_cause_no_writes(opts):
    from mojo.apps.account.models import Group

    inactive_after = Group.objects.get(pk=opts.inactive_group_id)
    assert inactive_after.last_activity is None, (
        "a non-member probe must NOT touch() an inactive group, but last_activity "
        f"was set: {inactive_after.last_activity!r}")
    assert inactive_after.modified == opts.inactive_modified_before, (
        "an inactive group's modified must not bump from a non-member probe: "
        f"{opts.inactive_modified_before!r} -> {inactive_after.modified!r}")

    active_after = Group.objects.get(pk=opts.active_group_id)
    assert active_after.last_activity is None, (
        "a non-member probe must NOT touch() an active group either (no write "
        f"before membership is confirmed), but last_activity was set: {active_after.last_activity!r}")
    assert active_after.modified == opts.active_modified_before, (
        "an active group's modified must not bump from a non-member probe: "
        f"{opts.active_modified_before!r} -> {active_after.modified!r}")


@th.django_unit_test("member happy path unchanged: member of an ACTIVE group gets their record, legitimate touch preserved")
def test_member_self_lookup_on_active_group(opts):
    from mojo.apps.account.models import Group, GroupMember

    _login(opts, MEMBER_USERNAME, MEMBER_PASSWORD)
    resp = opts.client.get(f"/api/group/{opts.active_group_id}/member")
    opts.client.logout()

    assert resp.status_code == 200, (
        f"a member's self-lookup on an ACTIVE group must succeed, got {resp.status_code}: {resp.body}")
    data = resp.response.data
    assert data.id > 0, (
        f"a member must receive their real membership record (id > 0), got: {dict(data)!r}")
    assert "permissions" in data, (
        f"the membership record must include permissions, got: {dict(data)!r}")

    group_after = Group.objects.get(pk=opts.active_group_id)
    assert group_after.last_activity is not None, (
        "a member self-lookup is legitimate activity — the active group must be touch()ed")
    member_after = GroupMember.objects.get(pk=data.id)
    assert member_after.last_activity is not None, (
        "the membership row must be touch()ed on a successful self-lookup")


@th.django_unit_test("inactive == nonexistent applies to members too: member of an INACTIVE group gets the uniform deny, no touch")
def test_member_of_inactive_group_denied(opts):
    from mojo.apps.account.models import Group

    modified_before = Group.objects.get(pk=opts.inactive_group_id).modified

    _login(opts, MEMBER_USERNAME, MEMBER_PASSWORD)
    resp_inactive = opts.client.get(f"/api/group/{opts.inactive_group_id}/member")
    resp_nonexistent = opts.client.get(f"/api/group/{NONEXISTENT_PK}/member")
    opts.client.logout()

    assert _deny_shape(resp_inactive) == _deny_shape(resp_nonexistent), (
        "even for its own members an INACTIVE group must behave like a nonexistent "
        f"one: nonexistent={_deny_shape(resp_nonexistent)!r} vs inactive={_deny_shape(resp_inactive)!r}")
    inactive_after = Group.objects.get(pk=opts.inactive_group_id)
    assert inactive_after.last_activity is None, (
        "a member lookup on an INACTIVE group must not touch() it, but last_activity "
        f"was set: {inactive_after.last_activity!r}")
    assert inactive_after.modified == modified_before, (
        "an inactive group's modified must not bump from a member lookup: "
        f"{modified_before!r} -> {inactive_after.modified!r}")


@th.django_unit_test("DM-048: membership in a DEACTIVATED parent no longer authorizes on an active child (THE regression), no touch")
def test_parent_membership_denied_when_parent_inactive(opts):
    from mojo.apps.account.models import Group

    _login(opts, CHAIN_USERNAME, CHAIN_PASSWORD)
    resp_child = opts.client.get(f"/api/group/{opts.active_child_id}/member")
    resp_nonexistent = opts.client.get(f"/api/group/{NONEXISTENT_PK}/member")
    opts.client.logout()

    assert _deny_shape(resp_child) == _deny_shape(resp_nonexistent), (
        "an active child of a DEACTIVATED parent must deny the parent's member "
        "exactly like a nonexistent pk: "
        f"nonexistent={_deny_shape(resp_nonexistent)!r} vs child={_deny_shape(resp_child)!r}")
    child_after = Group.objects.get(pk=opts.active_child_id)
    assert child_after.last_activity is None, (
        "a denied chain lookup must NOT touch() the child group, but last_activity "
        f"was set: {child_after.last_activity!r}")
    assert child_after.modified == opts.child_modified_before, (
        "the child group's modified must not bump from a denied chain lookup: "
        f"{opts.child_modified_before!r} -> {child_after.modified!r}")


@th.django_unit_test("DM-048: membership in an active grandparent above a DEACTIVATED middle no longer authorizes on the leaf")
def test_grandparent_membership_denied_across_inactive_middle(opts):
    _login(opts, CHAIN_USERNAME, CHAIN_PASSWORD)
    resp_leaf = opts.client.get(f"/api/group/{opts.active_leaf_id}/member")
    resp_nonexistent = opts.client.get(f"/api/group/{NONEXISTENT_PK}/member")
    opts.client.logout()

    assert _deny_shape(resp_leaf) == _deny_shape(resp_nonexistent), (
        "a deactivated MIDDLE group severs the chain — the grandparent's member "
        "must be denied on the leaf exactly like a nonexistent pk: "
        f"nonexistent={_deny_shape(resp_nonexistent)!r} vs leaf={_deny_shape(resp_leaf)!r}")


@th.django_unit_test("DM-048 subtree rule: even a DIRECT member of the active child is denied while an ancestor is inactive")
def test_direct_member_of_child_denied_under_inactive_parent(opts):
    from mojo.apps.account.models import GroupMember

    _login(opts, DIRECT_USERNAME, DIRECT_PASSWORD)
    resp_child = opts.client.get(f"/api/group/{opts.active_child_id}/member")
    resp_nonexistent = opts.client.get(f"/api/group/{NONEXISTENT_PK}/member")
    opts.client.logout()

    assert _deny_shape(resp_child) == _deny_shape(resp_nonexistent), (
        "a child under a DEACTIVATED parent is effectively inactive — even its "
        "own direct member must get the uniform deny: "
        f"nonexistent={_deny_shape(resp_nonexistent)!r} vs child={_deny_shape(resp_child)!r}")
    member_after = GroupMember.objects.get(pk=opts.direct_member_id)
    assert member_after.last_activity is None, (
        "a denied lookup must not touch() the membership row, but last_activity "
        f"was set: {member_after.last_activity!r}")


@th.django_unit_test("DM-048 direct model: get_member_for_user returns None under an inactive ancestor; is_active=False admin path unchanged")
def test_get_member_for_user_effective_active_contract(opts):
    from mojo.apps.account.models import Group, User

    child = Group.objects.get(pk=opts.active_child_id)
    chain_user = User.objects.get(pk=opts.chain_user_id)
    direct_user = User.objects.get(pk=opts.direct_user_id)

    assert child.get_member_for_user(chain_user, check_parents=True) is None, (
        "the parent walk must not resolve a membership in a DEACTIVATED parent")
    assert child.get_member_for_user(direct_user, check_parents=True) is None, (
        "a direct membership must not resolve while an ancestor is inactive "
        "(the child is effectively inactive)")
    admin_row = child.get_member_for_user(direct_user, check_parents=True, is_active=False)
    assert admin_row is not None, (
        "is_active=False (admin/introspection) must keep returning the raw "
        "membership row regardless of the group chain state")


@th.django_unit_test("DM-048 no one-way door: reactivating the parent instantly restores chain and direct access")
def test_reactivating_parent_restores_access(opts):
    from mojo.apps.account.models import Group

    parent = Group.objects.get(pk=opts.inactive_parent_id)
    parent.is_active = True
    parent.save()

    _login(opts, CHAIN_USERNAME, CHAIN_PASSWORD)
    resp_chain = opts.client.get(f"/api/group/{opts.active_child_id}/member")
    opts.client.logout()
    assert resp_chain.status_code == 200, (
        "reactivating the parent must instantly restore the inherited path, got "
        f"{resp_chain.status_code}: {resp_chain.body}")
    assert resp_chain.response.data.id == opts.chain_parent_member_id, (
        "the restored lookup must resolve the PARENT membership via the walk, got: "
        f"{dict(resp_chain.response.data)!r}")

    _login(opts, DIRECT_USERNAME, DIRECT_PASSWORD)
    resp_direct = opts.client.get(f"/api/group/{opts.active_child_id}/member")
    opts.client.logout()
    assert resp_direct.status_code == 200, (
        "reactivating the parent must instantly restore the direct member too, got "
        f"{resp_direct.status_code}: {resp_direct.body}")
    assert resp_direct.response.data.id == opts.direct_member_id, (
        "the direct member must get their own membership record back, got: "
        f"{dict(resp_direct.response.data)!r}")
