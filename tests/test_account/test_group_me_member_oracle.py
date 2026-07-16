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
"""
from testit import helpers as th


MEMBER_USERNAME = "dm039_member@test.com"
MEMBER_PASSWORD = "dm039_member_pw_99"
OUTSIDER_USERNAME = "dm039_outsider@test.com"
OUTSIDER_PASSWORD = "dm039_outsider_pw_99"
ACTIVE_GROUP = "dm039-active-group"
INACTIVE_GROUP = "dm039-inactive-group"
NONEXISTENT_PK = 999999999


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

    User.objects.filter(email__in=[MEMBER_USERNAME, OUTSIDER_USERNAME]).delete()
    Group.objects.filter(name__in=[ACTIVE_GROUP, INACTIVE_GROUP]).delete()

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
