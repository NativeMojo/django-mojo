"""Default-tier GitHub webhook HMAC contracts (maestro item #2558).

Pulled back from tests/test_github_extended_serial/github_app.py via the
`secret=` seam on verify_webhook_signature — no django.conf mutation, so the
forged-signature refusal runs on every default suite. The settings-read
variants stay opt-in.
"""

TESTIT_TIER = "core"
import hashlib
import hmac

from testit import helpers as th


@th.django_unit_test("github hmac: a valid signature is accepted")
def test_hmac_valid_signature(opts):
    from mojo.apps.github.services.github_app import verify_webhook_signature

    secret = "testit-webhook-secret"
    payload = b'{"action": "created"}'
    expected_sig = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(payload, expected_sig, secret=secret) is True, \
        "a correctly signed payload must verify"


@th.django_unit_test("github hmac: a forged signature is refused")
def test_hmac_forged_signature(opts):
    from mojo.apps.github.services.github_app import verify_webhook_signature

    assert verify_webhook_signature(
        b"payload", "sha256=definitely_wrong", secret="testit-secret") is False, \
        "a forged HMAC signature must be refused"

    tampered = b'{"action": "created", "evil": true}'
    secret = "testit-webhook-secret"
    good_sig = "sha256=" + hmac.new(
        secret.encode("utf-8"), b'{"action": "created"}', hashlib.sha256).hexdigest()
    assert verify_webhook_signature(tampered, good_sig, secret=secret) is False, \
        "a signature over different bytes must not verify a tampered payload"


@th.django_unit_test("github hmac: a missing signature header is refused")
def test_hmac_missing_header(opts):
    from mojo.apps.github.services.github_app import verify_webhook_signature

    assert verify_webhook_signature(b"payload", None, secret="testit-secret") is False, \
        "a missing signature header must be refused even with a secret configured"
