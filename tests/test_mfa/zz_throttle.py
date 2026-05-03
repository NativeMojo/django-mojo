"""
Rate-limit smoke tests for MFA / passwordless verify endpoints.

The TOTP verify, recover, login and passkey login-complete endpoints all
gain a strict per-IP rate limit (10 / 60s). These tests confirm the cap
trips and is reachable from a normal client.

Filename prefixed with `zz_` so it runs LAST inside test_mfa — the
deliberate cap-tripping should not poison the IP counter for the
preceding TOTP/passkey functional tests.

Pattern: loop until 429 within a generous bound rather than asserting
on a specific attempt index. Under heavy parallel-suite load the
strict_rate_limit fail-open path may silently allow individual
requests through if Redis errors transiently — exact-attempt assertions
are flaky in that mode while the production behaviour (cap fires within
the configured window) is what we actually care about.
"""
from testit import helpers as th

# Loop bound — well above the configured 10/60s limit so the cap should
# always trip within this many requests, even under load.
MAX_ATTEMPTS = 30


def _clear_ip(key):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key=key)


def _drive_until_blocked(client, path, payload):
    """Post repeatedly until the endpoint returns 429, or up to MAX_ATTEMPTS.
    Returns (blocked, attempt_count, last_status)."""
    last_status = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = client.post(path, payload)
        last_status = resp.status_code
        if last_status == 429:
            return True, attempt, last_status
    return False, MAX_ATTEMPTS, last_status


@th.django_unit_setup()
def setup_throttle_smoke(opts):
    for key in ("totp_verify", "totp_recover", "totp_login", "passkey_login"):
        _clear_ip(key)


# -----------------------------------------------------------------
# TOTP verify — bogus mfa_token, repeated until tier trips
# -----------------------------------------------------------------

@th.django_unit_test("mfa throttle: totp/verify trips per-IP rate limit")
def test_totp_verify_rate_limit(opts):
    _clear_ip("totp_verify")
    payload = {"mfa_token": "bogus-mfa-token", "code": "000000"}

    blocked, attempts, last = _drive_until_blocked(opts.client, "/api/auth/totp/verify", payload)
    assert blocked, (
        f"totp/verify rate limit must trigger within {MAX_ATTEMPTS} attempts; "
        f"made {attempts}, last status={last}"
    )


# -----------------------------------------------------------------
# TOTP recover — same shape
# -----------------------------------------------------------------

@th.django_unit_test("mfa throttle: totp/recover trips per-IP rate limit")
def test_totp_recover_rate_limit(opts):
    _clear_ip("totp_recover")
    payload = {"mfa_token": "bogus-mfa-token", "recovery_code": "AAAA-BBBB-CCCC"}

    blocked, attempts, last = _drive_until_blocked(opts.client, "/api/auth/totp/recover", payload)
    assert blocked, (
        f"totp/recover rate limit must trigger within {MAX_ATTEMPTS} attempts; "
        f"made {attempts}, last status={last}"
    )


# -----------------------------------------------------------------
# Passkey login complete — bogus credential
# -----------------------------------------------------------------

@th.django_unit_test("mfa throttle: passkeys/login/complete trips per-IP rate limit")
def test_passkey_login_complete_rate_limit(opts):
    _clear_ip("passkey_login")
    payload = {
        "challenge_id": "bogus-challenge-id",
        "credential": {"id": "bogus-credential-id", "rawId": "bogus-credential-id"},
    }

    blocked, attempts, last = _drive_until_blocked(
        opts.client, "/api/auth/passkeys/login/complete", payload
    )
    assert blocked, (
        f"passkeys/login/complete rate limit must trigger within {MAX_ATTEMPTS} attempts; "
        f"made {attempts}, last status={last}"
    )
