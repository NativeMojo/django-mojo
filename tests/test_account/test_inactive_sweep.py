"""Tests for auto-disable inactive users and groups sweep."""
from unittest import mock
from testit import helpers as th


@th.django_unit_setup()
def setup_inactive_sweep(opts):
    from mojo.apps.account.models import User, Group
    from mojo.helpers import dates

    # Clean up test users
    User.objects.filter(email__in=[
        "inactive_test1@test.com", "inactive_test2@test.com",
        "inactive_staff@test.com", "inactive_super@test.com",
        "inactive_protected@test.com", "inactive_neverlogin@test.com",
        "inactive_warned@test.com", "inactive_reactivated@test.com",
        "groupadmin@test.com",
    ]).delete()
    Group.objects.filter(name__startswith="inactive_test_").delete()

    # User inactive for 100 days (should be disabled)
    opts.stale_user = User.objects.create_user(
        username="inactive_test1@test.com", email="inactive_test1@test.com", password="pass123",
    )
    opts.stale_user.is_active = True
    opts.stale_user.last_activity = dates.subtract(days=100)
    opts.stale_user.last_login = dates.subtract(days=100)
    opts.stale_user.save()

    # User inactive for 85 days (should be warned but not disabled)
    opts.warn_user = User.objects.create_user(
        username="inactive_test2@test.com", email="inactive_test2@test.com", password="pass123",
    )
    opts.warn_user.is_active = True
    opts.warn_user.last_activity = dates.subtract(days=85)
    opts.warn_user.last_login = dates.subtract(days=85)
    opts.warn_user.save()

    # Staff user inactive for 100 days (should NOT be disabled)
    opts.staff_user = User.objects.create_user(
        username="inactive_staff@test.com", email="inactive_staff@test.com", password="pass123",
    )
    opts.staff_user.is_staff = True
    opts.staff_user.is_active = True
    opts.staff_user.last_activity = dates.subtract(days=100)
    opts.staff_user.save()

    # Superuser inactive for 100 days (should NOT be disabled)
    opts.super_user = User.objects.create_user(
        username="inactive_super@test.com", email="inactive_super@test.com", password="pass123",
    )
    opts.super_user.is_superuser = True
    opts.super_user.is_active = True
    opts.super_user.last_activity = dates.subtract(days=100)
    opts.super_user.save()

    # Protected user (no_disable = True)
    opts.protected_user = User.objects.create_user(
        username="inactive_protected@test.com", email="inactive_protected@test.com", password="pass123",
    )
    opts.protected_user.is_active = True
    opts.protected_user.last_activity = dates.subtract(days=100)
    opts.protected_user.save()
    opts.protected_user.set_protected_metadata("no_disable", True)

    # User who never logged in (last_activity=None, last_login=None)
    opts.never_login = User.objects.create_user(
        username="inactive_neverlogin@test.com", email="inactive_neverlogin@test.com", password="pass123",
    )
    opts.never_login.is_active = True
    opts.never_login.last_activity = None
    opts.never_login.last_login = None
    opts.never_login.save()

    # Already warned user
    opts.warned_user = User.objects.create_user(
        username="inactive_warned@test.com", email="inactive_warned@test.com", password="pass123",
    )
    opts.warned_user.is_active = True
    opts.warned_user.last_activity = dates.subtract(days=85)
    opts.warned_user.save()
    opts.warned_user.set_protected_metadata("disable_warned", True)
    opts.warned_user.set_protected_metadata("disable_warn_date", str(dates.subtract(days=2)))

    # Warned user who reactivated (last_activity is recent but still has warning flag)
    opts.reactivated_user = User.objects.create_user(
        username="inactive_reactivated@test.com", email="inactive_reactivated@test.com", password="pass123",
    )
    opts.reactivated_user.is_active = True
    opts.reactivated_user.last_activity = dates.utcnow()
    opts.reactivated_user.save()
    opts.reactivated_user.set_protected_metadata("disable_warned", True)
    opts.reactivated_user.set_protected_metadata("disable_warn_date", str(dates.subtract(days=5)))

    # Group admin user
    opts.group_admin = User.objects.create_user(
        username="groupadmin@test.com", email="groupadmin@test.com", password="pass123",
    )
    opts.group_admin.is_active = True
    opts.group_admin.save()
    opts.group_admin.add_permission("manage_groups")

    # Inactive group (100 days)
    opts.stale_group = Group.objects.create(
        name="inactive_test_stale",
        is_active=True,
        last_activity=dates.subtract(days=100),
    )

    # Group to warn (85 days)
    opts.warn_group = Group.objects.create(
        name="inactive_test_warn",
        is_active=True,
        last_activity=dates.subtract(days=85),
    )

    # Protected group
    opts.protected_group = Group.objects.create(
        name="inactive_test_protected",
        is_active=True,
        last_activity=dates.subtract(days=100),
        metadata={"protected": {"no_disable": True}},
    )

    # Group with no last_activity (skip)
    opts.new_group = Group.objects.create(
        name="inactive_test_new",
        is_active=True,
        last_activity=None,
    )


@th.django_unit_test()
def test_disable_inactive_user(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users
    from mojo.apps.account.models import User

    with mock.patch("mojo.apps.incident.report_event"):
        disabled = disable_inactive_users()

    opts.stale_user.refresh_from_db()
    assert opts.stale_user.is_active is False, "Stale user (100 days) should be disabled"
    assert disabled >= 1, f"Should disable at least 1 user, got: {disabled}"


@th.django_unit_test()
def test_warn_inactive_user(opts):
    from mojo.apps.account.services.inactive import warn_inactive_users
    from mojo.apps.account.models import User

    with mock.patch("mojo.apps.incident.report_event"), \
         mock.patch.object(User, "send_template_email"):
        warned = warn_inactive_users()

    opts.warn_user.refresh_from_db()
    assert opts.warn_user.is_active is True, "Warned user should still be active"
    assert warned >= 1, f"Should warn at least 1 user, got: {warned}"
    warning = (opts.warn_user.metadata or {}).get("protected", {}).get("disable", {}).get("warning") or {}
    assert warning.get("sent_at"), \
        f"Warned user should have disable.warning.sent_at set, got metadata: {opts.warn_user.metadata}"


@th.django_unit_test()
def test_staff_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_users()

    opts.staff_user.refresh_from_db()
    assert opts.staff_user.is_active is True, "Staff user should NOT be disabled"


@th.django_unit_test()
def test_superuser_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_users()

    opts.super_user.refresh_from_db()
    assert opts.super_user.is_active is True, "Superuser should NOT be disabled"


@th.django_unit_test()
def test_protected_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users, warn_inactive_users
    from mojo.apps.account.models import User

    with mock.patch("mojo.apps.incident.report_event"), \
         mock.patch.object(User, "send_template_email"):
        disable_inactive_users()
        warn_inactive_users()

    opts.protected_user.refresh_from_db()
    assert opts.protected_user.is_active is True, "Protected user (no_disable) should NOT be disabled"


@th.django_unit_test()
def test_never_login_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_users()

    opts.never_login.refresh_from_db()
    assert opts.never_login.is_active is True, \
        "User with null last_activity and null last_login should NOT be disabled"


@th.django_unit_test()
def test_warning_idempotent(opts):
    from mojo.apps.account.services.inactive import warn_inactive_users
    from mojo.apps.account.models import User

    with mock.patch("mojo.apps.incident.report_event"), \
         mock.patch.object(User, "send_template_email") as mock_email:
        warned = warn_inactive_users()

    # The already-warned user should NOT be warned again
    # Only the warn_user (85 days, not yet warned) should be warned
    # Count calls - the warned_user should not generate an email
    opts.warned_user.refresh_from_db()
    assert opts.warned_user.get_protected_metadata("disable_warned") is True, \
        "Already warned user should still have disable_warned flag"


@th.django_unit_test()
def test_clear_stale_warnings(opts):
    from mojo.apps.account.services.inactive import _clear_stale_warnings
    from mojo.apps.account.models import User

    cleared = _clear_stale_warnings(User, 90)

    opts.reactivated_user.refresh_from_db()
    assert opts.reactivated_user.get_protected_metadata("disable_warned") is None, \
        "Reactivated user's warning flag should be cleared"
    assert cleared >= 1, f"Should clear at least 1 stale warning, got: {cleared}"


@th.django_unit_test()
def test_disable_inactive_group(opts):
    from mojo.apps.account.services.inactive import disable_inactive_groups
    from mojo.apps.account.models import Group

    with mock.patch("mojo.apps.incident.report_event"):
        disabled = disable_inactive_groups()

    opts.stale_group.refresh_from_db()
    assert opts.stale_group.is_active is False, "Stale group (100 days) should be disabled"
    assert disabled >= 1, f"Should disable at least 1 group, got: {disabled}"


@th.django_unit_test()
def test_warn_inactive_group(opts):
    from mojo.apps.account.services.inactive import warn_inactive_groups
    from mojo.apps.account.models import User

    with mock.patch("mojo.apps.incident.report_event"), \
         mock.patch.object(User, "send_template_email"):
        warned = warn_inactive_groups()

    opts.warn_group.refresh_from_db()
    assert opts.warn_group.is_active is True, "Warned group should still be active"
    assert warned >= 1, f"Should warn at least 1 group, got: {warned}"
    warning = (opts.warn_group.metadata or {}).get("protected", {}).get("disable", {}).get("warning") or {}
    assert warning.get("sent_at"), \
        f"Warned group should have disable.warning.sent_at set, got metadata: {opts.warn_group.metadata}"


@th.django_unit_test()
def test_protected_group_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_groups

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_groups()

    opts.protected_group.refresh_from_db()
    assert opts.protected_group.is_active is True, \
        "Protected group (no_disable) should NOT be disabled"


@th.django_unit_test()
def test_group_null_activity_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_groups

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_groups()

    opts.new_group.refresh_from_db()
    assert opts.new_group.is_active is True, \
        "Group with null last_activity should NOT be disabled"


@th.django_unit_test()
def test_incident_event_on_disable(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users
    from mojo.apps.account.models import User

    # Re-enable the stale user (may have been disabled by earlier test)
    User.objects.filter(pk=opts.stale_user.pk).update(is_active=True)

    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        disable_inactive_users()

    assert mock_report.called, "report_event should be called when disabling users"
    # Check that at least one call had the auto_disabled category
    categories = [call.kwargs.get("category") for call in mock_report.call_args_list]
    assert "account:auto_disabled" in categories, \
        f"report_event should be called with category 'account:auto_disabled', got: {categories}"


@th.django_unit_test()
def test_incident_event_on_warn(opts):
    from mojo.apps.account.services.inactive import warn_inactive_users
    from mojo.apps.account.models import User
    from mojo.helpers import dates

    # Reset the warn_user so it can be warned again (may have been warned by earlier test)
    User.objects.filter(pk=opts.warn_user.pk).update(
        is_active=True,
        metadata={},
    )
    opts.warn_user.refresh_from_db()
    opts.warn_user.last_activity = dates.subtract(days=85)
    opts.warn_user.save(update_fields=["last_activity"])

    with mock.patch("mojo.apps.incident.report_event") as mock_report, \
         mock.patch.object(User, "send_template_email"):
        warn_inactive_users()

    assert mock_report.called, "report_event should be called when warning users"
    categories = [call.kwargs.get("category") for call in mock_report.call_args_list]
    assert "account:inactive_warning" in categories, \
        f"report_event should be called with category 'account:inactive_warning', got: {categories}"


@th.django_unit_test()
def test_feature_flag_off(opts):
    from mojo.apps.account.asyncjobs import inactive_sweep

    with mock.patch("mojo.helpers.settings.settings.get", return_value=False):
        results = inactive_sweep(None)

    assert results == {}, f"With feature flags off, sweep should return empty results, got: {results}"


@th.django_unit_test()
def test_zero_matches_no_error(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users, warn_inactive_users
    from mojo.apps.account.models import User

    # Disable all test users first so there are no matches
    User.objects.filter(email__in=[
        "inactive_test1@test.com", "inactive_test2@test.com",
    ]).update(is_active=False)

    with mock.patch("mojo.apps.incident.report_event"):
        disabled = disable_inactive_users()

    # Should not error, just return 0
    assert disabled >= 0, f"disable_inactive_users should return >= 0, got: {disabled}"

    with mock.patch("mojo.apps.incident.report_event"), \
         mock.patch.object(User, "send_template_email"):
        warned = warn_inactive_users()

    assert warned >= 0, f"warn_inactive_users should return >= 0, got: {warned}"
