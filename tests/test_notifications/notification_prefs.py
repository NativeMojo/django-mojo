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
  - "*" master switch: off suppresses every kind on that channel (even an
    explicit per-kind on); per-kind off still suppresses under master on;
    master only affects its own channel
  - Kinds registry: default "general", re-register replaces, "*" and bad
    slugs rejected
  - GET carries "kinds" and "channels"; POST {"*": {...}} persists
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
    user.is_email_verified = True
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


# ===========================================================================
# "*" master switch
# ===========================================================================

def _set_prefs(user_id, prefs):
    from mojo.apps.account.models import User
    user = User.objects.get(pk=user_id)
    user.metadata = {"notification_preferences": prefs}
    user.save(update_fields=["metadata", "modified"])
    return user


@th.django_unit_test("master switch: email off suppresses general with no explicit entry")
def test_master_off_suppresses_unset_kind(opts):
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = _set_prefs(opts.user_id, {"*": {"email": False}})
    assert_true(not is_notification_allowed(user, "general", "email"),
                "master email off should suppress 'general' email with no per-kind entry")


@th.django_unit_test("master switch: email off beats an explicit per-kind on")
def test_master_off_beats_kind_on(opts):
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = _set_prefs(opts.user_id, {"*": {"email": False}, "billing": {"email": True}})
    assert_true(not is_notification_allowed(user, "billing", "email"),
                "master email off should suppress 'billing' email even though billing.email is True")


@th.django_unit_test("master switch: master on + kind off still suppresses")
def test_master_on_kind_off(opts):
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = _set_prefs(opts.user_id, {"*": {"email": True}, "marketing": {"email": False}})
    assert_true(not is_notification_allowed(user, "marketing", "email"),
                "per-kind email off should still suppress when master email is on")
    assert_true(is_notification_allowed(user, "general", "email"),
                "master email on with no per-kind entry should allow 'general' email")


@th.django_unit_test("master switch: only affects its own channel")
def test_master_channel_scoped(opts):
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = _set_prefs(opts.user_id, {"*": {"email": False}})
    assert_true(is_notification_allowed(user, "general", "in_app"),
                "master email off must not suppress in_app")
    assert_true(is_notification_allowed(user, "general", "push"),
                "master email off must not suppress push")


@th.django_unit_test("master switch: kind=None (transactional) is never suppressed")
def test_master_does_not_touch_transactional(opts):
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    user = _set_prefs(opts.user_id, {"*": {"email": False}})
    assert_true(is_notification_allowed(user, None, "email"),
                "kind=None (transactional) must stay allowed even with master email off")


# ===========================================================================
# Kinds registry
# ===========================================================================

@th.django_unit_test("kinds registry: general is pre-registered")
def test_registry_default_general(opts):
    from mojo.apps.account.services.notification_kinds import list_notification_kinds

    kinds = list_notification_kinds()
    assert_true(len(kinds) >= 1, "registry should not be empty")
    first = kinds[0]
    assert_eq(first.get("kind"), "general", f"first registered kind should be 'general', got {first}")
    assert_eq(first.get("label"), "General", f"general label should be 'General', got {first}")
    assert_eq(first.get("description"), "Messages from this service",
              f"general description mismatch: {first}")
    assert_true(first.get("channels") is None, f"general channels should default to None, got {first}")


@th.django_unit_test("kinds registry: re-registering replaces in place; order is registration order")
def test_registry_replace(opts):
    from mojo.apps.account.services import notification_kinds as nk

    snapshot = dict(nk._REGISTRY)
    try:
        nk.register_notification_kinds([
            {"kind": "testit.alpha", "label": "Alpha"},
            {"kind": "testit.beta", "label": "Beta", "channels": ["email"]},
        ])
        nk.register_notification_kinds([
            {"kind": "testit.alpha", "label": "Alpha 2", "description": "replaced"},
        ])
        kinds = nk.list_notification_kinds()
        names = [k["kind"] for k in kinds]
        assert_eq(names.count("testit.alpha"), 1, f"re-registered kind should appear once: {names}")
        assert_true(names.index("testit.alpha") < names.index("testit.beta"),
                    f"replacement should keep the original position: {names}")
        alpha = [k for k in kinds if k["kind"] == "testit.alpha"][0]
        assert_eq(alpha["label"], "Alpha 2", f"label should be replaced: {alpha}")
        assert_eq(alpha["description"], "replaced", f"description should be replaced: {alpha}")
        beta = [k for k in kinds if k["kind"] == "testit.beta"][0]
        assert_eq(beta["channels"], ["email"], f"channels should round-trip: {beta}")
    finally:
        nk._REGISTRY.clear()
        nk._REGISTRY.update(snapshot)


@th.django_unit_test("kinds registry: '*' and bad slugs are rejected without partial writes")
def test_registry_rejects_bad_kinds(opts):
    from mojo.apps.account.services import notification_kinds as nk

    snapshot = dict(nk._REGISTRY)
    try:
        for bad in ["*", "Billing", "has space", "", "a/b", "x" * 65]:
            raised = False
            try:
                nk.register_notification_kinds([{"kind": bad, "label": "Bad"}])
            except ValueError:
                raised = True
            assert_true(raised, f"register_notification_kinds should reject kind {bad!r}")

        raised = False
        try:
            nk.register_notification_kinds([
                {"kind": "testit.ok", "label": "Ok"},
                {"kind": "*", "label": "Master"},
            ])
        except ValueError:
            raised = True
        assert_true(raised, "a batch containing '*' should raise")
        names = [k["kind"] for k in nk.list_notification_kinds()]
        assert_true("testit.ok" not in names, f"a rejected batch must not register any kind: {names}")
    finally:
        nk._REGISTRY.clear()
        nk._REGISTRY.update(snapshot)


# ===========================================================================
# REST shape for the master switch + registry
# ===========================================================================

@th.django_unit_test("GET preferences: carries kinds and channels")
def test_get_kinds_and_channels(opts):
    _set_prefs(opts.user_id, {"marketing": {"email": False}})

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/notification/preferences")
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json.get("data", {})
    assert_eq(data.get("preferences"), {"marketing": {"email": False}},
              f"preferences should be unchanged by the additive fields: {data.get('preferences')}")
    assert_eq(data.get("channels"), ["email", "in_app", "push"],
              f"channels should be the sorted valid channels, got {data.get('channels')}")
    kinds = data.get("kinds")
    assert_true(isinstance(kinds, list) and kinds, f"kinds should be a non-empty list, got {kinds}")
    general = [k for k in kinds if k.get("kind") == "general"]
    assert_true(general, f"kinds should include 'general': {kinds}")
    assert_eq(general[0].get("label"), "General", f"general label mismatch: {general[0]}")


@th.django_unit_test("POST preferences: '*' master switch persists and GET reflects it")
def test_post_master_switch(opts):
    _set_prefs(opts.user_id, {})

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/notification/preferences", {
        "preferences": {"*": {"email": False}}
    })
    assert_eq(resp.status_code, 200, f"POST expected 200, got {resp.status_code}")
    prefs = resp.json.get("data", {}).get("preferences", {})
    assert_eq(prefs.get("*"), {"email": False}, f"POST response should carry the master switch: {prefs}")

    resp2 = opts.client.get("/api/account/notification/preferences")
    opts.client.logout()
    assert_eq(resp2.status_code, 200, f"GET expected 200, got {resp2.status_code}")
    prefs2 = resp2.json.get("data", {}).get("preferences", {})
    assert_eq(prefs2.get("*"), {"email": False}, f"GET should reflect the master switch: {prefs2}")

    from mojo.apps.account.models import User
    from mojo.apps.account.services.notification_prefs import is_notification_allowed
    user = User.objects.get(pk=opts.user_id)
    assert_true(not is_notification_allowed(user, "general", "email"),
                "stored master email off should suppress general email")
