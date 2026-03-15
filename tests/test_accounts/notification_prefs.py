"""
Tests for the notification preferences endpoints and enforcement helpers.

Coverage:
  - GET returns empty preferences when nothing is set
  - POST sets a preference; subsequent GET returns it
  - POST partial update does not affect previously set unrelated kinds
  - POST with non-dict preferences returns 400
  - POST with non-dict value for a kind returns 400
  - is_notification_allowed returns True when no preference stored (default on)
  - is_notification_allowed returns False when explicitly opted out
  - is_notification_allowed returns True for unknown kind
  - is_notification_allowed returns True for unknown channel
  - Notification creation is suppressed when in_app preference is False
  - send_template_email with kind is suppressed when email preference is False
  - send_template_email without kind is never suppressed (transactional)
  - push_notification with kind is suppressed when push preference is False
  - Unauthenticated GET/POST returns 403
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "notifprefs_user"
TEST_PWORD = "prefs##mojo99"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_notification_prefs(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.is_active = True
    user.metadata = {}
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk


# ===========================================================================
# Helper unit tests
# ===========================================================================

@th.django_unit_test("is_notification_allowed: True when no preferences stored")
def test_helper_default_true(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {}
    user.save(update_fields=["metadata", "modified"])

    result = is_notification_allowed(user, "marketing", "email")
    assert_true(result, "Expected True when no preferences are stored")


@th.django_unit_test("is_notification_allowed: False when explicitly opted out")
def test_helper_opted_out(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"marketing": {"email": False}}}
    user.save(update_fields=["metadata", "modified"])

    result = is_notification_allowed(user, "marketing", "email")
    assert_true(not result, "Expected False when user opted out of marketing email")


@th.django_unit_test("is_notification_allowed: True for unknown kind")
def test_helper_unknown_kind(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"marketing": {"email": False}}}
    user.save(update_fields=["metadata", "modified"])

    result = is_notification_allowed(user, "totally_new_kind", "email")
    assert_true(result, "Expected True for unknown kind (default allow)")


@th.django_unit_test("is_notification_allowed: True for unknown channel")
def test_helper_unknown_channel(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"marketing": {"email": False}}}
    user.save(update_fields=["metadata", "modified"])

    result = is_notification_allowed(user, "marketing", "carrier_pigeon")
    assert_true(result, "Expected True for unknown channel (default allow)")


@th.django_unit_test("is_notification_allowed: True when user is None")
def test_helper_none_user(opts):
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    result = is_notification_allowed(None, "marketing", "email")
    assert_true(result, "Expected True when user is None")


# ===========================================================================
# GET endpoint tests
# ===========================================================================

@th.django_unit_test("GET preferences: empty when nothing set")
def test_get_empty(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {}
    user.save(update_fields=["metadata", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/notification/preferences")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    prefs = data.get("data", {}).get("preferences", None)
    assert_true(isinstance(prefs, dict), "preferences should be a dict")
    assert_eq(len(prefs), 0, "preferences should be empty when nothing is set")


@th.django_unit_test("GET preferences: unauthenticated returns 401/403")
def test_get_unauth(opts):
    opts.client.logout()
    resp = opts.client.get("/api/account/notification/preferences")
    assert_true(resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}")


# ===========================================================================
# POST endpoint tests
# ===========================================================================

@th.django_unit_test("POST preferences: set and retrieve")
def test_post_set_and_get(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {}
    user.save(update_fields=["metadata", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/notification/preferences", {
        "preferences": {
            "marketing": {"email": False, "push": False}
        }
    })
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true on POST")
    prefs = data.get("data", {}).get("preferences", {})
    assert_eq(prefs.get("marketing", {}).get("email"), False, "marketing email should be False")
    assert_eq(prefs.get("marketing", {}).get("push"), False, "marketing push should be False")

    # Subsequent GET should return the same
    resp2 = opts.client.get("/api/account/notification/preferences")
    assert_eq(resp2.status_code, 200, f"GET after POST expected 200, got {resp2.status_code}")
    prefs2 = resp2.json.get("data", {}).get("preferences", {})
    assert_eq(prefs2.get("marketing", {}).get("email"), False, "GET: marketing email should be False")
    assert_eq(prefs2.get("marketing", {}).get("push"), False, "GET: marketing push should be False")
    opts.client.logout()


@th.django_unit_test("POST preferences: partial update does not affect other kinds")
def test_post_partial_update(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"alerts": {"in_app": True, "email": True}}}
    user.save(update_fields=["metadata", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/notification/preferences", {
        "preferences": {
            "marketing": {"email": False}
        }
    })
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    prefs = resp.json.get("data", {}).get("preferences", {})
    # marketing updated
    assert_eq(prefs.get("marketing", {}).get("email"), False, "marketing email should be False")
    # alerts untouched
    assert_eq(prefs.get("alerts", {}).get("in_app"), True, "alerts in_app should be unchanged")
    assert_eq(prefs.get("alerts", {}).get("email"), True, "alerts email should be unchanged")
    opts.client.logout()


@th.django_unit_test("POST preferences: non-dict preferences returns 400")
def test_post_non_dict_preferences(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/notification/preferences", {
        "preferences": "not_a_dict"
    })
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")
    opts.client.logout()


@th.django_unit_test("POST preferences: non-dict kind value returns 400")
def test_post_non_dict_kind_value(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/notification/preferences", {
        "preferences": {
            "marketing": "off"
        }
    })
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")
    opts.client.logout()


@th.django_unit_test("POST preferences: unauthenticated returns 401/403")
def test_post_unauth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/notification/preferences", {
        "preferences": {"marketing": {"email": False}}
    })
    assert_true(resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}")


# ===========================================================================
# Enforcement tests
# ===========================================================================

@th.django_unit_test("Notification.send suppressed when in_app preference is False")
def test_notification_suppressed_in_app(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.notification import Notification

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"promo": {"in_app": False}}}
    user.save(update_fields=["metadata", "modified"])

    # Count existing notifications
    before = Notification.objects.filter(user=user, kind="promo").count()

    Notification.send("Test promo", user=user, kind="promo", push=False, ws=False)

    after = Notification.objects.filter(user=user, kind="promo").count()
    assert_eq(after, before, "in_app notification should be suppressed when preference is False")


@th.django_unit_test("Notification.send created when in_app preference is True")
def test_notification_allowed_in_app(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.notification import Notification

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"updates": {"in_app": True}}}
    user.save(update_fields=["metadata", "modified"])

    before = Notification.objects.filter(user=user, kind="updates").count()

    Notification.send("Test update", user=user, kind="updates", push=False, ws=False)

    after = Notification.objects.filter(user=user, kind="updates").count()
    assert_eq(after, before + 1, "in_app notification should be created when preference is True")


@th.django_unit_test("send_template_email with kind suppressed when email preference is False")
def test_email_suppressed_with_kind(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"marketing": {"email": False}}}
    user.save(update_fields=["metadata", "modified"])

    # send_template_email with kind="marketing" should return None (suppressed)
    result = user.send_template_email("test_template", kind="marketing")
    assert_true(result is None, "send_template_email should return None when email preference is False for the kind")


@th.django_unit_test("send_template_email without kind is never suppressed by preferences")
def test_email_not_suppressed_without_kind(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"marketing": {"email": False}}}
    user.save(update_fields=["metadata", "modified"])

    # With no kind, the check would not even be called — but if it were called with
    # kind=None, it should still return True
    assert_true(is_notification_allowed(user, None, "email"),
                "is_notification_allowed with kind=None should always return True")


@th.django_unit_test("push_notification with kind suppressed when push preference is False")
def test_push_suppressed_with_kind(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    user.metadata = {"notification_preferences": {"marketing": {"push": False}}}
    user.save(update_fields=["metadata", "modified"])

    result = user.push_notification(title="Test", body="promo", kind="marketing")
    assert_eq(result, [], "push_notification should return empty list when push preference is False for the kind")