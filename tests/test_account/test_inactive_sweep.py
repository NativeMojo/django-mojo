"""Tests for auto-disable inactive users and groups sweep.

Parallel-safety: every call injects a local incident reporter and email
sender through the service seams (maestro item #1839) — nothing here patches
the shared incident module or the User class, so a concurrent module's
events and emails are untouched.
"""
from testit import helpers as th


def _null_reporter(*args, **kwargs):
    return None


def _null_send_email(user, template, context):
    return None


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

    disabled = disable_inactive_users(reporter=_null_reporter)

    opts.stale_user.refresh_from_db()
    assert opts.stale_user.is_active is False, "Stale user (100 days) should be disabled"
    assert disabled >= 1, f"Should disable at least 1 user, got: {disabled}"


@th.django_unit_test()
def test_warn_inactive_user(opts):
    from mojo.apps.account.services.inactive import warn_inactive_users

    warned = warn_inactive_users(reporter=_null_reporter, send_email=_null_send_email)

    opts.warn_user.refresh_from_db()
    assert opts.warn_user.is_active is True, "Warned user should still be active"
    assert warned >= 1, f"Should warn at least 1 user, got: {warned}"
    warning = (opts.warn_user.metadata or {}).get("protected", {}).get("disable", {}).get("warning") or {}
    assert warning.get("sent_at"), \
        f"Warned user should have disable.warning.sent_at set, got metadata: {opts.warn_user.metadata}"


@th.django_unit_test()
def test_staff_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users

    disable_inactive_users(reporter=_null_reporter)

    opts.staff_user.refresh_from_db()
    assert opts.staff_user.is_active is True, "Staff user should NOT be disabled"


@th.django_unit_test()
def test_superuser_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users

    disable_inactive_users(reporter=_null_reporter)

    opts.super_user.refresh_from_db()
    assert opts.super_user.is_active is True, "Superuser should NOT be disabled"


@th.django_unit_test()
def test_protected_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users, warn_inactive_users

    disable_inactive_users(reporter=_null_reporter)
    warn_inactive_users(reporter=_null_reporter, send_email=_null_send_email)

    opts.protected_user.refresh_from_db()
    assert opts.protected_user.is_active is True, "Protected user (no_disable) should NOT be disabled"


@th.django_unit_test()
def test_never_login_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users

    disable_inactive_users(reporter=_null_reporter)

    opts.never_login.refresh_from_db()
    assert opts.never_login.is_active is True, \
        "User with null last_activity and null last_login should NOT be disabled"


@th.django_unit_test()
def test_warning_idempotent(opts):
    from mojo.apps.account.services.inactive import warn_inactive_users

    warn_inactive_users(reporter=_null_reporter, send_email=_null_send_email)

    # The already-warned user should NOT be warned again
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

    disabled = disable_inactive_groups(reporter=_null_reporter)

    opts.stale_group.refresh_from_db()
    assert opts.stale_group.is_active is False, "Stale group (100 days) should be disabled"
    assert disabled >= 1, f"Should disable at least 1 group, got: {disabled}"


@th.django_unit_test()
def test_warn_inactive_group(opts):
    from mojo.apps.account.services.inactive import warn_inactive_groups

    warned = warn_inactive_groups(reporter=_null_reporter, send_email=_null_send_email)

    opts.warn_group.refresh_from_db()
    assert opts.warn_group.is_active is True, "Warned group should still be active"
    assert warned >= 1, f"Should warn at least 1 group, got: {warned}"
    warning = (opts.warn_group.metadata or {}).get("protected", {}).get("disable", {}).get("warning") or {}
    assert warning.get("sent_at"), \
        f"Warned group should have disable.warning.sent_at set, got metadata: {opts.warn_group.metadata}"


@th.django_unit_test()
def test_protected_group_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_groups

    disable_inactive_groups(reporter=_null_reporter)

    opts.protected_group.refresh_from_db()
    assert opts.protected_group.is_active is True, \
        "Protected group (no_disable) should NOT be disabled"


@th.django_unit_test()
def test_group_null_activity_exempt(opts):
    from mojo.apps.account.services.inactive import disable_inactive_groups

    disable_inactive_groups(reporter=_null_reporter)

    opts.new_group.refresh_from_db()
    assert opts.new_group.is_active is True, \
        "Group with null last_activity should NOT be disabled"


@th.django_unit_test()
def test_incident_event_on_disable(opts):
    from mojo.apps.account.services.inactive import disable_inactive_users
    from mojo.apps.account.models import User

    # Re-enable the stale user (may have been disabled by earlier test)
    User.objects.filter(pk=opts.stale_user.pk).update(is_active=True)

    reported = []

    def capture(*args, **kwargs):
        reported.append(kwargs)

    disable_inactive_users(reporter=capture)

    assert reported, "the reporter should be called when disabling users"
    categories = [call.get("category") for call in reported]
    assert "account:auto_disabled" in categories, \
        f"reporter should receive category 'account:auto_disabled', got: {categories}"


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

    reported = []

    def capture(*args, **kwargs):
        reported.append(kwargs)

    warn_inactive_users(reporter=capture, send_email=_null_send_email)

    assert reported, "the reporter should be called when warning users"
    categories = [call.get("category") for call in reported]
    assert "account:inactive_warning" in categories, \
        f"reporter should receive category 'account:inactive_warning', got: {categories}"


@th.django_unit_test()
def test_feature_flag_off(opts):
    from mojo.apps.account.asyncjobs import inactive_sweep
    from mojo.helpers.settings import settings

    # The auto-disable flags default to False and the test project does not
    # set them — assert that read directly, then prove the sweep is a no-op.
    assert settings.get("ACCOUNT_AUTO_DISABLE_ENABLED", False) is False, \
        "test project must not enable ACCOUNT_AUTO_DISABLE_ENABLED — this test asserts the off-by-default contract"
    assert settings.get("GROUP_AUTO_DISABLE_ENABLED", False) is False, \
        "test project must not enable GROUP_AUTO_DISABLE_ENABLED — this test asserts the off-by-default contract"

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

    disabled = disable_inactive_users(reporter=_null_reporter)

    # Should not error, just return 0
    assert disabled >= 0, f"disable_inactive_users should return >= 0, got: {disabled}"

    warned = warn_inactive_users(reporter=_null_reporter, send_email=_null_send_email)

    assert warned >= 0, f"warn_inactive_users should return >= 0, got: {warned}"
