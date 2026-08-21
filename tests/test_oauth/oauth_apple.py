"""
Unit tests for AppleOAuthProvider.

Covers: profile extraction from id_token and error cases.

The auth-URL and client_secret JWT tests (which install APPLE_* settings on
django.conf.settings process-wide) moved to
tests/test_oauth_extended_serial/oauth_apple.py (maestro item #1839).
"""
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
