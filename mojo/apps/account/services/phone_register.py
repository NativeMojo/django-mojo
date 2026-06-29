"""
Register-time phone verification service.

Verify-then-register: phone ownership is proven *before* the User row is
created. Avoids spam-driven half-registered accounts (phone numbers are a
scarce, costly resource).

Two-step Redis-backed token flow:

  start(phone)            -> (session_token, code, ttl)
      Caller (endpoint) dispatches `code` over SMS. session_token is opaque.

  verify_code(token, code) -> (verified_token, phone, ttl)
      Reads the session and compares the code; on a match, mints a
      verified_token and deletes the session (consume-on-success). A wrong
      code leaves the session intact for retry within the TTL.

  consume(token, phone)   -> bool
      Atomic getdel of verified_token. Returns True iff the verified phone
      matches the supplied phone. on_register calls this right before
      creating the user.

Redis keys:
    phone:register:session:<session_token>   = {"phone", "code", "ip", "ts"}
    phone:register:verified:<verified_token> = {"phone"}

Both keys use opaque uuid4 hex tokens. Session TTL defaults to 10 min,
verified-token TTL defaults to 10 min (configurable via settings).
"""
import json
import uuid

from mojo import errors as merrors
from mojo.helpers import crypto, dates, test_mode as _tm
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings


_SESSION_PREFIX = "phone:register:session:"
_VERIFIED_PREFIX = "phone:register:verified:"


def session_ttl():
    return settings.get("PHONE_REGISTER_SESSION_TTL", 600, kind="int")


def verified_ttl():
    return settings.get("PHONE_REGISTER_VERIFIED_TTL", 600, kind="int")


def _valid_token_hex(token):
    return (
        isinstance(token, str)
        and len(token) == 32
        and token.isalnum()
    )


def start(phone, ip=None):
    """Mint a session token and code for a phone number.

    Returns (session_token, code, ttl). Caller dispatches the SMS.
    """
    if not phone:
        raise merrors.ValueException("phone is required")
    session_token = uuid.uuid4().hex
    code = crypto.random_string(6, allow_digits=True, allow_chars=False, allow_special=False)
    payload = {
        "phone": phone,
        "code": code,
        "ip": ip or "",
        "ts": int(dates.utcnow().timestamp()),
    }
    ttl = session_ttl()
    get_connection().setex(
        f"{_SESSION_PREFIX}{session_token}",
        ttl,
        json.dumps(payload),
    )
    return session_token, code, ttl


def _dev_bypass_code(request=None):
    """Return the configured dev bypass code, or None.

    When set, this is a fixed code that verify_code() accepts in addition
    to the real generated code. Lets dev environments exercise the phone
    verify flow without a working SMS gateway. Operator-controlled trust
    model: setting alone enables the bypass; production is expected to
    leave it unset (mojo.apps.account.apps.AppConfig.ready emits a
    server-startup warning when it's non-empty).

    Honors a per-request X-Mojo-Test-Phone-Verify-Bypass-Code header
    when the test-mode gate passes (loopback + MOJO_TEST_MODE + no proxy
    chain). Used by tests so they don't require th.server_settings
    (which would force test_register to be serial).
    """
    if request is not None and _tm.is_test_request(request):
        hdr = request.META.get("HTTP_X_MOJO_TEST_PHONE_VERIFY_BYPASS_CODE")
        if hdr is not None:
            return hdr if hdr else None
    raw = settings.get("AUTH_PHONE_VERIFY_DEV_BYPASS_CODE", "")
    return raw if raw else None


def verify_code(session_token, code, request=None):
    """Verify the submitted code and, on success, consume the session.

    Returns (verified_token, phone, ttl) on success. Raises ValueException
    on missing/expired session or mismatched code. Single-use ON SUCCESS:
    the session is deleted only when the code matches, so a wrong code leaves
    the session intact and the user can retry the correct code on the same
    session_token until it succeeds or the TTL expires. Brute force is bounded
    by the endpoint rate limit (phone_register_verify, 10/60s) plus the TTL.

    Honors the AUTH_PHONE_VERIFY_DEV_BYPASS_CODE setting: when configured,
    the bypass code is accepted in addition to the real generated code.
    """
    if not _valid_token_hex(session_token):
        raise merrors.ValueException("Invalid or expired verification session")
    if not code:
        raise merrors.ValueException("Invalid code")
    raw = get_connection().get(f"{_SESSION_PREFIX}{session_token}")
    if not raw:
        raise merrors.ValueException("Invalid or expired verification session")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise merrors.ValueException("Invalid or expired verification session")
    stored_code = data.get("code")
    submitted = str(code).strip()
    bypass = _dev_bypass_code(request=request)
    real_match = stored_code and _ct_eq(submitted, stored_code)
    # Constant-time compare both branches so wrong-code timing does not
    # disclose which path matched. Use str() guards because settings may
    # return non-string types in pathological configs.
    bypass_match = bypass is not None and _ct_eq(submitted, str(bypass))
    if not (real_match or bypass_match):
        raise merrors.ValueException("Invalid code")
    phone = data.get("phone")
    if not phone:
        raise merrors.ValueException("Invalid or expired verification session")
    # Code verified — consume the session now (single-use ON SUCCESS only).
    # A wrong code above raised without deleting, so the user can retry the
    # correct code on the same session_token until it succeeds or the TTL expires.
    get_connection().delete(f"{_SESSION_PREFIX}{session_token}")
    verified_token = uuid.uuid4().hex
    ttl = verified_ttl()
    get_connection().setex(
        f"{_VERIFIED_PREFIX}{verified_token}",
        ttl,
        json.dumps({"phone": phone}),
    )
    return verified_token, phone, ttl


def consume(verified_token, phone):
    """Atomically consume a verified token; return True iff phones match.

    Single-use: the key is deleted on every call. on_register invokes this
    once per registration before creating the user.
    """
    if not _valid_token_hex(verified_token):
        return False
    raw = get_connection().getdel(f"{_VERIFIED_PREFIX}{verified_token}")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    stored_phone = data.get("phone")
    if not stored_phone or not phone:
        return False
    return _ct_eq(str(stored_phone), str(phone))


def _ct_eq(a, b):
    """Constant-time equality for short strings. Bound length check first."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
