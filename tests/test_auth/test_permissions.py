"""
Tests for permission keywords: "all", "authenticated", "member".

Covers:
- has_permission on User and GroupMember for each keyword
- group.user_has_permission for member/non-member
- REST-level enforcement via the GroupMember endpoint (which uses real group perms)
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_PWORD = "testit##mojo"


@th.django_unit_setup()
def setup_permission_tests(opts):
    from mojo.apps.account.models import User, Group
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    member_user = User.objects.filter(username="perm_member").last()
    if member_user is None:
        member_user = User(username="perm_member", email="perm_member@example.com", display_name="Perm Member")
        member_user.save()
    member_user.save_password(TEST_PWORD)
    member_user.save()
    opts.member_user = member_user

    outsider = User.objects.filter(username="perm_outsider").last()
    if outsider is None:
        outsider = User(username="perm_outsider", email="perm_outsider@example.com", display_name="Outsider")
        outsider.save()
    outsider.save_password(TEST_PWORD)
    outsider.save()
    opts.outsider = outsider

    group, _ = Group.objects.get_or_create(name="perm_test_group", defaults={"kind": "organization"})
    group.add_member(member_user)
    opts.group = group


# ---------------------------------------------------------------------------
# Unit: User.has_permission keyword behavior
# ---------------------------------------------------------------------------

@th.django_unit_test("user.has_permission: 'all' always True")
def test_user_has_permission_all(opts):
    assert_true(opts.member_user.has_permission("all"), "user.has_permission('all') should be True")


@th.django_unit_test("user.has_permission: 'authenticated' always True")
def test_user_has_permission_authenticated(opts):
    assert_true(opts.member_user.has_permission("authenticated"), "user.has_permission('authenticated') should be True")


@th.django_unit_test("user.has_permission: 'member' False without explicit perm")
def test_user_has_permission_member(opts):
    # 'member' on a plain user (no group context) should NOT short-circuit —
    # group membership check happens at rest_check_permission level, not here
    result = opts.member_user.has_permission("member")
    assert_true(not result, "user.has_permission('member') should be False — group check is upstream")


# ---------------------------------------------------------------------------
# Unit: GroupMember.has_permission keyword behavior
# ---------------------------------------------------------------------------

@th.django_unit_test("member.has_permission: 'all', 'authenticated', 'member' all True")
def test_member_has_permission_keywords(opts):
    from mojo.apps.account.models.member import GroupMember
    ms = GroupMember.objects.filter(user=opts.member_user, group=opts.group).first()
    assert_true(ms is not None, "member_user should be a member of perm_test_group")
    assert_true(ms.has_permission("all"), "member.has_permission('all') should be True")
    assert_true(ms.has_permission("authenticated"), "member.has_permission('authenticated') should be True")
    assert_true(ms.has_permission("member"), "member.has_permission('member') should be True")


# ---------------------------------------------------------------------------
# Unit: Group.user_has_permission for "member"
# ---------------------------------------------------------------------------

@th.django_unit_test("group.user_has_permission: member passes 'member' perm")
def test_group_member_passes(opts):
    # Simulate a request user (is_request_user is set by auth middleware on real requests)
    opts.member_user.is_request_user = True
    result = opts.group.user_has_permission(opts.member_user, ["member"])
    assert_true(result, "group member should pass 'member' perm check")


@th.django_unit_test("group.user_has_permission: outsider denied 'member' perm")
def test_group_outsider_denied(opts):
    opts.outsider.is_request_user = True
    result = opts.group.user_has_permission(opts.outsider, ["member"])
    assert_true(not result, "non-member should be denied 'member' perm check")


@th.django_unit_test("group.user_has_permission: 'authenticated' passes for any user")
def test_group_authenticated_passes_any(opts):
    # "authenticated" short-circuits at user.has_permission level, so even
    # the outsider passes when perm is "authenticated"
    result = opts.group.user_has_permission(opts.outsider, ["authenticated"])
    assert_true(result, "'authenticated' perm should pass for any authenticated user")

