"""Tests for the User POST_SAVE_ACTIONS that mirror /api/auth/* dedicated endpoints.

Five actions added alongside the existing dedicated endpoints (legacy routes
remain for back-compat):

  change_username        — mirrors POST /api/auth/username/change
  revoke_sessions        — mirrors POST /api/auth/sessions/revoke
  confirm_totp           — mirrors POST /api/account/totp/confirm
  regenerate_totp_codes  — mirrors POST /api/account/totp/recovery-codes/regenerate
  disable_totp           — mirrors DELETE /api/account/totp
"""
from unittest import mock
from testit import helpers as th


USERNAME = "user_actions_self@test.com"
PASSWORD = "user_actions_pw_99"
OTHER_USERNAME = "user_actions_other@test.com"
RENAMED_USERNAME = "user_actions_renamed"  # no @ — validate_username rejects @ that doesn't match email


@th.django_unit_setup()
def setup_user_actions(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP

    User.objects.filter(email__in=[USERNAME, OTHER_USERNAME]).delete()
    User.objects.filter(username=RENAMED_USERNAME).delete()

    user = User.objects.create_user(username=USERNAME, email=USERNAME, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    UserTOTP.objects.filter(user=user).delete()
    opts.user_id = user.pk
    opts.user_auth_key = user.get_auth_key()

    other = User.objects.create_user(username=OTHER_USERNAME, email=OTHER_USERNAME, password="other_pw_99")
    other.is_active = True
    other.is_email_verified = True
    other.save()
    other.add_permission("manage_users")
    opts.other_id = other.pk


@th.django_unit_test()
def test_change_username_via_action(opts):
    from mojo.apps.account.models import User

    User.objects.filter(username=RENAMED_USERNAME).delete()

    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"change_username": {"username": RENAMED_USERNAME, "current_password": PASSWORD}},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"change_username action should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    user = User.objects.get(pk=opts.user_id)
    assert user.username == RENAMED_USERNAME, \
        f"username should be updated, got {user.username}"

    # Restore for later tests
    User.objects.filter(pk=opts.user_id).update(username=USERNAME)


@th.django_unit_test()
def test_change_username_wrong_password_rejected(opts):
    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"change_username": {"username": "rejected_x@test.com", "current_password": "wrong_pw"}},
    )
    opts.client.logout()

    assert resp.status_code != 200, \
        f"wrong current_password should be rejected, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test()
def test_change_username_admin_cannot_act_on_other(opts):
    """An admin acting on someone else's user record cannot trigger change_username."""
    from mojo.apps.account.models import User

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"change_username": {"username": "admin_attempted@test.com", "current_password": "other_pw_99"}},
    )
    opts.client.logout()

    assert resp.status_code != 200, \
        f"admin acting on another user should be rejected, got {resp.status_code}"
    user = User.objects.get(pk=opts.user_id)
    assert user.username == USERNAME, "target username should be unchanged"


@th.django_unit_test()
def test_revoke_sessions_via_action(opts):
    from mojo.apps.account.models import User

    pre_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()

    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"revoke_sessions": {"current_password": PASSWORD}},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"revoke_sessions should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    post_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    assert post_auth_key != pre_auth_key, \
        "auth_key should be rotated after revoke_sessions"


@th.django_unit_test()
def test_revoke_sessions_wrong_password_rejected(opts):
    from mojo.apps.account.models import User

    pre_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()

    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"revoke_sessions": {"current_password": "wrong_pw"}},
    )
    opts.client.logout()

    assert resp.status_code != 200, \
        f"wrong current_password should reject revoke_sessions, got {resp.status_code}"
    post_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    assert post_auth_key == pre_auth_key, \
        "auth_key should NOT be rotated when password is wrong"


@th.django_unit_test()
def test_confirm_totp_via_action(opts):
    import pyotp
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.apps.account.services import totp as totp_service

    UserTOTP.objects.filter(user_id=opts.user_id).delete()
    secret = totp_service.generate_secret()
    totp = UserTOTP.objects.create(user_id=opts.user_id, is_enabled=False)
    totp.set_secret("totp_secret", secret)
    totp.save()

    code = pyotp.TOTP(secret).now()

    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"confirm_totp": {"code": code}},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"confirm_totp action should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    totp.refresh_from_db()
    assert totp.is_enabled is True, "TOTP should be enabled after confirm"
    user = User.objects.get(pk=opts.user_id)
    assert user.requires_mfa is True, "requires_mfa should be set after TOTP confirm"

    # Reset for downstream tests
    UserTOTP.objects.filter(user_id=opts.user_id).delete()
    User.objects.filter(pk=opts.user_id).update(requires_mfa=False)


@th.django_unit_test()
def test_confirm_totp_invalid_code_rejected(opts):
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.apps.account.services import totp as totp_service

    UserTOTP.objects.filter(user_id=opts.user_id).delete()
    secret = totp_service.generate_secret()
    totp = UserTOTP.objects.create(user_id=opts.user_id, is_enabled=False)
    totp.set_secret("totp_secret", secret)
    totp.save()

    # Non-numeric / wrong-length code is unconditionally rejected by pyotp.verify.
    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"confirm_totp": {"code": "not-a-real-code"}},
    )
    opts.client.logout()

    assert resp.status_code != 200, \
        f"invalid TOTP code should be rejected, got {resp.status_code}"
    totp.refresh_from_db()
    assert totp.is_enabled is False, "TOTP should NOT be enabled after invalid code"


@th.django_unit_test()
def test_regenerate_totp_codes_via_action(opts):
    import pyotp
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.apps.account.services import totp as totp_service

    UserTOTP.objects.filter(user_id=opts.user_id).delete()
    secret = totp_service.generate_secret()
    totp = UserTOTP.objects.create(user_id=opts.user_id, is_enabled=True)
    totp.set_secret("totp_secret", secret)
    totp.save()
    initial_codes = totp.generate_recovery_codes()
    code = pyotp.TOTP(secret).now()

    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"regenerate_totp_codes": {"code": code}},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"regenerate_totp_codes action should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    body = resp.response
    new_codes = body.data["recovery_codes"] if body and body.data else None
    assert isinstance(new_codes, list) and len(new_codes) == 8, \
        f"should return 8 new recovery codes, got: {new_codes!r}"
    assert set(new_codes) != set(initial_codes), \
        "new codes should be different from initial set"


@th.django_unit_test()
def test_disable_totp_via_action(opts):
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.apps.account.services import totp as totp_service

    UserTOTP.objects.filter(user_id=opts.user_id).delete()
    secret = totp_service.generate_secret()
    totp = UserTOTP.objects.create(user_id=opts.user_id, is_enabled=True)
    totp.set_secret("totp_secret", secret)
    totp.save()

    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"disable_totp": True},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"disable_totp action should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    totp.refresh_from_db()
    assert totp.is_enabled is False, "TOTP should be disabled after action"


@th.django_unit_test()
def test_self_service_actions_reject_admin_on_other(opts):
    """All five self-service actions must reject an admin acting on a different user."""
    from mojo.apps.account.models import User

    cases = [
        {"change_username": {"username": "admin_blocked@test.com", "current_password": "other_pw_99"}},
        {"revoke_sessions": {"current_password": "other_pw_99"}},
        {"confirm_totp": {"code": "000000"}},
        {"regenerate_totp_codes": {"code": "000000"}},
        {"disable_totp": True},
    ]

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    for body in cases:
        resp = opts.client.post(f"/api/user/{opts.user_id}", body)
        action_name = next(iter(body.keys()))
        assert resp.status_code != 200, \
            f"admin acting on another user should be rejected for {action_name}, got {resp.status_code}"
    opts.client.logout()

    user = User.objects.get(pk=opts.user_id)
    assert user.username == USERNAME, "target username should remain unchanged after admin attempts"
