"""
Rate-limit smoke tests for MFA / passwordless verify endpoints.

The TOTP verify, recover, login and passkey login-complete endpoints all
gain a strict per-IP rate limit (10 / 60s). These tests confirm the cap
trips and is reachable from a normal client.

Filename prefixed with `zz_` so it runs LAST inside test_mfa — the
deliberate cap-tripping should not poison the IP counter for the
preceding TOTP/passkey functional tests.
"""
from testit import helpers as th


def _clear_ip(key):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key=key)


@th.django_unit_setup()
def setup_throttle_smoke(opts):
    for key in ("totp_verify", "totp_recover", "totp_login", "passkey_login"):
        _clear_ip(key)


# -----------------------------------------------------------------
# TOTP verify — bogus mfa_token, repeated until tier trips
# -----------------------------------------------------------------

@th.django_unit_test("mfa throttle: totp/verify trips after 10 attempts")
def test_totp_verify_rate_limit(opts):
    _clear_ip("totp_verify")
    payload = {"mfa_token": "bogus-mfa-token", "code": "000000"}

    for attempt in range(1, 11):
        resp = opts.client.post("/api/auth/totp/verify", payload)
        assert resp.status_code != 429, (
            f"totp/verify attempt {attempt}: rate limit fired too early "
            f"(status={resp.status_code})"
        )

    resp = opts.client.post("/api/auth/totp/verify", payload)
    assert resp.status_code == 429, (
        f"totp/verify attempt 11: expected 429 from per-IP rate limit, got {resp.status_code}"
    )


# -----------------------------------------------------------------
# TOTP recover — same shape
# -----------------------------------------------------------------

@th.django_unit_test("mfa throttle: totp/recover trips after 10 attempts")
def test_totp_recover_rate_limit(opts):
    _clear_ip("totp_recover")
    payload = {"mfa_token": "bogus-mfa-token", "recovery_code": "AAAA-BBBB-CCCC"}

    for attempt in range(1, 11):
        resp = opts.client.post("/api/auth/totp/recover", payload)
        assert resp.status_code != 429, (
            f"totp/recover attempt {attempt}: rate limit fired too early "
            f"(status={resp.status_code})"
        )

    resp = opts.client.post("/api/auth/totp/recover", payload)
    assert resp.status_code == 429, (
        f"totp/recover attempt 11: expected 429, got {resp.status_code}"
    )


# -----------------------------------------------------------------
# Passkey login complete — bogus credential
# -----------------------------------------------------------------

@th.django_unit_test("mfa throttle: passkeys/login/complete trips after 10 attempts")
def test_passkey_login_complete_rate_limit(opts):
    _clear_ip("passkey_login")
    payload = {
        "challenge_id": "bogus-challenge-id",
        "credential": {"id": "bogus-credential-id", "rawId": "bogus-credential-id"},
    }

    for attempt in range(1, 11):
        resp = opts.client.post("/api/auth/passkeys/login/complete", payload)
        assert resp.status_code != 429, (
            f"passkeys/login/complete attempt {attempt}: rate limit fired too early "
            f"(status={resp.status_code})"
        )

    resp = opts.client.post("/api/auth/passkeys/login/complete", payload)
    assert resp.status_code == 429, (
        f"passkeys/login/complete attempt 11: expected 429, got {resp.status_code}"
    )
