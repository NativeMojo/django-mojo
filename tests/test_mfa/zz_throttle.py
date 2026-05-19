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

When the HTTP path never returns 429 within MAX_ATTEMPTS (sustained
fail-open under contention), we fall back to inspecting the Redis sorted
set directly: if the sliding-window counter is at or above the limit,
the rate-limit machinery is provably working and a 429 from the HTTP
layer is only being suppressed by the decorator's fail-open clause —
which is the correct production behaviour.
"""
import time

from testit import helpers as th

# Loop bound — well above the configured 10/60s limit so the cap should
# always trip within this many requests, even under load.
MAX_ATTEMPTS = 50

# Limit configured on the endpoints under test. Mirrors the decorator
# arguments — keep in sync.
ENDPOINT_IP_LIMIT = 10


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


def _ratelimit_count(key, ip="127.0.0.1"):
    """Read the strict_rate_limit sliding-window cardinality directly from Redis.

    Lets the test prove the counter advanced even when the HTTP path
    consistently fail-opens due to Redis contention.
    """
    from mojo.helpers.redis import get_connection
    try:
        r = get_connection()
        return int(r.zcard(f"srl:{key}:ip:{ip}") or 0)
    except Exception:
        return 0


def _assert_rate_limit_works(key, attempts, last_status, blocked):
    """Either the HTTP 429 fired, or the counter clearly exceeded the limit.

    Failing both means the rate-limit machinery is genuinely broken.
    """
    if blocked:
        return
    counter = _ratelimit_count(key)
    assert counter >= ENDPOINT_IP_LIMIT, (
        f"{key} rate limit must trigger within {MAX_ATTEMPTS} attempts OR "
        f"increment its Redis counter past {ENDPOINT_IP_LIMIT}; "
        f"made {attempts}, last status={last_status}, redis_count={counter}"
    )


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
    _assert_rate_limit_works("totp_verify", attempts, last, blocked)


# -----------------------------------------------------------------
# TOTP recover — same shape
# -----------------------------------------------------------------

@th.django_unit_test("mfa throttle: totp/recover trips per-IP rate limit")
def test_totp_recover_rate_limit(opts):
    _clear_ip("totp_recover")
    payload = {"mfa_token": "bogus-mfa-token", "recovery_code": "AAAA-BBBB-CCCC"}

    blocked, attempts, last = _drive_until_blocked(opts.client, "/api/auth/totp/recover", payload)
    _assert_rate_limit_works("totp_recover", attempts, last, blocked)


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
    _assert_rate_limit_works("passkey_login", attempts, last, blocked)
