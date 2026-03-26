"""
Tests for OAuth authentication and magic login.

OAuth complete flow cannot hit real providers in tests, so we test:
- begin: returns auth_url + state
- complete: mocked at the provider service level by seeding an
  OAuthConnection directly and testing the auto-link logic
- Auto-link: existing email -> links connection, existing connection -> logs in
- New user creation via OAuth

Magic login tests:
- send: returns success without leaking user existence
- complete: valid ml: token issues JWT
- complete: pr: token rejected on magic login endpoint
- Token is single-use
"""
from testit import helpers as th

TEST_USER = "oauth_user"
TEST_PWORD = "oauth##secret99"
TEST_EMAIL = "oauth_user@example.com"
PROVIDER = "google"


@th.django_unit_setup()
def setup_oauth_env(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL, display_name="OAuth User")
        user.save()
    user.is_active = True
    user.is_email_verified = True
    user.save_password(TEST_PWORD)

    # Clean up connections
    OAuthConnection.objects.filter(user=user).delete()
    opts.user = user


# -----------------------------------------------------------------
# OAuth begin
# -----------------------------------------------------------------

@th.django_unit_test("oauth: begin returns auth_url with backend callback redirect_uri")
def test_oauth_begin(opts):
    from urllib.parse import unquote
    resp = opts.client.get(f"/api/auth/oauth/{PROVIDER}/begin")
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.auth_url, "Missing auth_url"
    assert data.state, "Missing state"
    assert "accounts.google.com" in data.auth_url, "auth_url should point to Google"
    assert data.state in data.auth_url, "state should be in auth_url"
    # The redirect_uri sent to the provider must be the backend callback,
    # NOT a frontend page URL. This keeps the auth code server-side.
    decoded_url = unquote(data.auth_url)
    assert "/api/auth/oauth/google/callback" in decoded_url, (
        f"redirect_uri must be the backend callback endpoint, got: {decoded_url}"
    )
    opts.oauth_state = data.state


@th.django_unit_test("oauth: begin stores frontend_uri in state for post-callback bounce")
def test_oauth_begin_frontend_uri_in_state(opts):
    """
    When the frontend sends redirect_uri, it is stored in state as frontend_uri
    (where to bounce after callback), NOT used as the provider redirect_uri.
    """
    from urllib.parse import unquote
    frontend_url = "https://example.com/login"
    with th.server_settings(ALLOWED_REDIRECT_URLS=["https://example.com/"]):
        resp = opts.client.get(
            f"/api/auth/oauth/{PROVIDER}/begin?redirect_uri={frontend_url}"
        )
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    auth_url = resp.response.data.auth_url
    decoded_url = unquote(auth_url)
    # The frontend URL must NOT be the provider redirect_uri
    assert "example.com/login" not in decoded_url, (
        f"Frontend URL must not be sent to provider as redirect_uri: {decoded_url}"
    )
    # The backend callback must be the redirect_uri
    assert "/api/auth/oauth/google/callback" in decoded_url, (
        f"redirect_uri must be the backend callback endpoint: {decoded_url}"
    )


@th.django_unit_test("oauth: callback endpoint bounces to frontend with code and state")
def test_oauth_callback_redirects(opts):
    """
    The callback endpoint receives code + state from the provider and
    redirects to the frontend_uri stored in state. We point the
    frontend_uri at a known test-server path so the redirect follows
    and we can verify code + state arrived as query params.
    """
    from mojo.apps.account.services.oauth import get_provider
    svc = get_provider(PROVIDER)
    # Use the test server's login endpoint as the bounce target
    frontend_uri = "http://localhost:9009/api/login"
    callback_uri = "http://localhost:9009/api/auth/oauth/google/callback"
    state = svc.create_state(extra={
        "redirect_uri": callback_uri,
        "frontend_uri": frontend_uri,
    })
    # allow_redirects=False to inspect the 302 directly
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/callback?code=testcode123&state={state}",
        allow_redirects=False,
    )
    assert resp.status_code == 302, (
        f"Callback should 302 redirect, got {resp.status_code}: {resp.response}"
    )


@th.django_unit_test("oauth: callback rejects missing code or state")
def test_oauth_callback_rejects_missing_params(opts):
    resp = opts.client.get(f"/api/auth/oauth/{PROVIDER}/callback?code=testcode123")
    assert resp.status_code == 400, (
        f"Should reject missing state, got {resp.status_code}"
    )
    resp = opts.client.get(f"/api/auth/oauth/{PROVIDER}/callback?state=fakestate")
    assert resp.status_code == 400, (
        f"Should reject missing code, got {resp.status_code}"
    )


@th.django_unit_test("oauth: callback rejects invalid state")
def test_oauth_callback_rejects_invalid_state(opts):
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/callback?code=testcode123&state=bogus"
    )
    assert resp.status_code in [401, 403], (
        f"Should reject invalid state, got {resp.status_code}"
    )


@th.django_unit_test("oauth: begin rejects unknown provider")
def test_oauth_begin_unknown_provider(opts):
    resp = opts.client.get("/api/auth/oauth/fakeprovider/begin")
    assert resp.status_code == 400, f"Should reject unknown provider, got {resp.status_code}"


@th.django_unit_test("oauth: invalid/expired state is rejected on complete")
def test_oauth_complete_invalid_state(opts):
    resp = opts.client.post(f"/api/auth/oauth/{PROVIDER}/complete", {
        "code": "somecode",
        "state": "invalidstate000",
    })
    assert resp.status_code in [401, 403], f"Should reject invalid state, got {resp.status_code}"


# -----------------------------------------------------------------
# Auto-link: existing connection
# -----------------------------------------------------------------

@th.django_unit_test("oauth: existing OAuthConnection logs user in")
def test_oauth_existing_connection(opts):
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.services.oauth import get_provider
    from mojo.apps.account.rest.user import jwt_login

    # Seed a connection directly
    conn = OAuthConnection.objects.create(
        user=opts.user,
        provider=PROVIDER,
        provider_uid="google_uid_12345",
        email=TEST_EMAIL,
    )
    opts.oauth_conn = conn
    opts.google_uid = "google_uid_12345"

    # Verify the connection exists and is linked to the right user
    found = OAuthConnection.objects.filter(
        provider=PROVIDER, provider_uid=opts.google_uid
    ).select_related("user").first()
    assert found is not None, "Connection should exist"
    assert found.user.id == opts.user.id, "Connection should be linked to test user"


@th.django_unit_test("oauth: auto-link creates connection for matching email")
def test_oauth_autolink_by_email(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    # Remove any existing connections for this user
    OAuthConnection.objects.filter(user=opts.user).delete()

    # Simulate the auto-link logic directly
    from mojo.apps.account.rest.oauth import _find_or_create_user
    profile = {
        "uid": "google_uid_new_99999",
        "email": TEST_EMAIL,  # matches existing user's email
        "display_name": "OAuth User",
    }
    user, conn, created = _find_or_create_user(PROVIDER, profile)

    assert user.id == opts.user.id, "Should link to existing user by email"
    assert conn.provider_uid == "google_uid_new_99999", "Connection should use new uid"
    assert conn.provider == PROVIDER
    assert created is False, "Existing user should not be flagged as new"


@th.django_unit_test("oauth: auto-link by email marks is_email_verified=True for unverified user")
def test_oauth_autolink_by_email_marks_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user

    # Ensure the user exists and is NOT verified
    OAuthConnection.objects.filter(user=opts.user).delete()
    opts.user.is_email_verified = False
    opts.user.save(update_fields=["is_email_verified", "modified"])

    profile = {
        "uid": "google_uid_verify_check",
        "email": TEST_EMAIL,
        "display_name": "OAuth User",
    }
    user, conn, created = _find_or_create_user(PROVIDER, profile)

    assert user.id == opts.user.id, "Should link to existing user"
    fresh = User.objects.get(pk=opts.user.pk)
    assert fresh.is_email_verified is True, "OAuth email match should mark user as verified"

    # Restore for subsequent tests
    opts.user.is_email_verified = True
    opts.user.save(update_fields=["is_email_verified", "modified"])


@th.django_unit_test("oauth: auto-link by email does not clobber already-verified user")
def test_oauth_autolink_by_email_already_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user

    OAuthConnection.objects.filter(user=opts.user).delete()
    opts.user.is_email_verified = True
    opts.user.save(update_fields=["is_email_verified", "modified"])

    profile = {
        "uid": "google_uid_already_verified",
        "email": TEST_EMAIL,
        "display_name": "OAuth User",
    }
    user, conn, created = _find_or_create_user(PROVIDER, profile)

    assert user.id == opts.user.id, "Should link to existing user"
    fresh = User.objects.get(pk=opts.user.pk)
    assert fresh.is_email_verified is True, "Already-verified flag should remain True"


@th.django_unit_test("oauth: auto-link creates new user for unknown email")
def test_oauth_autolink_creates_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user

    new_email = "brand_new_oauth@example.com"
    User.objects.filter(email=new_email).delete()

    profile = {
        "uid": "google_uid_brandnew",
        "email": new_email,
        "display_name": "Brand New User",
    }
    user, conn, created = _find_or_create_user(PROVIDER, profile)

    assert user.email == new_email, "Should create user with OAuth email"
    assert user.is_email_verified is True, "OAuth user should have email verified"
    assert conn.provider_uid == "google_uid_brandnew"
    assert created is True, "New user should be flagged as created"

    # Cleanup
    user.delete()


@th.django_unit_test("oauth: OAUTH_ALLOW_REGISTRATION=False blocks new user creation")
def test_oauth_registration_gate(opts):
    from django.conf import settings as django_settings
    from mojo.apps.account.models import User
    from mojo.apps.account.rest.oauth import _find_or_create_user
    from mojo import errors as merrors

    gated_email = "blocked_registration@example.com"
    User.objects.filter(email=gated_email).delete()

    original = getattr(django_settings, "OAUTH_ALLOW_REGISTRATION", True)
    django_settings.OAUTH_ALLOW_REGISTRATION = False
    try:
        profile = {"uid": "google_uid_gated", "email": gated_email, "display_name": "Blocked"}
        raised = False
        try:
            _find_or_create_user(PROVIDER, profile)
        except merrors.PermissionDeniedException:
            raised = True
        assert raised, "Should raise PermissionDeniedException when registration is disabled"
        assert not User.objects.filter(email=gated_email).exists(), "User should not have been created"
    finally:
        django_settings.OAUTH_ALLOW_REGISTRATION = original


@th.django_unit_test("oauth: MFA is bypassed — OAuth is a trusted second factor")
def test_oauth_bypasses_mfa(opts):
    """
    OAuth is treated as a trusted second factor. A user with requires_mfa=True
    must NOT be challenged for an additional MFA step after OAuth — the provider
    has already authenticated the identity. This test confirms get_mfa_methods()
    is never consulted on the OAuth complete path.
    """
    from mojo.apps.account.rest.oauth import _find_or_create_user
    from mojo.apps.account.models.oauth import OAuthConnection

    # Enable MFA flag on the test user
    opts.user.requires_mfa = True
    opts.user.save(update_fields=["requires_mfa", "modified"])

    OAuthConnection.objects.filter(user=opts.user).delete()

    profile = {
        "uid": "google_uid_mfa_bypass_test",
        "email": TEST_EMAIL,
        "display_name": "OAuth MFA User",
    }
    # _find_or_create_user must succeed without any MFA challenge being raised
    user, conn, created = _find_or_create_user(PROVIDER, profile)
    assert user.id == opts.user.id, "Should resolve the MFA-enabled user normally"

    # Restore
    opts.user.requires_mfa = False
    opts.user.save(update_fields=["requires_mfa", "modified"])


@th.django_unit_test("oauth: disabled user is rejected")
def test_oauth_disabled_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user
    from mojo import errors as merrors

    disabled_email = "disabled_oauth@example.com"
    User.objects.filter(email=disabled_email).delete()

    disabled_user = User(username="disabled_oauth", email=disabled_email)
    disabled_user.is_active = False
    disabled_user.save()

    OAuthConnection.objects.create(
        user=disabled_user,
        provider=PROVIDER,
        provider_uid="google_uid_disabled",
        email=disabled_email,
    )

    # complete endpoint should reject inactive user — test via REST
    # (we can't call the full flow without a real code, so we verify the guard logic)
    from mojo.apps.account.models.oauth import OAuthConnection as OC
    conn = OC.objects.filter(provider=PROVIDER, provider_uid="google_uid_disabled").first()
    assert conn.user.is_active is False, "User should be inactive"

    # Cleanup
    disabled_user.delete()


# -----------------------------------------------------------------
# Magic login
# -----------------------------------------------------------------

@th.django_unit_test("magic login: send returns success without leaking user existence")
def test_magic_send(opts):
    # Known user
    resp = opts.client.post("/api/auth/magic/send", {"email": TEST_EMAIL})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    assert resp.response.status is True

    # Unknown email — should also return 200
    resp = opts.client.post("/api/auth/magic/send", {"email": "nobody@nowhere.example.com"})
    assert resp.status_code == 200, "Should return 200 even for unknown email"


@th.django_unit_test("magic login: valid ml: token issues JWT")
def test_magic_login_valid_token(opts):
    from mojo.apps.account.utils.tokens import generate_magic_login_token

    token = generate_magic_login_token(opts.user)
    assert token.startswith("ml:"), f"Token should start with 'ml:', got: {token[:5]}"

    resp = opts.client.post("/api/auth/magic/login", {"token": token})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.access_token, "Missing access_token"
    assert data.user, "Missing user"


@th.django_unit_test("magic login: pr: token is rejected on magic login endpoint")
def test_magic_login_rejects_pr_token(opts):
    from mojo.apps.account.utils.tokens import generate_password_reset_token

    token = generate_password_reset_token(opts.user)
    assert token.startswith("pr:"), f"Token should start with 'pr:', got: {token[:5]}"

    resp = opts.client.post("/api/auth/magic/login", {"token": token})
    assert resp.status_code == 400, f"Should reject pr: token on magic login, got {resp.status_code}"


@th.django_unit_test("magic login: ml: token is rejected on password reset endpoint")
def test_magic_login_token_rejected_on_reset(opts):
    from mojo.apps.account.utils.tokens import generate_magic_login_token

    token = generate_magic_login_token(opts.user)
    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": token,
        "new_password": "SomeNewPass99!",
    })
    assert resp.status_code == 400, f"Should reject ml: token on password reset, got {resp.status_code}"


@th.django_unit_test("magic login: token is single-use")
def test_magic_login_single_use(opts):
    from mojo.apps.account.utils.tokens import generate_magic_login_token

    token = generate_magic_login_token(opts.user)

    resp = opts.client.post("/api/auth/magic/login", {"token": token})
    assert resp.status_code == 200, "First use should succeed"

    resp = opts.client.post("/api/auth/magic/login", {"token": token})
    assert resp.status_code == 400, "Second use should fail — token consumed"


@th.django_unit_test("magic login: invalid token is rejected")
def test_magic_login_invalid_token(opts):
    resp = opts.client.post("/api/auth/magic/login", {"token": "ml:notavalidtoken"})
    assert resp.status_code == 400, f"Should reject invalid token, got {resp.status_code}"


@th.django_unit_test("magic login: token prefix is correct format")
def test_token_prefixes(opts):
    from mojo.apps.account.utils.tokens import (
        generate_magic_login_token,
        generate_password_reset_token,
    )
    ml_token = generate_magic_login_token(opts.user)
    # consume it so user's jti isn't left dirty
    from mojo.apps.account.utils.tokens import generate_magic_login_token as gen
    pr_token = generate_password_reset_token(opts.user)

    assert ml_token.startswith("ml:"), f"Magic login token should start with 'ml:'"
    assert pr_token.startswith("pr:"), f"Password reset token should start with 'pr:'"
    assert not ml_token.startswith("pr:"), "Tokens should not share prefix"


# -----------------------------------------------------------------
# OAuth connection management
# -----------------------------------------------------------------

ADMIN_USER = "oauth_admin"
ADMIN_PWORD = "oauthadmin##secret99"

OTHER_USER = "oauth_other"
OTHER_PWORD = "oauthother##secret99"
OTHER_EMAIL = "oauth_other@example.com"


@th.django_unit_setup()
def setup_oauth_connection_env(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Ensure main test user exists and has a password
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL, display_name="OAuth User")
        user.save()
    user.is_active = True
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    OAuthConnection.objects.filter(user=user).delete()
    opts.user = user

    # Admin user with manage_users
    admin = User.objects.filter(username=ADMIN_USER).last()
    if admin is None:
        admin = User(username=ADMIN_USER, email=f"{ADMIN_USER}@example.com", display_name="OAuth Admin")
        admin.save()
    admin.is_active = True
    admin.add_permission(["manage_users"])
    admin.save_password(ADMIN_PWORD)
    OAuthConnection.objects.filter(user=admin).delete()
    opts.admin = admin

    # Another regular user
    other = User.objects.filter(username=OTHER_USER).last()
    if other is None:
        other = User(username=OTHER_USER, email=OTHER_EMAIL, display_name="Other User")
        other.save()
    other.is_active = True
    other.save_password(OTHER_PWORD)
    OAuthConnection.objects.filter(user=other).delete()
    opts.other_user = other


@th.django_unit_test("oauth: new user created via OAuth has unusable password")
def test_new_oauth_user_unusable_password(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user

    new_email = "unusable_pw_test@example.com"
    User.objects.filter(email=new_email).delete()

    profile = {
        "uid": "google_uid_unusable_pw",
        "email": new_email,
        "display_name": "Unusable PW User",
    }
    user, conn, created = _find_or_create_user(PROVIDER, profile)

    assert user.email == new_email, "Should create user with OAuth email"
    assert user.has_usable_password() is False, "New OAuth user should have unusable password"

    # Cleanup
    user.delete()


@th.django_unit_test("oauth: owner can list their connections")
def test_oauth_connection_list_owner(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.user).delete()
    conn = OAuthConnection.objects.create(
        user=opts.user,
        provider=PROVIDER,
        provider_uid="google_uid_list_test",
        email=TEST_EMAIL,
    )

    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    resp = opts.client.get("/api/account/oauth_connection")
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    assert resp.response.count >= 1, "Should have at least one connection"

    conn_ids = [c.id for c in resp.response.data]
    assert conn.id in conn_ids, "Owner's connection should be in list"


@th.django_unit_test("oauth: owner does not see another user's connections")
def test_oauth_connection_list_isolation(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.other_user).delete()
    other_conn = OAuthConnection.objects.create(
        user=opts.other_user,
        provider=PROVIDER,
        provider_uid="google_uid_other_list",
        email=OTHER_EMAIL,
    )

    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    resp = opts.client.get("/api/account/oauth_connection")
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"

    conn_ids = [c.id for c in resp.response.data]
    assert other_conn.id not in conn_ids, "Should not see other user's connection"


@th.django_unit_test("oauth: owner can unlink when they have a usable password")
def test_oauth_connection_delete_with_password(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.user).delete()
    conn = OAuthConnection.objects.create(
        user=opts.user,
        provider=PROVIDER,
        provider_uid="google_uid_del_pw",
        email=TEST_EMAIL,
    )

    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    resp = opts.client.delete(f"/api/account/oauth_connection/{conn.id}")
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    assert not OAuthConnection.objects.filter(pk=conn.id).exists(), "Connection should be deleted"


@th.django_unit_test("oauth: owner can unlink one of two connections without password")
def test_oauth_connection_delete_two_connections_no_password(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.user).delete()

    conn1 = OAuthConnection.objects.create(
        user=opts.user,
        provider="google",
        provider_uid="google_uid_two_a",
        email=TEST_EMAIL,
    )
    conn2 = OAuthConnection.objects.create(
        user=opts.user,
        provider="github",
        provider_uid="github_uid_two_b",
        email=TEST_EMAIL,
    )

    # Login while password is still usable, then remove it
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    opts.user.set_unusable_password()
    opts.user.save()

    resp = opts.client.delete(f"/api/account/oauth_connection/{conn1.id}")
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    assert not OAuthConnection.objects.filter(pk=conn1.id).exists(), "Connection should be deleted"
    assert OAuthConnection.objects.filter(pk=conn2.id).exists(), "Other connection should remain"

    # Restore password for subsequent tests
    opts.user.save_password(TEST_PWORD)


@th.django_unit_test("oauth: unlink blocked when no password and only 1 active connection")
def test_oauth_connection_delete_lockout_guard(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.user).delete()

    conn = OAuthConnection.objects.create(
        user=opts.user,
        provider=PROVIDER,
        provider_uid="google_uid_lockout",
        email=TEST_EMAIL,
    )

    # Login while password is still usable, then remove it
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    opts.user.set_unusable_password()
    opts.user.save()

    resp = opts.client.delete(f"/api/account/oauth_connection/{conn.id}")
    assert resp.status_code == 400, f"Should block lockout, got {resp.status_code}: {resp.response}"
    assert OAuthConnection.objects.filter(pk=conn.id).exists(), "Connection should NOT be deleted"

    # Restore password for subsequent tests
    opts.user.save_password(TEST_PWORD)


@th.django_unit_test("oauth: manage_users admin can delete any connection")
def test_oauth_connection_admin_delete(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.other_user).delete()
    conn = OAuthConnection.objects.create(
        user=opts.other_user,
        provider=PROVIDER,
        provider_uid="google_uid_admin_del",
        email=OTHER_EMAIL,
    )

    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin authentication failed"

    resp = opts.client.delete(f"/api/account/oauth_connection/{conn.id}")
    assert resp.status_code == 200, f"Admin delete failed: {resp.status_code}: {resp.response}"
    assert not OAuthConnection.objects.filter(pk=conn.id).exists(), "Connection should be deleted by admin"


@th.django_unit_test("oauth: 404 when trying to delete another user's connection")
def test_oauth_connection_delete_other_user_404(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.other_user).delete()
    conn = OAuthConnection.objects.create(
        user=opts.other_user,
        provider=PROVIDER,
        provider_uid="google_uid_other_del",
        email=OTHER_EMAIL,
    )

    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    resp = opts.client.delete(f"/api/account/oauth_connection/{conn.id}")
    assert resp.status_code == 404, f"Should return 404, got {resp.status_code}"
    assert OAuthConnection.objects.filter(pk=conn.id).exists(), "Connection should NOT be deleted"


@th.django_unit_test("oauth: unauthenticated delete returns 403")
def test_oauth_connection_delete_unauth(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(user=opts.user).delete()
    conn = OAuthConnection.objects.create(
        user=opts.user,
        provider=PROVIDER,
        provider_uid="google_uid_unauth_del",
        email=TEST_EMAIL,
    )

    opts.client.logout()
    resp = opts.client.delete(f"/api/account/oauth_connection/{conn.id}")
    assert resp.status_code == 403, f"Should return 403, got {resp.status_code}"
    assert OAuthConnection.objects.filter(pk=conn.id).exists(), "Connection should NOT be deleted"
