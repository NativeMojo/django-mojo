"""Tests for the User POST_SAVE_ACTIONS that mirror /api/auth/* dedicated endpoints.

Five actions added alongside the existing dedicated endpoints (legacy routes
remain for back-compat):

  change_username        — mirrors POST /api/auth/username/change
  revoke_sessions        — mirrors POST /api/auth/sessions/revoke
  confirm_totp           — mirrors POST /api/account/totp/confirm
  regenerate_totp_codes  — mirrors POST /api/account/totp/recovery-codes/regenerate
  disable_totp           — mirrors DELETE /api/account/totp

Authorization is the model's normal SAVE security — the record owner acting on
self, or an admin with `users`/`manage_users`. There is no `current_password`
gate: passwordless accounts (passkey / SMS-OTP) have no password to verify, and
the authenticated request already proves identity.
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
        {"change_username": {"username": RENAMED_USERNAME}},
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
def test_change_username_admin_can_act_on_other(opts):
    """An admin (manage_users) may change another user's username via the action."""
    from mojo.apps.account.models import User

    admin_set_username = "user_actions_byother"  # no @ — validate_username rejects @ that isn't the email
    User.objects.filter(username=admin_set_username).delete()

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"change_username": {"username": admin_set_username}},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"admin should be able to change another user's username, got {resp.status_code}: {opts.client.last_response.body}"
    user = User.objects.get(pk=opts.user_id)
    assert user.username == admin_set_username, \
        f"target username should be updated by admin, got {user.username}"

    # Restore for later tests
    User.objects.filter(pk=opts.user_id).update(username=USERNAME)


@th.django_unit_test()
def test_revoke_sessions_via_action(opts):
    from mojo.apps.account.models import User

    pre_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()

    assert opts.client.login(USERNAME, PASSWORD), "self login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"revoke_sessions": True},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"revoke_sessions should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    post_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    assert post_auth_key != pre_auth_key, \
        "auth_key should be rotated after revoke_sessions"


@th.django_unit_test()
def test_revoke_sessions_admin_can_act_on_other(opts):
    """An admin (manage_users) may force-revoke another user's sessions."""
    from mojo.apps.account.models import User

    pre_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"revoke_sessions": True},
    )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"admin should be able to revoke another user's sessions, got {resp.status_code}: {opts.client.last_response.body}"
    post_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    assert post_auth_key != pre_auth_key, \
        "target auth_key should be rotated by admin revoke"


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
def test_admin_can_change_email_on_other_user(opts):
    """Admin with manage_users can change another user's email via direct field write."""
    from mojo.apps.account.models import User

    new_email = "user_actions_renamed_email@test.com"
    User.objects.filter(email=new_email).delete()

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", {"email": new_email})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"manage_users admin should be able to change email, got {resp.status_code}: {opts.client.last_response.body}"
    user = User.objects.get(pk=opts.user_id)
    assert user.email == new_email, f"email should be updated, got {user.email}"

    # Restore for downstream tests
    User.objects.filter(pk=opts.user_id).update(email=USERNAME, username=USERNAME)


@th.django_unit_test()
def test_users_perm_can_change_email_on_other_user(opts):
    """A caller with the `users` domain category perm (no manage_users) can change credentials."""
    from mojo.apps.account.models import User

    User.objects.filter(email__in=["user_actions_users_perm@test.com"]).delete()
    bare_admin = User.objects.create_user(
        username="user_actions_users_perm@test.com",
        email="user_actions_users_perm@test.com",
        password="bare_pw_99")
    bare_admin.is_active = True
    bare_admin.is_email_verified = True
    bare_admin.save()
    bare_admin.add_permission("users")
    # explicitly NOT manage_users

    new_email = "user_actions_users_perm_renamed@test.com"
    User.objects.filter(email=new_email).delete()

    assert opts.client.login("user_actions_users_perm@test.com", "bare_pw_99"), "bare admin login failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", {"email": new_email})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"caller with `users` perm should be able to change credentials, got {resp.status_code}: {opts.client.last_response.body}"

    # Restore + cleanup
    User.objects.filter(pk=opts.user_id).update(email=USERNAME, username=USERNAME)
    User.objects.filter(pk=bare_admin.pk).delete()


@th.django_unit_test()
def test_owner_only_cannot_direct_change_email(opts):
    """A self-acting user without users/manage_users perm cannot direct-change email — must use change flow."""
    from mojo.apps.account.models import User

    User.objects.filter(email__in=["user_actions_owner_only@test.com"]).delete()
    plain_user = User.objects.create_user(
        username="user_actions_owner_only@test.com",
        email="user_actions_owner_only@test.com",
        password="plain_pw_99")
    plain_user.is_active = True
    plain_user.is_email_verified = True
    plain_user.save()
    # No admin perms — only owner-via-self-acting

    assert opts.client.login("user_actions_owner_only@test.com", "plain_pw_99"), "plain user login failed"
    resp = opts.client.post(
        f"/api/user/{plain_user.pk}",
        {"email": "user_actions_owner_only_renamed@test.com"})
    opts.client.logout()

    assert resp.status_code != 200, \
        f"owner-only self-acting user should be blocked from direct email change, got {resp.status_code}"

    User.objects.filter(pk=plain_user.pk).delete()


@th.django_unit_test()
def test_admin_can_force_verify_email_on_other_user(opts):
    """Admin with manage_users can flip is_email_verified on another user."""
    from mojo.apps.account.models import User

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", {"is_email_verified": True})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"manage_users admin should be able to force-verify email, got {resp.status_code}: {opts.client.last_response.body}"
    user = User.objects.get(pk=opts.user_id)
    assert user.is_email_verified is True, "is_email_verified should be flipped"


@th.django_unit_test()
def test_users_perm_can_force_verify_phone_on_other_user(opts):
    """A caller with `users` perm (not manage_users) can flip is_phone_verified."""
    from mojo.apps.account.models import User

    User.objects.filter(email__in=["user_actions_phone_admin@test.com"]).delete()
    bare_admin = User.objects.create_user(
        username="user_actions_phone_admin@test.com",
        email="user_actions_phone_admin@test.com",
        password="bare_pw_99")
    bare_admin.is_active = True
    bare_admin.save()
    bare_admin.add_permission("users")

    User.objects.filter(pk=opts.user_id).update(is_phone_verified=False)

    assert opts.client.login("user_actions_phone_admin@test.com", "bare_pw_99"), "bare admin login failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", {"is_phone_verified": True})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"caller with `users` perm should be able to force-verify phone, got {resp.status_code}: {opts.client.last_response.body}"
    user = User.objects.get(pk=opts.user_id)
    assert user.is_phone_verified is True, "is_phone_verified should be flipped"

    User.objects.filter(pk=bare_admin.pk).delete()


@th.django_unit_test()
def test_owner_only_cannot_force_verify_email(opts):
    """A self-acting user without admin perms cannot flip is_email_verified."""
    from mojo.apps.account.models import User

    User.objects.filter(email__in=["user_actions_owner_verify@test.com"]).delete()
    plain_user = User.objects.create_user(
        username="user_actions_owner_verify@test.com",
        email="user_actions_owner_verify@test.com",
        password="plain_pw_99")
    plain_user.is_active = True
    plain_user.is_email_verified = False
    plain_user.save()

    assert opts.client.login("user_actions_owner_verify@test.com", "plain_pw_99"), "plain user login failed"
    resp = opts.client.post(f"/api/user/{plain_user.pk}", {"is_email_verified": True})
    opts.client.logout()

    assert resp.status_code != 200, \
        f"owner-only self-acting user should be blocked from force-verify, got {resp.status_code}"
    user = User.objects.get(pk=plain_user.pk)
    assert user.is_email_verified is False, "is_email_verified should NOT be flipped by owner-only caller"

    User.objects.filter(pk=plain_user.pk).delete()


@th.django_unit_test()
def test_admin_can_flip_requires_mfa(opts):
    """requires_mfa is admin-tier (users / manage_users / superuser), not superuser-only."""
    from mojo.apps.account.models import User

    User.objects.filter(pk=opts.user_id).update(requires_mfa=False)

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", {"requires_mfa": True})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"manage_users admin should be able to flip requires_mfa, got {resp.status_code}: {opts.client.last_response.body}"
    user = User.objects.get(pk=opts.user_id)
    assert user.requires_mfa is True, "requires_mfa should be flipped"

    User.objects.filter(pk=opts.user_id).update(requires_mfa=False)


@th.django_unit_test()
def test_is_dob_verified_silently_ignored_for_non_superuser(opts):
    """is_dob_verified is in NO_SAVE_FIELDS — silently ignored for everyone via REST.

    The SUPERUSER_ONLY_FIELDS gate is a defense-in-depth backstop; the primary
    guarantee comes from NO_SAVE_FIELDS dropping the field before it reaches
    the save path. So the request succeeds with a 200 but the field doesn't
    change.
    """
    from mojo.apps.account.models import User

    User.objects.filter(pk=opts.user_id).update(is_dob_verified=False)

    assert opts.client.login(OTHER_USERNAME, "other_pw_99"), "admin login failed"
    resp = opts.client.post(f"/api/user/{opts.user_id}", {"is_dob_verified": True})
    opts.client.logout()

    user = User.objects.get(pk=opts.user_id)
    assert user.is_dob_verified is False, \
        f"is_dob_verified should be silently ignored (NO_SAVE_FIELDS), got: {user.is_dob_verified}"


@th.django_unit_test()
def test_users_perm_can_set_password_on_other_user(opts):
    """A caller with `users` perm can set a new_password on another user."""
    from mojo.apps.account.models import User

    User.objects.filter(email__in=["user_actions_pw_admin@test.com", "user_actions_pw_target@test.com"]).delete()
    bare_admin = User.objects.create_user(
        username="user_actions_pw_admin@test.com",
        email="user_actions_pw_admin@test.com",
        password="bare_pw_99")
    bare_admin.is_active = True
    bare_admin.save()
    bare_admin.add_permission("users")

    target = User.objects.create_user(
        username="user_actions_pw_target@test.com",
        email="user_actions_pw_target@test.com",
        password="initial_pw_99")
    target.is_active = True
    target.is_email_verified = True
    target.save()

    new_password = "ResetByAdmin##99"

    assert opts.client.login("user_actions_pw_admin@test.com", "bare_pw_99"), "bare admin login failed"
    resp = opts.client.post(f"/api/user/{target.pk}", {"new_password": new_password})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"caller with `users` perm should be able to set new_password, got {resp.status_code}: {opts.client.last_response.body}"
    target.refresh_from_db()
    assert target.check_password(new_password), \
        "target user's password should be the new one set by admin"

    User.objects.filter(pk__in=[bare_admin.pk, target.pk]).delete()


@th.django_unit_test()
def test_users_perm_can_disable_other_user(opts):
    """`users` perm (no manage_users) is now equivalent — can disable / reactivate."""
    from unittest import mock as _mock
    from mojo.apps.account.models import User

    User.objects.filter(email__in=["user_actions_disable_admin@test.com", "user_actions_disable_target@test.com"]).delete()
    bare_admin = User.objects.create_user(
        username="user_actions_disable_admin@test.com",
        email="user_actions_disable_admin@test.com",
        password="bare_pw_99")
    bare_admin.is_active = True
    bare_admin.save()
    bare_admin.add_permission("users")

    target = User.objects.create_user(
        username="user_actions_disable_target@test.com",
        email="user_actions_disable_target@test.com",
        password="target_pw_99")
    target.is_active = True
    target.metadata = {}
    target.save()

    assert opts.client.login("user_actions_disable_admin@test.com", "bare_pw_99"), "bare admin login failed"
    with _mock.patch("mojo.apps.incident.report_event"):
        resp = opts.client.post(f"/api/user/{target.pk}", {"disable": {"reason": "admin", "note": "users-perm test"}})
    opts.client.logout()

    assert resp.status_code == 200, \
        f"caller with `users` perm should be able to disable, got {resp.status_code}: {opts.client.last_response.body}"
    target.refresh_from_db()
    assert target.is_active is False, "target user should be disabled"

    User.objects.filter(pk__in=[bare_admin.pk, target.pk]).delete()


@th.django_unit_test()
def test_actions_reject_unrelated_non_admin(opts):
    """A plain user (not owner, no users/manage_users) is blocked at the save layer
    from running any of the five actions on someone else's record."""
    from mojo.apps.account.models import User

    User.objects.filter(email="user_actions_stranger@test.com").delete()
    stranger = User.objects.create_user(
        username="user_actions_stranger@test.com",
        email="user_actions_stranger@test.com",
        password="stranger_pw_99")
    stranger.is_active = True
    stranger.is_email_verified = True
    stranger.save()  # deliberately no perms — only owner-of-self elsewhere

    pre_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()

    cases = [
        {"change_username": {"username": "stranger_set"}},
        {"revoke_sessions": True},
        {"confirm_totp": {"code": "000000"}},
        {"regenerate_totp_codes": {"code": "000000"}},
        {"disable_totp": True},
    ]

    assert opts.client.login("user_actions_stranger@test.com", "stranger_pw_99"), "stranger login failed"
    for body in cases:
        resp = opts.client.post(f"/api/user/{opts.user_id}", body)
        action_name = next(iter(body.keys()))
        assert resp.status_code != 200, \
            f"unrelated non-admin must be rejected for {action_name}, got {resp.status_code}"
    opts.client.logout()

    user = User.objects.get(pk=opts.user_id)
    assert user.username == USERNAME, "target username must be unchanged after stranger attempts"
    assert user.get_auth_key() == pre_auth_key, \
        "target auth_key must NOT be rotated by an unrelated non-admin"

    User.objects.filter(pk=stranger.pk).delete()
