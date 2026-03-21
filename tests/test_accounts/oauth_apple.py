"""
Unit tests for AppleOAuthProvider.

Covers: auth URL construction, client_secret JWT generation,
profile extraction from id_token, and error cases.
"""
import time
from unittest.mock import patch, MagicMock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


def _make_id_token(sub="apple_uid_123", email="user@example.com", extra=None):
    """Build a minimal Apple id_token (unsigned, for decode-only tests)."""
    import jwt
    payload = {"sub": sub, "email": email, "aud": "com.example.web"}
    if extra:
        payload.update(extra)
    # encode without a real key — tests decode with verify_signature=False
    return jwt.encode(payload, "test-secret", algorithm="HS256")


# ---------------------------------------------------------------------------
# Auth URL
# ---------------------------------------------------------------------------

@th.django_unit_test("apple oauth: get_auth_url contains required params")
def test_auth_url_params(opts):
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider
    from django.conf import settings as django_settings

    django_settings.APPLE_CLIENT_ID = "com.example.web"
    try:
        svc = AppleOAuthProvider()
        url = svc.get_auth_url(state="teststate", redirect_uri="https://example.com/callback")
        assert_true("appleid.apple.com/auth/authorize" in url,
                    "URL should point to Apple auth endpoint")
        assert_true("client_id=com.example.web" in url,
                    "URL should contain client_id")
        assert_true("state=teststate" in url,
                    "URL should contain state")
        assert_true("response_type=code" in url,
                    "URL should contain response_type=code")
        assert_true("redirect_uri=" in url,
                    "URL should contain redirect_uri")
    finally:
        del django_settings.APPLE_CLIENT_ID


@th.django_unit_test("apple oauth: get_auth_url uses response_mode=form_post (VERIFY-003)")
def test_auth_url_uses_form_post(opts):
    """
    Regression for VERIFY-003:
    Apple rejects response_mode=query when email scope is requested.
    Must use form_post so Apple POSTs code+state back to redirect_uri.
    """
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider
    from django.conf import settings as django_settings

    django_settings.APPLE_CLIENT_ID = "com.example.web"
    try:
        svc = AppleOAuthProvider()
        url = svc.get_auth_url(state="teststate", redirect_uri="https://example.com/callback")
        assert_true("response_mode=form_post" in url,
                    f"Apple requires response_mode=form_post when email scope is requested, got: {url}")
        assert_true("response_mode=query" not in url,
                    f"response_mode=query must not appear in Apple auth URL, got: {url}")
    finally:
        del django_settings.APPLE_CLIENT_ID


# ---------------------------------------------------------------------------
# Client secret JWT
# ---------------------------------------------------------------------------

@th.django_unit_test("apple oauth: _build_client_secret generates valid ES256 JWT")
def test_client_secret_jwt(opts):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider
    from django.conf import settings as django_settings

    # Generate a real EC key for testing
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    django_settings.APPLE_TEAM_ID    = "TEAMID1234"
    django_settings.APPLE_CLIENT_ID  = "com.example.web"
    django_settings.APPLE_KEY_ID     = "KEYID12345"
    django_settings.APPLE_PRIVATE_KEY = pem
    try:
        svc = AppleOAuthProvider()
        secret = svc._build_client_secret()

        public_key = key.public_key()
        decoded = jwt.decode(secret, public_key, algorithms=["ES256"],
                             audience="https://appleid.apple.com")

        assert_eq(decoded["iss"], "TEAMID1234", "iss should be APPLE_TEAM_ID")
        assert_eq(decoded["sub"], "com.example.web", "sub should be APPLE_CLIENT_ID")
        assert_eq(decoded["aud"], "https://appleid.apple.com", "aud should be Apple audience")
        assert_true(decoded["exp"] > int(time.time()), "exp should be in the future")
    finally:
        for attr in ("APPLE_TEAM_ID", "APPLE_CLIENT_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"):
            if hasattr(django_settings, attr):
                delattr(django_settings, attr)


@th.django_unit_test("apple oauth: _build_client_secret raises if settings missing")
def test_client_secret_missing_settings(opts):
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider
    from django.conf import settings as django_settings

    # Ensure none of the Apple settings are present
    for attr in ("APPLE_TEAM_ID", "APPLE_CLIENT_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"):
        if hasattr(django_settings, attr):
            delattr(django_settings, attr)

    svc = AppleOAuthProvider()
    raised = False
    try:
        svc._build_client_secret()
    except ValueError:
        raised = True
    assert_true(raised, "should raise ValueError when Apple settings are missing")


# ---------------------------------------------------------------------------
# get_profile
# ---------------------------------------------------------------------------

@th.django_unit_test("apple oauth: get_profile extracts uid and email from id_token")
def test_get_profile_success(opts):
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider

    svc = AppleOAuthProvider()
    id_token = _make_id_token(sub="apple_uid_123", email="Alice@Example.com")
    profile = svc.get_profile({"id_token": id_token, "access_token": "dummy"})

    assert_eq(profile["uid"], "apple_uid_123", "uid should be the sub claim")
    assert_eq(profile["email"], "alice@example.com", "email should be lowercased")


@th.django_unit_test("apple oauth: get_profile raises if id_token missing")
def test_get_profile_no_id_token(opts):
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider

    svc = AppleOAuthProvider()
    raised = False
    try:
        svc.get_profile({"access_token": "dummy"})
    except ValueError:
        raised = True
    assert_true(raised, "should raise ValueError when id_token is absent")


@th.django_unit_test("apple oauth: get_profile raises if email missing from id_token")
def test_get_profile_no_email(opts):
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider
    import jwt

    id_token = jwt.encode({"sub": "uid123"}, "test-secret", algorithm="HS256")
    svc = AppleOAuthProvider()
    raised = False
    try:
        svc.get_profile({"id_token": id_token})
    except ValueError:
        raised = True
    assert_true(raised, "should raise ValueError when email is absent from id_token")


@th.django_unit_test("apple oauth: get_profile accepts relay email address")
def test_get_profile_relay_email(opts):
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider

    svc = AppleOAuthProvider()
    relay = "abc123@privaterelay.appleid.com"
    id_token = _make_id_token(sub="uid_relay", email=relay)
    profile = svc.get_profile({"id_token": id_token})

    assert_eq(profile["email"], relay, "relay email should be stored as-is")
