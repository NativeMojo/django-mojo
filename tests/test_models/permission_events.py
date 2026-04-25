"""
Permission-denial event emission via the REST dispatcher.

Asserts each branch of `_evaluate_permission` produces the right
HTTP status, exactly one incident event, and the expected metadata
(`branch`, `event_type`, `model_name`).

See planning/issues/spurious-permission-denied-events-on-list.md
"""
from testit import helpers as th


TEST_NOPERM = "perm_events_noperm"
TEST_PWORD = "testit##mojo"
TEST_FIXTURE_GROUP = "perm-events-fixture"
TEST_RULESET = "perm-events-ruleset"


@th.django_unit_setup()
def setup_permission_events(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.models.event import Event

    user = User.objects.filter(username=TEST_NOPERM).last()
    if user is None:
        user = User(
            username=TEST_NOPERM,
            display_name=TEST_NOPERM,
            email=f"{TEST_NOPERM}@example.com",
        )
        user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    GroupMember.objects.filter(user=user).delete()
    opts.user_id = user.id

    Group.objects.filter(name=TEST_FIXTURE_GROUP).delete()
    group = Group(name=TEST_FIXTURE_GROUP, kind="default")
    group.save()
    opts.fixture_group_id = group.id

    RuleSet.objects.filter(name=TEST_RULESET).delete()
    ruleset = RuleSet.objects.create(name=TEST_RULESET, category="perm_events_cat")
    opts.ruleset_id = ruleset.id

    Event.objects.filter(uid=user.id).delete()


def _events_for_user(uid, category=None):
    from mojo.apps.incident.models.event import Event
    qs = Event.objects.filter(uid=uid)
    if category:
        qs = qs.filter(category=category)
    return list(qs.values("category", "details", "metadata"))


@th.django_unit_test()
def test_get_protected_instance_emits_view_permission_denied(opts):
    """GET /api/group/<id> by a user with no view_groups + not a member.

    Group implements check_view_permission so the predicate routes through
    the instance-view branch and the dispatcher emits view_permission_denied.
    """
    from mojo.apps.incident.models.event import Event
    Event.objects.filter(uid=opts.user_id).delete()

    assert opts.client.login(TEST_NOPERM, TEST_PWORD), "login failed"

    resp = opts.client.get(f"/api/group/{opts.fixture_group_id}")
    assert resp.status_code == 403, (
        f"Expected 403, got {resp.status_code}: {resp.response!r}"
    )

    events = _events_for_user(opts.user_id, category="view_permission_denied")
    assert len(events) == 1, (
        f"Expected exactly 1 view_permission_denied event, got "
        f"{len(events)}: {events!r}"
    )
    meta = events[0]["metadata"]
    assert meta.get("branch") == "instance.check_view_permission", (
        f"Expected branch=instance.check_view_permission, got {meta.get('branch')!r}"
    )
    assert meta.get("model_name") == "Group", (
        f"Expected model_name=Group, got {meta.get('model_name')!r}"
    )
    assert meta.get("instance"), "Expected non-empty `instance` repr in metadata"


@th.django_unit_test()
def test_post_protected_emits_user_permission_denied(opts):
    """POST /api/incident/event/ruleset by a no-perms user.

    No instance, perms not satisfied at user.has_permission → fires
    user_permission_denied via the dispatcher.
    """
    from mojo.apps.incident.models.event import Event
    Event.objects.filter(uid=opts.user_id, category="user_permission_denied").delete()

    assert opts.client.login(TEST_NOPERM, TEST_PWORD), "login failed"

    resp = opts.client.post(
        "/api/incident/event/ruleset",
        json={"name": "perm-events-attempt", "category": "perm_events_cat"},
    )
    assert resp.status_code == 403, (
        f"Expected 403 on POST ruleset by noperm user, got "
        f"{resp.status_code}: {resp.response!r}"
    )

    events = _events_for_user(opts.user_id, category="user_permission_denied")
    assert len(events) == 1, (
        f"Expected exactly 1 user_permission_denied event, got "
        f"{len(events)}: {events!r}"
    )
    meta = events[0]["metadata"]
    assert meta.get("branch") == "user.has_permission", (
        f"Expected branch=user.has_permission, got {meta.get('branch')!r}"
    )
    assert meta.get("model_name") == "RuleSet", (
        f"Expected model_name=RuleSet, got {meta.get('model_name')!r}"
    )


@th.django_unit_test()
def test_delete_protected_emits_user_permission_denied(opts):
    """DELETE /api/incident/event/ruleset/<id> by a no-perms user."""
    from mojo.apps.incident.models.event import Event
    Event.objects.filter(uid=opts.user_id, category="user_permission_denied").delete()

    assert opts.client.login(TEST_NOPERM, TEST_PWORD), "login failed"

    resp = opts.client.delete(f"/api/incident/event/ruleset/{opts.ruleset_id}")
    assert resp.status_code == 403, (
        f"Expected 403 on DELETE ruleset by noperm user, got "
        f"{resp.status_code}: {resp.response!r}"
    )

    events = _events_for_user(opts.user_id, category="user_permission_denied")
    assert len(events) == 1, (
        f"Expected exactly 1 user_permission_denied event for DELETE, "
        f"got {len(events)}: {events!r}"
    )


@th.django_unit_test()
def test_recovery_path_emits_no_event(opts):
    """A list endpoint with a recovery fallback (Group's empty-list path)
    must produce zero denial events for an authenticated user.
    Doubles up the regression already covered in test_account."""
    from mojo.apps.incident.models.event import Event
    Event.objects.filter(uid=opts.user_id).delete()

    assert opts.client.login(TEST_NOPERM, TEST_PWORD), "login failed"
    resp = opts.client.get("/api/group", params={"size": 50})
    assert resp.status_code == 200, (
        f"Expected 200 from recovery path, got {resp.status_code}"
    )

    bogus = Event.objects.filter(
        uid=opts.user_id,
        category__in=[
            "user_permission_denied",
            "view_permission_denied",
            "group_member_permission_denied",
        ],
    ).count()
    assert bogus == 0, (
        f"Recovery path emitted {bogus} false-positive denial event(s)"
    )
