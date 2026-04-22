"""
Admin RestMeta endpoints — list/detail require VIEW_PERMS, status updates
require SAVE_PERMS, delete requires DELETE_PERMS.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


LIST_PATH = "/api/account/public_message"


@th.django_unit_setup()
def setup_admin(opts):
    from mojo.apps.account.models import User, Group, GroupMember, PublicMessage

    User.objects.filter(email__in=[
        "pm-admin-view@example.com",
        "pm-admin-manage@example.com",
        "pm-admin-none@example.com",
        "pm-admin-group@example.com",
    ]).delete()
    Group.objects.filter(name__in=["PM-AdminGroup", "PM-AdminOther"]).delete()
    PublicMessage.objects.filter(email__in=[
        "pm-admin-ungrouped@example.com",
        "pm-admin-g1@example.com",
        "pm-admin-g2@example.com",
    ]).delete()

    opts.group = Group.objects.create(name="PM-AdminGroup", is_active=True)
    opts.other_group = Group.objects.create(name="PM-AdminOther", is_active=True)

    opts.view_user = User.objects.create_user(
        username="pm-admin-view@example.com",
        email="pm-admin-view@example.com",
        password="test123",
    )
    opts.view_user.add_permission("view_support")

    opts.manage_user = User.objects.create_user(
        username="pm-admin-manage@example.com",
        email="pm-admin-manage@example.com",
        password="test123",
    )
    opts.manage_user.add_permission("manage_support")

    opts.none_user = User.objects.create_user(
        username="pm-admin-none@example.com",
        email="pm-admin-none@example.com",
        password="test123",
    )

    opts.group_only_user = User.objects.create_user(
        username="pm-admin-group@example.com",
        email="pm-admin-group@example.com",
        password="test123",
    )
    gm = GroupMember.objects.create(
        user=opts.group_only_user, group=opts.group, is_active=True,
    )
    gm.add_permission("view_support")

    # One ungrouped message, one in opts.group, one in opts.other_group.
    opts.ungrouped_msg = PublicMessage.objects.create(
        kind="contact_us",
        name="Ungrouped",
        email="pm-admin-ungrouped@example.com",
        message="hi",
    )
    opts.g1_msg = PublicMessage.objects.create(
        kind="support",
        group=opts.group,
        name="InGroup",
        email="pm-admin-g1@example.com",
        message="hi",
        metadata={"category": "bug", "severity": "low"},
    )
    opts.g2_msg = PublicMessage.objects.create(
        kind="support",
        group=opts.other_group,
        name="InOther",
        email="pm-admin-g2@example.com",
        message="hi",
        metadata={"category": "other", "severity": "low"},
    )


@th.django_unit_test()
def test_admin_list_requires_permission(opts):
    opts.client.logout()
    resp = opts.client.get(LIST_PATH)
    assert_true(
        resp.status_code in (401, 403),
        f"unauthenticated access should be 401/403, got {resp.status_code}",
    )


@th.django_unit_test()
def test_admin_list_denied_without_perms(opts):
    opts.client.login("pm-admin-none@example.com", "test123")
    resp = opts.client.get(LIST_PATH)
    assert_true(
        resp.status_code in (401, 403) or (
            resp.status_code == 200 and (
                resp.response.data is None
                or (hasattr(resp.response.data, 'get')
                    and not resp.response.data.get("data"))
            )
        ),
        f"user without support perms should not see messages, got {resp.status_code} {resp.response}",
    )
    opts.client.logout()


@th.django_unit_test()
def test_admin_list_view_support_can_read(opts):
    opts.client.login("pm-admin-view@example.com", "test123")
    resp = opts.client.get(LIST_PATH)
    assert_eq(resp.status_code, 200, f"view_support should read list, got {resp.status_code}")
    data = resp.response.data
    # Response shape: either {data: [...]} or {..., data: {...}}; check at least list exists
    assert_true(data is not None, f"expected data payload, got {resp.response}")
    opts.client.logout()


@th.django_unit_test()
def test_admin_manage_support_can_update_status(opts):
    from mojo.apps.account.models import PublicMessage

    opts.client.login("pm-admin-manage@example.com", "test123")
    resp = opts.client.post(f"{LIST_PATH}/{opts.ungrouped_msg.pk}", {"status": "closed"})
    assert_eq(
        resp.status_code, 200,
        f"manage_support should update status, got {resp.status_code}: {resp.response}",
    )
    opts.ungrouped_msg.refresh_from_db()
    assert_eq(
        opts.ungrouped_msg.status, "closed",
        f"status should persist as 'closed', got {opts.ungrouped_msg.status}",
    )
    opts.client.logout()


@th.django_unit_test()
def test_admin_group_scoped_filter(opts):
    """
    A user with only group-scoped view_support should NOT see messages outside
    their group. System-level view_support users see everything.
    """
    from mojo.apps.account.models import PublicMessage

    # Group-only view: must not reveal opts.other_group's messages.
    opts.client.login("pm-admin-group@example.com", "test123")
    resp = opts.client.get(LIST_PATH)
    assert_eq(resp.status_code, 200, f"group-scoped perm should list, got {resp.status_code}")
    data = resp.response.data
    # data is {data: [...], count: N, ...} after framework processing
    rows = []
    if hasattr(data, "get"):
        rows = data.get("data") or []
    emails = [r.get("email") for r in rows if isinstance(r, dict) or hasattr(r, "get")]
    assert_true(
        "pm-admin-g2@example.com" not in emails,
        f"group-only admin must NOT see other group's message, got emails={emails}",
    )
    opts.client.logout()


@th.django_unit_test()
def test_delete_requires_manage_support(opts):
    from mojo.apps.account.models import PublicMessage

    # view_support should NOT be able to delete.
    opts.client.login("pm-admin-view@example.com", "test123")
    resp = opts.client.delete(f"{LIST_PATH}/{opts.g1_msg.pk}")
    assert_true(
        resp.status_code in (401, 403, 404, 405),
        f"view_support must not delete, got {resp.status_code}: {resp.response}",
    )
    assert_true(
        PublicMessage.objects.filter(pk=opts.g1_msg.pk).exists(),
        "record should still exist after denied delete",
    )
    opts.client.logout()

    # manage_support CAN delete.
    opts.client.login("pm-admin-manage@example.com", "test123")
    resp = opts.client.delete(f"{LIST_PATH}/{opts.g1_msg.pk}")
    assert_true(
        resp.status_code in (200, 204),
        f"manage_support should delete, got {resp.status_code}: {resp.response}",
    )
    assert_true(
        not PublicMessage.objects.filter(pk=opts.g1_msg.pk).exists(),
        "record should be gone after manage_support delete",
    )
    opts.client.logout()
