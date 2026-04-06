"""
Tests for the comms domain send_notification tool.

Verifies tool registration, recipient resolution, channel dispatching,
and edge cases (cap, inactive users, missing phone, preferences).
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_PREFIX = "notif_test_"
TEST_DOMAIN = "notiftest.example.com"


def _cleanup():
    """Remove test users and related data before setup."""
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.account.models.notification import Notification

    User.objects.filter(email__startswith=TEST_EMAIL_PREFIX).delete()
    Group.objects.filter(name="notif_test_group").delete()


def _create_user(suffix, is_active=True, phone_number=None, metadata=None,
                 permissions=None, is_superuser=False):
    from mojo.apps.account.models import User

    email = f"{TEST_EMAIL_PREFIX}{suffix}@{TEST_DOMAIN}"
    User.objects.filter(email=email).delete()

    user = User.objects.create_user(
        username=email, email=email, password="TestPass1!",
    )
    user.is_active = is_active
    user.is_superuser = is_superuser
    if phone_number:
        user.phone_number = phone_number
    if metadata:
        user.metadata = metadata
    user.save()
    if permissions:
        for perm in permissions:
            user.add_permission(perm)
    return user


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_notifications(opts):
    _cleanup()

    opts.user_a = _create_user("a", phone_number="+15551234567")
    opts.user_b = _create_user("b", phone_number="+15559876543")
    opts.user_inactive = _create_user("inactive", is_active=False)
    opts.user_no_phone = _create_user("nophone")
    opts.user_super = _create_user("super", is_superuser=True, permissions=["comms"])
    opts.user_oncall = _create_user("oncall", metadata={"role": "oncall"})

    # Create a group with user_a and user_b as members
    from mojo.apps.account.models import Group, GroupMember
    Group.objects.filter(name="notif_test_group").delete()
    opts.group = Group.objects.create(name="notif_test_group")
    GroupMember.objects.create(group=opts.group, user=opts.user_a, is_active=True)
    GroupMember.objects.create(group=opts.group, user=opts.user_b, is_active=True)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_tool_registered_with_mutates(opts):
    """send_notification should be registered with mutates=True in comms domain."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    assert_true("send_notification" in registry,
                "send_notification should be registered")
    entry = registry["send_notification"]
    assert_eq(entry["domain"], "comms",
              f"Expected domain 'comms', got {entry['domain']}")
    assert_eq(entry["mutates"], True,
              "send_notification should have mutates=True")
    assert_eq(entry["permission"], "comms",
              f"Expected permission 'comms', got {entry['permission']}")


@th.django_unit_test()
def test_comms_domain_in_discovery(opts):
    """comms should appear in DOMAIN_DESCRIPTIONS."""
    from mojo.apps.assistant import DOMAIN_DESCRIPTIONS

    assert_true("comms" in DOMAIN_DESCRIPTIONS,
                f"comms should be in DOMAIN_DESCRIPTIONS, got {list(DOMAIN_DESCRIPTIONS.keys())}")


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_resolve_by_usernames(opts):
    """Resolve recipients by username list."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    users, errors = _resolve_recipients({"usernames": [opts.user_a.username]})
    assert_eq(len(users), 1, f"Expected 1 user, got {len(users)}")
    assert_eq(users[0].pk, opts.user_a.pk,
              f"Expected user_a (pk={opts.user_a.pk}), got pk={users[0].pk}")
    assert_eq(len(errors), 0, f"Expected no errors, got {errors}")


@th.django_unit_test()
def test_resolve_by_email_fallback(opts):
    """Resolve recipients by email when username lookup fails."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    users, errors = _resolve_recipients({"usernames": [opts.user_a.email]})
    assert_eq(len(users), 1, f"Expected 1 user, got {len(users)}")
    assert_eq(users[0].pk, opts.user_a.pk,
              f"Expected user_a, got pk={users[0].pk}")


@th.django_unit_test()
def test_resolve_by_permission(opts):
    """Resolve recipients by permission filter."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    users, errors = _resolve_recipients({"permission": "is_superuser"})
    user_ids = {u.pk for u in users}
    assert_true(opts.user_super.pk in user_ids,
                f"Superuser should be in results, got pks={user_ids}")
    # All returned users should be active
    for u in users:
        assert_true(u.is_active, f"User {u.pk} should be active")


@th.django_unit_test()
def test_resolve_by_group(opts):
    """Resolve recipients by group_id."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    users, errors = _resolve_recipients({"group_id": opts.group.pk})
    user_ids = {u.pk for u in users}
    assert_true(opts.user_a.pk in user_ids,
                f"user_a should be in group results, got pks={user_ids}")
    assert_true(opts.user_b.pk in user_ids,
                f"user_b should be in group results, got pks={user_ids}")


@th.django_unit_test()
def test_resolve_by_metadata(opts):
    """Resolve recipients by metadata filter."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    users, errors = _resolve_recipients({"metadata": {"role": "oncall"}})
    user_ids = {u.pk for u in users}
    assert_true(opts.user_oncall.pk in user_ids,
                f"oncall user should be in results, got pks={user_ids}")


@th.django_unit_test()
def test_resolve_by_email_domain(opts):
    """Resolve recipients by email domain."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    users, errors = _resolve_recipients({"email_domain": TEST_DOMAIN})
    # Should find all active test users with that domain
    assert_true(len(users) >= 4,
                f"Expected at least 4 active users with domain {TEST_DOMAIN}, got {len(users)}")
    for u in users:
        assert_true(u.is_active, f"User {u.pk} should be active")
        assert_true(u.email.endswith("@" + TEST_DOMAIN),
                    f"User email {u.email} should end with @{TEST_DOMAIN}")


@th.django_unit_test()
def test_resolve_excludes_inactive_users(opts):
    """Inactive users should never be included in resolution."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    # By username — inactive user should be in errors
    users, errors = _resolve_recipients({"usernames": [opts.user_inactive.username]})
    assert_eq(len(users), 0, "Inactive user should not be in users list")
    assert_eq(len(errors), 1, f"Expected 1 error for inactive user, got {len(errors)}")
    assert_true("inactive" in errors[0]["reason"],
                f"Error should mention inactive, got: {errors[0]['reason']}")

    # By email domain — inactive user should be filtered out
    users2, _ = _resolve_recipients({"email_domain": TEST_DOMAIN})
    inactive_pks = {u.pk for u in users2}
    assert_true(opts.user_inactive.pk not in inactive_pks,
                "Inactive user should not appear in email_domain results")


@th.django_unit_test()
def test_recipient_cap_enforced(opts):
    """More than MAX_RECIPIENTS should return an error."""
    from mojo.apps.assistant.services.tools.notifications import _tool_send_notification, MAX_RECIPIENTS
    from mojo.apps.account.models import User
    from unittest.mock import patch

    # Mock _resolve_recipients to return too many users
    fake_users = [opts.user_a] * (MAX_RECIPIENTS + 1)

    with patch("mojo.apps.assistant.services.tools.notifications._resolve_recipients",
               return_value=(fake_users, [])):
        result = _tool_send_notification({
            "channel": "in_app",
            "body": "test",
            "recipients": {"usernames": ["x"]},
        }, opts.user_super)

    assert_true("error" in result, f"Expected error in result, got {result}")
    assert_true("Too many" in result["error"],
                f"Error should mention too many recipients, got: {result['error']}")


# ---------------------------------------------------------------------------
# Channel dispatching
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_send_in_app(opts):
    """Sending in_app should create a Notification record."""
    from mojo.apps.account.models.notification import Notification
    from mojo.apps.assistant.services.tools.notifications import _tool_send_notification

    # Clean up any existing notifications for user_a
    Notification.objects.filter(user=opts.user_a, title="Test In-App").delete()

    result = _tool_send_notification({
        "channel": "in_app",
        "body": "Hello from assistant",
        "title": "Test In-App",
        "recipients": {"usernames": [opts.user_a.username]},
    }, opts.user_super)

    assert_eq(result.get("sent"), 1, f"Expected 1 sent, got {result}")
    assert_eq(result.get("failed"), 0, f"Expected 0 failed, got {result}")

    notif = Notification.objects.filter(user=opts.user_a, title="Test In-App").first()
    assert_true(notif is not None, "Notification record should exist in DB")
    assert_eq(notif.body, "Hello from assistant",
              f"Notification body should match, got: {notif.body}")


@th.django_unit_test()
def test_send_sms_missing_phone(opts):
    """User without phone number should be skipped for SMS."""
    from mojo.apps.assistant.services.tools.notifications import _tool_send_notification

    result = _tool_send_notification({
        "channel": "sms",
        "body": "Test SMS",
        "recipients": {"usernames": [opts.user_no_phone.username]},
    }, opts.user_super)

    assert_eq(result.get("skipped"), 1, f"Expected 1 skipped, got {result}")
    assert_eq(result.get("sent"), 0, f"Expected 0 sent, got {result}")
    assert_true(len(result.get("errors", [])) > 0, "Should have error details")
    assert_true("no phone number" in result["errors"][0]["reason"],
                f"Error should mention no phone number, got: {result['errors'][0]['reason']}")


@th.django_unit_test()
def test_notification_prefs_respected(opts):
    """User who opted out of a channel should be skipped."""
    from mojo.apps.assistant.services.tools.notifications import _tool_send_notification

    # Set user_a to opt out of in_app for "general" kind
    opts.user_a.metadata = {"notification_preferences": {"general": {"in_app": False}}}
    opts.user_a.save(update_fields=["metadata"])

    result = _tool_send_notification({
        "channel": "in_app",
        "body": "Should be skipped",
        "title": "Opt Out Test",
        "recipients": {"usernames": [opts.user_a.username]},
    }, opts.user_super)

    assert_eq(result.get("skipped"), 1, f"Expected 1 skipped, got {result}")
    assert_eq(result.get("sent"), 0, f"Expected 0 sent, got {result}")
    assert_true("opted out" in result["errors"][0]["reason"],
                f"Error should mention opted out, got: {result['errors'][0]['reason']}")

    # Clean up — restore preferences
    opts.user_a.metadata = {}
    opts.user_a.save(update_fields=["metadata"])


@th.django_unit_test()
def test_delivery_summary_format(opts):
    """Response should have sent, skipped, failed, errors, total_recipients keys."""
    from mojo.apps.assistant.services.tools.notifications import _tool_send_notification

    result = _tool_send_notification({
        "channel": "in_app",
        "body": "Format test",
        "title": "Format Test",
        "recipients": {"usernames": [opts.user_a.username]},
    }, opts.user_super)

    for key in ("sent", "skipped", "failed", "errors", "total_recipients"):
        assert_true(key in result,
                    f"Result should contain '{key}', got keys: {list(result.keys())}")
    assert_true(isinstance(result["errors"], list),
                f"errors should be a list, got {type(result['errors'])}")


@th.django_unit_test()
def test_invalid_channel_rejected(opts):
    """Invalid channel should return error."""
    from mojo.apps.assistant.services.tools.notifications import _tool_send_notification

    result = _tool_send_notification({
        "channel": "carrier_pigeon",
        "body": "coo",
        "recipients": {"usernames": [opts.user_a.username]},
    }, opts.user_super)

    assert_true("error" in result, f"Expected error for invalid channel, got {result}")


@th.django_unit_test()
def test_multiple_recipient_keys_rejected(opts):
    """Providing multiple recipient strategies should return error."""
    from mojo.apps.assistant.services.tools.notifications import _resolve_recipients

    users, errors = _resolve_recipients({
        "usernames": ["x"],
        "group_id": 1,
    })
    assert_eq(len(users), 0, "Should return no users")
    assert_true(len(errors) > 0, "Should return error")
    assert_true("exactly one" in errors[0]["reason"].lower(),
                f"Error should mention exactly one, got: {errors[0]['reason']}")
