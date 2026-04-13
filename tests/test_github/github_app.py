"""
Tests for GitHub App service — JWT, token caching, webhook signature verification.

These tests use test RSA keys and do not hit real GitHub APIs.
"""
import hashlib
import hmac
import time

from testit import helpers as th


# RSA key pair for testing (2048-bit, generated for tests only)
TEST_RSA_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7MhgHcTz6sE2I2yPB
aFDrBz9vFqU4jK5Z0JqFV7g0CJq6EMg3cS9QLZY0TiNX3CpR3GSv1MhKPfHaQH6F
z0bH+3MKC0TVr1I7L4lQaJj2HCT9mFVNRWKHPfN3oC7ESUlF5kFF5MzV5N6hTEjBQ
r/WZL3GxBOlAyR8E8kf7u1Bgo0HRzLUJ5c0wuVcn8CsxKlQtK5N3SBFn0SAOQHJ2v
K5f1WS7GIMvekCBM0AvZi1VKxdVDAi1JN9JxOIi0oWPT1DAf8qr/cH3aEEBjKqFE5P
IflVp19tdEH1ePZauR4eJTIxWLOP2L3JEvHYrwIDAQABAoIBAFnSq/3OhJnHF7J2kM
EFXGaSYGeWv9s3mD/K9YMeP2s5q+2A6m6OZQFTYQ+r+3E4La1lQxMb8VEn0S5OPYMU
5aeNmGVvD8AAxC+cADT+HRKEC0EMwL2k0JWTJ1FJFfsSCGDJKPSsNNEb2RGCBL91J4
lqB1SnN0NZwZP2LPrfXbD8AxVCPnIoj11RWNHm7yNqBLM3EGqanMOxnXlA9dwkN4MN
+4VPHQDkxAQN2bKUXemDnFmOCRbp6DVfm1sJcLJ4xXLYKqK/i8kp2c8saN7fRbxjnD
xFnM8TGPvkWDCiAhLsI4xnupMFbkb5LPHmHVYP3v7eSdG4qdvRO8ECgYEA7aPLkfS
zU3J+k4u+kDGy7G3XBlzYxpGqx2s3F8gL/edrkviJjG4bHDn5fDR3FfIq2AaSZfR7s
3e+2s3U0CIAJ5FBVm3+6K7LTJbRiJUmKsA8kVNEPP5Y7FQKPFnNT3R5ZU5gFJ4DX0k
B3S5JHkHDhNTNGLJMrr3G4i3/xClP1cCgYEA4rG2IM/sYzz3UjrfxRAyiEU+0Ckek1
f1W3tSL0kMX3DKVMQNV8pL4iyJ2xTk4T6qT6URKLqaIqLFiK3EL0BQ6alJU/K5+q/L
NeBLi7x/m5j02vkmgA4pKv/6RW0CQpEkKSLxl/uBLGxkVJROnAsh9bZ3rTiHRCX+0P
G5u2hMsCkCgYEAglqCchyIQPFQoIR6LSrZc2s3fR9LcLV6W7Lg/HFMSmKMD3/7X2FE
+NsLLFHnTJ2I0t8F4i3E6aSG0pPC3dFVjcVDlF7vfN0REXR3vMiVQP3ENMpmHvA8B4
J0ljgH6ht1s+JlxQk5OKN7fHKOoRY8A4GFHqJFVsAFn+PjJhp70CgYBbv31K8lLCR0
CEJFw+pn0VYBnqYjPHxfNJA/8syZdfgKC8PT2q5E1FRZWX2y3cGdR3g1kWHdnij/ke
G3e+Sc5sTEnGONpVWKsMpV7p7V2U5SVN6TA5F1pCvP9B3E+C0OYfWFKvA8SdP2DLVK
j3zJlIFbWOEBLdOPmxkXWM38g6QKBgAKxfJJnVr1fqFNqNcFOEJnkLxGBJD3c0FHkP
JoC9H/bxO+DEnFLU2+dWnKGHPMm6i7zB0RRUFQ4MuPBXIIAKi6V6hP0vKz7M/LwWj
Gc7FeAf3je6Ua8xmMEJ2VZ/DqJGUOVH4Ww5bHAiSnRddlB2Lj/ODAl3a/X/Jx7HjI7
-----END RSA PRIVATE KEY-----"""


@th.django_unit_test("github app: is_configured returns False when settings missing")
def test_is_configured_false(opts):
    from mojo.apps.github.services.github_app import is_configured

    # Without settings, should return False
    result = is_configured()
    assert result is False, "is_configured should be False without GITHUB_APP_ID"


@th.django_unit_test("github app: is_token_valid with None returns False")
def test_is_token_valid_none(opts):
    from mojo.apps.github.services.github_app import is_token_valid

    assert is_token_valid(None) is False, "is_token_valid(None) should be False"


@th.django_unit_test("github app: is_token_valid with future expiry returns True")
def test_is_token_valid_future(opts):
    from django.utils import timezone
    from mojo.apps.github.services.github_app import is_token_valid

    future = timezone.now() + timezone.timedelta(hours=1)
    assert is_token_valid(future) is True, "Token expiring in 1 hour should be valid"


@th.django_unit_test("github app: is_token_valid with past expiry returns False")
def test_is_token_valid_past(opts):
    from django.utils import timezone
    from mojo.apps.github.services.github_app import is_token_valid

    past = timezone.now() - timezone.timedelta(hours=1)
    assert is_token_valid(past) is False, "Expired token should not be valid"


@th.django_unit_test("github app: is_token_valid within buffer returns False")
def test_is_token_valid_within_buffer(opts):
    from django.utils import timezone
    from mojo.apps.github.services.github_app import is_token_valid

    # Expires in 2 minutes — inside the 5-minute buffer
    near_future = timezone.now() + timezone.timedelta(minutes=2)
    assert is_token_valid(near_future, buffer_seconds=300) is False, (
        "Token expiring within buffer should not be valid"
    )


@th.django_unit_test("github app: generate_jwt raises when not configured")
def test_generate_jwt_not_configured(opts):
    from mojo.apps.github.services.github_app import generate_jwt

    raised = False
    try:
        generate_jwt()
    except ValueError as e:
        raised = True
        assert "not configured" in str(e).lower(), f"Error should mention configuration: {e}"

    assert raised, "generate_jwt should raise ValueError when not configured"


@th.django_unit_test("github app: verify_webhook_signature rejects when secret not set")
def test_verify_webhook_no_secret(opts):
    from mojo.apps.github.services.github_app import verify_webhook_signature

    result = verify_webhook_signature(b"payload", "sha256=abc123")
    assert result is False, "Should reject when GITHUB_WEBHOOK_SECRET is not set"


@th.django_unit_test("github app: verify_webhook_signature rejects missing signature")
def test_verify_webhook_no_signature(opts):
    from django.conf import settings as django_settings
    from mojo.apps.github.services.github_app import verify_webhook_signature

    django_settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    try:
        result = verify_webhook_signature(b"payload", None)
    finally:
        del django_settings.GITHUB_WEBHOOK_SECRET
    assert result is False, "Should reject when signature header is missing"


@th.django_unit_test("github app: verify_webhook_signature accepts valid signature")
def test_verify_webhook_valid(opts):
    from django.conf import settings as django_settings
    from mojo.apps.github.services.github_app import verify_webhook_signature

    secret = "test-webhook-secret"
    payload = b'{"action": "created"}'

    # Compute the expected signature
    expected_sig = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    django_settings.GITHUB_WEBHOOK_SECRET = secret
    try:
        result = verify_webhook_signature(payload, expected_sig)
    finally:
        del django_settings.GITHUB_WEBHOOK_SECRET
    assert result is True, "Should accept valid HMAC signature"


@th.django_unit_test("github app: verify_webhook_signature rejects invalid signature")
def test_verify_webhook_invalid(opts):
    from django.conf import settings as django_settings
    from mojo.apps.github.services.github_app import verify_webhook_signature

    django_settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    try:
        result = verify_webhook_signature(b"payload", "sha256=definitely_wrong")
    finally:
        del django_settings.GITHUB_WEBHOOK_SECRET
    assert result is False, "Should reject invalid HMAC signature"
