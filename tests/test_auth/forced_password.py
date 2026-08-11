"""Forced temporary-password state and single-use credential regressions."""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


EMAIL = "forced-password@test.com"
PASSWORD = "Initial-forced-password-19!"
ADMIN_EMAIL = "forced-password-admin@test.com"
ADMIN_PASSWORD = "Forced-password-admin-49!"
DELEGATE_EMAIL = "forced-password-delegate@test.com"


@th.django_unit_setup()
def setup_forced_password(opts):
    from mojo.apps.account.models import User

    User.objects.filter(
        email__in=[EMAIL, ADMIN_EMAIL, DELEGATE_EMAIL]).delete()
    user = User.objects.create_user(username=EMAIL, email=EMAIL, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.save()
    opts.user_id = user.pk
    admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL,
        password=ADMIN_PASSWORD)
    admin.is_active = True
    admin.is_superuser = True
    admin.save()
    opts.admin_id = admin.pk


@th.django_unit_test("administrator temporary password rotates sessions and reveals once")
def test_admin_temporary_password_semantics(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import admin_passwords
    from mojo.apps.account.services.disable import disconnect_realtime as real_disconnect

    user = User.objects.get(pk=opts.user_id)
    admin = User.objects.get(pk=opts.admin_id)
    old_auth_key = user.get_auth_key()
    request = SimpleNamespace(user=admin)
    target_disconnects = []

    def capture_target_disconnect(entity, request=None):
        if entity.pk == user.pk:
            target_disconnects.append((entity.pk, request))
            return None
        return real_disconnect(entity, request=request)

    with mock.patch(
            "mojo.apps.account.services.disable.disconnect_realtime",
            side_effect=capture_target_disconnect):
        result = admin_passwords.issue_temporary_password(request, user.pk)
    user.refresh_from_db()
    temporary = result.get("temporary_password")
    assert temporary and user.check_password(temporary), \
        "the generated temporary password must be real and returned only by the service result"
    assert user.requires_password_change is True, \
        "temporary issuance must persist forced-change state"
    assert user.auth_key != old_auth_key, \
        "temporary issuance must invalidate all existing JWTs"
    assert target_disconnects == [(user.pk, request)], \
        "temporary issuance must disconnect the target exactly once"


@th.django_unit_test("admin password recovery refuses a superuser target from a non-superuser caller")
def test_admin_password_recovery_refuses_superuser_target(opts):
    """A delegated (manage_users) admin must not recover a superuser account.

    Regression for the privilege-escalation where issuing a temporary password
    (revealed to the caller) or a reset link for a superuser let a subordinate
    admin seize a session more privileged than their own.
    """
    from mojo import errors as merrors
    from mojo.apps.account.models import User
    from mojo.apps.account.services import admin_passwords

    superuser = User.objects.get(pk=opts.admin_id)
    victim_auth_key = superuser.get_auth_key()

    User.objects.filter(email=DELEGATE_EMAIL).delete()
    delegate = User.objects.create_user(
        username=DELEGATE_EMAIL, email=DELEGATE_EMAIL,
        password="Delegate-admin-59!")
    delegate.is_active = True
    delegate.is_superuser = False
    delegate.permissions = {"manage_users": True}
    delegate.save()
    request = SimpleNamespace(user=delegate)

    for label, action in (
            ("temporary password", admin_passwords.issue_temporary_password),
            ("reset link", admin_passwords.send_reset_link)):
        try:
            action(request, superuser.pk)
        except merrors.PermissionDeniedException:
            pass
        else:
            assert False, \
                f"a non-superuser admin must be refused when issuing a {label} for a superuser"

    superuser.refresh_from_db()
    assert superuser.get_auth_key() == victim_auth_key, \
        "a refused recovery must not rotate the superuser's auth key (no session takeover)"
    assert superuser.requires_password_change is False, \
        "a refused recovery must not force a password change on the superuser"
    assert superuser.check_password(ADMIN_PASSWORD), \
        "a refused recovery must leave the superuser's password intact"


@th.django_unit_test("temporary-password credentials are short-lived, typed, and single use")
def test_forced_password_token_single_use(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    user.requires_password_change = True
    user.save(update_fields=["requires_password_change", "modified"])
    token = tokens.generate_forced_password_token(user)
    assert token.startswith("tp:"), f"forced credential must use tp: prefix, got {token[:3]!r}"
    first = tokens.verify_forced_password_token(token)
    assert first.pk == user.pk, "first forced-password credential use must resolve its user"
    try:
        tokens.verify_forced_password_token(token)
    except merrors.ValueException:
        pass
    else:
        assert False, "a consumed forced-password credential must not verify twice"


@th.django_unit_test("only a validated permanent password clears forced state")
def test_permanent_password_clears_forced_state(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    user.requires_password_change = True
    user.set_password("Another-temporary-password-29!")
    user.save(update_fields=["password", "requires_password_change", "modified"])
    user.refresh_from_db()
    assert user.requires_password_change is True, \
        "ordinary password hashing must preserve the server-managed forced flag"

    user.set_permanent_password("A-new-permanent-password-39!")
    user.save(update_fields=["password", "requires_password_change", "modified"])
    user.refresh_from_db()
    assert user.requires_password_change is False, \
        "validated persisted permanent password must clear the forced flag"
    assert user.check_password("A-new-permanent-password-39!"), \
        "the permanent replacement password must be persisted"


@th.django_unit_test("all token issuance paths stop at forced-password response")
def test_forced_password_auth_choke_points_are_frozen(opts):
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2]
              / "mojo/apps/account/rest/user.py").read_text()
    assert "if user.requires_password_change:\n        return forced_password_response(user)" in source, \
        "JWT and refresh issuance must stop at the forced-password response"
    assert "if user.requires_password_change:\n        return jwt_login(" in source, \
        "password login must bypass MFA token issuance and enter the forced-password choke point"
    assert "if user.requires_password_change:\n        return forced_password_response(user)" in source[source.index("def group_token_login"):], \
        "group-token login must refuse token minting while forced change is pending"
    assert '@md.POST("auth/password/forced")' in source, \
        "the dedicated forced-password completion endpoint must remain registered"
