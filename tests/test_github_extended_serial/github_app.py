"""
GitHub App webhook-signature tests that mutate django.conf.settings in the
test process — moved out of tests/test_github/github_app.py into this opt-in
serial package (maestro item #1839). Each installs/deletes
GITHUB_WEBHOOK_SECRET process-wide, which races parallel test threads even
with a try/finally restore.

These tests use test secrets and do not hit real GitHub APIs.
"""
import hashlib
import hmac

from testit import helpers as th


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
