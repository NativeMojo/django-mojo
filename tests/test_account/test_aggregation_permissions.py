"""
Permission-scope tests for the `_mode` aggregation surface.

Aggregation must inherit the same per-row visibility scoping as the
`_mode=list` path: owner-only filtering, group-only filtering, and
the hard deny when neither owner nor group scope applies.
"""
from testit import helpers as th


PWORD = "aggperm##mojo99"


def _reset_user(username, password=PWORD):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(password)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    return user


@th.django_unit_setup()
def setup_aggregation_perms(opts):
    """Two users in two groups; one with view_security in group A only."""
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps.incident.models import Event

    Event.objects.filter(category__in=["aggperm:a", "aggperm:b", "aggperm:none"]).delete()
    Group.objects.filter(name__in=["aggperm-grp-a", "aggperm-grp-b"]).delete()

    group_a = Group(name="aggperm-grp-a", kind="default")
    group_a.save()
    group_b = Group(name="aggperm-grp-b", kind="default")
    group_b.save()

    member_a = _reset_user("aggperm_member_a")
    no_perm = _reset_user("aggperm_no_perm")
    GroupMember.objects.filter(user__in=[member_a, no_perm]).delete()
    # member_a is in group A and has view_security inside that group.
    member_a_membership = GroupMember(user=member_a, group=group_a)
    member_a_membership.save()
    member_a_membership.add_permission("view_security")

    # Seed events, group-stamped.
    for _ in range(3):
        Event.objects.create(category="aggperm:a", source_ip="172.16.0.1", group=group_a)
    for _ in range(5):
        Event.objects.create(category="aggperm:b", source_ip="172.16.0.2", group=group_b)
    # Ungrouped events the no-perm user must not see.
    Event.objects.create(category="aggperm:none", source_ip="172.16.0.3")

    opts.member_a_user = "aggperm_member_a"
    opts.no_perm_user = "aggperm_no_perm"
    opts.pword = PWORD
    opts.group_a_id = group_a.id
    opts.group_b_id = group_b.id


@th.django_unit_test()
def test_aggregation_respects_group_scope(opts):
    """A user with view_security only in group A sees only group A rows."""
    assert opts.client.login(opts.member_a_user, opts.pword), "member_a login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "count",
            "category__in": "aggperm:a,aggperm:b,aggperm:none",
        },
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    assert resp.response["count"] == 3, (
        f"member_a is in group A only; expected count=3 (group A events), "
        f"got {resp.response['count']}: {resp.response}"
    )


@th.django_unit_test()
def test_aggregation_respects_perm_deny(opts):
    """A user with no perms anywhere must not get aggregates over global rows."""
    assert opts.client.login(opts.no_perm_user, opts.pword), "no-perm login failed"
    resp = opts.client.get(
        "/api/incident/event",
        params={
            "_mode": "count",
            "category__in": "aggperm:a,aggperm:b,aggperm:none",
        },
    )
    # Either an explicit 403 (perm-deny mode on) or 200 with count=0.
    if resp.status_code == 200:
        assert resp.response["count"] == 0, (
            f"no-perm user got count={resp.response['count']}; "
            f"aggregation must not leak rows when user has no view perm"
        )
    else:
        assert resp.status_code in (401, 403), (
            f"expected 401/403 for no-perm aggregation, got {resp.status_code}: {resp.body}"
        )
