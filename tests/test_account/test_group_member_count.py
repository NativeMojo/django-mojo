"""Tests for Group.member_count property surfaced via REST graphs."""
from testit import helpers as th


ADMIN_USERNAME = "group_count_admin@test.com"
ADMIN_PASSWORD = "group_count_admin_99"
GROUP_NAME = "group_count_test_group"


@th.django_unit_setup()
def setup_group_member_count(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    User.objects.filter(email__in=[
        ADMIN_USERNAME,
        "group_count_member1@test.com", "group_count_member2@test.com",
        "group_count_member3@test.com",
    ]).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    admin = User.objects.create_user(username=ADMIN_USERNAME, email=ADMIN_USERNAME, password=ADMIN_PASSWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.save()
    admin.add_permission("manage_groups")
    admin.add_permission("view_groups")
    opts.admin_id = admin.pk

    group = Group.objects.create(name=GROUP_NAME, is_active=True)
    opts.group_id = group.pk

    # 2 active members + 1 inactive
    for i in (1, 2, 3):
        u = User.objects.create_user(
            username=f"group_count_member{i}@test.com",
            email=f"group_count_member{i}@test.com",
            password="member_pw_99")
        u.is_active = True
        u.save()
        gm = GroupMember.objects.create(user=u, group=group, is_active=(i != 3))


@th.django_unit_test()
def test_member_count_property(opts):
    from mojo.apps.account.models import Group

    group = Group.objects.get(pk=opts.group_id)
    assert group.member_count == 2, \
        f"member_count should count only active members (2 of 3), got: {group.member_count}"


@th.django_unit_test()
def test_member_count_in_default_graph(opts):
    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/group/{opts.group_id}")
    opts.client.logout()

    assert resp.status_code == 200, \
        f"GET should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    body = resp.response.data
    assert body.get("member_count") == 2, \
        f"default graph should include member_count=2, got: {body.get('member_count')}"


@th.django_unit_test()
def test_member_count_in_list_response(opts):
    """Listing groups should include member_count for each entry (default graph)."""
    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/group?id={opts.group_id}")
    opts.client.logout()

    assert resp.status_code == 200, \
        f"GET list should succeed, got {resp.status_code}"
    items = resp.response.data
    assert isinstance(items, list) and items, \
        f"expected at least one group in list response, got: {items!r}"
    target = next((g for g in items if g.get("id") == opts.group_id), None)
    assert target is not None, f"target group {opts.group_id} should be in list response"
    assert target.get("member_count") == 2, \
        f"list response should include member_count=2 for the target group, got: {target.get('member_count')}"


@th.django_unit_test()
def test_member_count_not_in_basic_graph(opts):
    """`basic` graph stays minimal — member_count is `default`-only."""
    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/group/{opts.group_id}?graph=basic")
    opts.client.logout()

    assert resp.status_code == 200, f"GET should succeed, got {resp.status_code}"
    body = resp.response.data
    assert "member_count" not in body, \
        f"basic graph should NOT include member_count, got: {body.get('member_count')}"
