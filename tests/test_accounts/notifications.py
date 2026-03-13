"""
Tests for the Notification model and user.notify() API.

Covers:
- Notification.send() creates DB records
- user.notify() delegates correctly
- Group fan-out creates one record per member
- mark_read action via REST
- Expiry field set correctly
- Persistent notifications (expires_in=None)
- REST: owner can list/read own notifications
- REST: user cannot see another user's notifications
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "notif_user"
TEST_OTHER = "notif_other"
TEST_PWORD = "notif##mojo"


@th.django_unit_setup()
def setup_notifications(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.notification import Notification
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com", display_name="Notif User")
        user.save()
    user.save_password(TEST_PWORD)
    user.save()
    opts.user = user

    other = User.objects.filter(username=TEST_OTHER).last()
    if other is None:
        other = User(username=TEST_OTHER, email=f"{TEST_OTHER}@example.com", display_name="Other User")
        other.save()
    other.save_password(TEST_PWORD)
    other.save()
    opts.other = other

    group, _ = Group.objects.get_or_create(name="notif_test_group", defaults={"kind": "organization"})
    group.add_member(user)
    group.add_member(other)
    opts.group = group

    # clean up leftover notifications
    Notification.objects.filter(user__in=[user, other]).delete()


# ---------------------------------------------------------------------------
# Unit: Notification.send()
# ---------------------------------------------------------------------------

@th.django_unit_test("Notification.send: creates DB record for user")
def test_send_creates_record(opts):
    from mojo.apps.account.models.notification import Notification

    before = Notification.objects.filter(user=opts.user).count()
    Notification.send("Test title", "Test body", user=opts.user, push=False, ws=False)
    after = Notification.objects.filter(user=opts.user).count()
    assert_eq(after, before + 1, "send() should create one Notification row")


@th.django_unit_test("Notification.send: fields stored correctly")
def test_send_fields(opts):
    from mojo.apps.account.models.notification import Notification

    notifs = Notification.send(
        "Hello", "World",
        user=opts.user, kind="alert",
        data={"foo": "bar"}, action_url="/test",
        push=False, ws=False,
    )
    assert_eq(len(notifs), 1, "should return list with one notification")
    n = notifs[0]
    assert_eq(n.title, "Hello", "title should match")
    assert_eq(n.body, "World", "body should match")
    assert_eq(n.kind, "alert", "kind should match")
    assert_eq(n.data.get("foo"), "bar", "data should match")
    assert_eq(n.action_url, "/test", "action_url should match")
    assert_true(n.is_unread, "new notification should be unread")
    assert_true(n.expires_at is not None, "expires_at should be set with default expiry")

    opts.notif_id = n.pk


@th.django_unit_test("Notification.send: persistent when expires_in=None")
def test_send_persistent(opts):
    from mojo.apps.account.models.notification import Notification

    notifs = Notification.send("Persist", user=opts.user, expires_in=None, push=False, ws=False)
    assert_eq(notifs[0].expires_at, None, "expires_at should be None for persistent notifications")


@th.django_unit_test("user.notify: delegates to Notification.send")
def test_user_notify(opts):
    from mojo.apps.account.models.notification import Notification

    before = Notification.objects.filter(user=opts.user).count()
    opts.user.notify("Via user.notify", push=False, ws=False)
    after = Notification.objects.filter(user=opts.user).count()
    assert_eq(after, before + 1, "user.notify() should create a Notification row")


# ---------------------------------------------------------------------------
# Unit: Group fan-out
# ---------------------------------------------------------------------------

@th.django_unit_test("Notification.send: group fan-out creates one per member")
def test_group_fanout(opts):
    from mojo.apps.account.models.notification import Notification

    before_user = Notification.objects.filter(user=opts.user).count()
    before_other = Notification.objects.filter(user=opts.other).count()

    Notification.send("Group alert", group=opts.group, push=False, ws=False)

    assert_eq(
        Notification.objects.filter(user=opts.user).count(),
        before_user + 1,
        "group send should create a notification for member_user",
    )
    assert_eq(
        Notification.objects.filter(user=opts.other).count(),
        before_other + 1,
        "group send should create a notification for other member",
    )


@th.django_unit_test("Notification.send: user+group deduplicates user")
def test_send_user_and_group_no_dupe(opts):
    from mojo.apps.account.models.notification import Notification

    before = Notification.objects.filter(user=opts.user).count()
    # user is already a member of group — should not get two records
    Notification.send("No dupe", user=opts.user, group=opts.group, push=False, ws=False)
    after = Notification.objects.filter(user=opts.user).count()
    assert_eq(after, before + 1, "user should receive only one notification when in both user and group")


# ---------------------------------------------------------------------------
# Unit: mark_read action
# ---------------------------------------------------------------------------

@th.django_unit_test("on_action_mark_read: sets is_unread=False")
def test_mark_read_action(opts):
    from mojo.apps.account.models.notification import Notification

    n = Notification.objects.get(pk=opts.notif_id)
    assert_true(n.is_unread, "should start unread")
    n.on_action_mark_read(True)
    n.refresh_from_db()
    assert_true(not n.is_unread, "should be marked read after action")


# ---------------------------------------------------------------------------
# REST: owner access
# ---------------------------------------------------------------------------

@th.django_unit_test("REST: user can list own unread notifications")
def test_rest_list_unread(opts):
    from mojo.apps.account.models.notification import Notification

    Notification.send("REST unread test", user=opts.user, push=False, ws=False)

    opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(opts.client.is_authenticated, "login should succeed")

    resp = opts.client.get("/api/account/notification")
    assert_eq(resp.status_code, 200, f"owner should list own notifications, got {resp.status_code}")
    assert_true(resp.response.count > 0, "should have at least one unread notification")


@th.django_unit_test("REST: mark_read via POST action")
def test_rest_mark_read(opts):
    from mojo.apps.account.models.notification import Notification

    notifs = Notification.send("Mark via REST", user=opts.user, push=False, ws=False)
    n = notifs[0]

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(f"/api/account/notification/{n.pk}", {"mark_read": True})
    assert_eq(resp.status_code, 200, f"mark_read action should return 200, got {resp.status_code}")

    n.refresh_from_db()
    assert_true(not n.is_unread, "notification should be marked read")


@th.django_unit_test("REST: user cannot see another user's notifications")
def test_rest_cannot_see_others(opts):
    from mojo.apps.account.models.notification import Notification

    notifs = Notification.send("Private", user=opts.other, push=False, ws=False)
    n = notifs[0]

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get(f"/api/account/notification/{n.pk}")
    assert_true(resp.status_code in [401, 403, 404], f"user should not access another's notification, got {resp.status_code}")


@th.django_unit_test("REST: unauthenticated cannot list notifications")
def test_rest_unauthenticated(opts):
    from mojo.models.rest import MOJO_REST_LIST_PERM_DENY
    opts.client.logout()
    resp = opts.client.get("/api/account/notification")
    if MOJO_REST_LIST_PERM_DENY:
        assert_true(resp.status_code in [401, 403], f"unauthenticated should be denied, got {resp.status_code}")
    else:
        assert_true(resp.status_code in [200, 401, 403], f"unauthenticated should be denied or empty, got {resp.status_code}")
        if resp.status_code == 200:
            assert_eq(resp.response.count, 0, "unauthenticated should get empty list")
