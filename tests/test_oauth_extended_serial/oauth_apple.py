"""
AppleOAuthProvider tests that mutate django.conf.settings in the test
process — moved out of tests/test_oauth/oauth_apple.py into this opt-in
serial package (maestro item #1839). Process-global settings mutation races
parallel test threads even with a try/finally restore.

Covers: auth URL construction and client_secret JWT generation (both need
APPLE_* settings installed process-wide).
"""
import time
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


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


@th.django_unit_test("apple oauth: get_auth_url fully encodes redirect_uri and uses %20 for spaces")
def test_auth_url_encoding(opts):
    """
    Regression: requests.utils.quote left '/' unencoded (Apple rejects redirect_uri).
    urlencode() without quote_via encodes spaces as '+' (Apple may reject scope).
    urlencode(quote_via=quote) must be used: slashes -> %2F, spaces -> %20.
    """
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider
    from django.conf import settings as django_settings

    django_settings.APPLE_CLIENT_ID = "com.example.web"
    try:
        svc = AppleOAuthProvider()
        url = svc.get_auth_url(state="s", redirect_uri="https://example.com/callback")
        assert_true("https%3A%2F%2F" in url,
                    f"redirect_uri slashes must be encoded as %2F, got: {url}")
        assert_true("https%3A//" not in url,
                    f"unencoded slashes in redirect_uri cause Apple to reject the request, got: {url}")
        assert_true("openid%20email" in url,
                    f"scope spaces must be encoded as %20 not +, got: {url}")
        assert_true("openid+email" not in url,
                    f"+ encoding for spaces in scope may be rejected by Apple, got: {url}")
    finally:
        del django_settings.APPLE_CLIENT_ID


@th.django_unit_test("apple oauth: get_auth_url uses form_post with passed redirect_uri")
def test_auth_url_uses_form_post(opts):
    """
    Apple requires response_mode=form_post when email scope is requested.
    The redirect_uri (backend callback) is passed in from on_oauth_begin which
    derives it from the request origin — no hardcoded settings needed.
    """
    from mojo.apps.account.services.oauth.apple import AppleOAuthProvider
    from django.conf import settings as django_settings

    django_settings.APPLE_CLIENT_ID = "com.example.web"
    try:
        svc = AppleOAuthProvider()
        callback = "https://example.com/api/auth/oauth/apple/callback"
        url = svc.get_auth_url(state="teststate", redirect_uri=callback)
        assert_true("response_mode=form_post" in url,
                    f"Apple requires response_mode=form_post for email scope, got: {url}")
        assert_true("api%2Fauth%2Foauth%2Fapple%2Fcallback" in url,
                    f"redirect_uri must be the backend callback URL, got: {url}")
        assert_true("response_mode=query" not in url,
                    f"response_mode=query is rejected by Apple when email scope is requested, got: {url}")
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
