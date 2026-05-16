"""
Register-time phone verification service.

Verify-then-register: phone ownership is proven *before* the User row is
created. Avoids spam-driven half-registered accounts (phone numbers are a
scarce, costly resource).

Two-step Redis-backed token flow:

  start(phone)            -> (session_token, code, ttl)
      Caller (endpoint) dispatches `code` over SMS. session_token is opaque.

  verify_code(token, code) -> (verified_token, phone, ttl)
      Atomic getdel of session; on success mints a verified_token.

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
from mojo.helpers import crypto, dates
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


def verify_code(session_token, code):
    """Atomically consume the session and mint a verified token.

    Returns (verified_token, phone, ttl) on success. Raises ValueException
    on missing session, expired session, or mismatched code. Single-use:
    the session is deleted whether the code matches or not (rate-limit at
    the endpoint layer prevents brute force).
    """
    if not _valid_token_hex(session_token):
        raise merrors.ValueException("Invalid or expired verification session")
    if not code:
        raise merrors.ValueException("Invalid code")
    raw = get_connection().getdel(f"{_SESSION_PREFIX}{session_token}")
    if not raw:
        raise merrors.ValueException("Invalid or expired verification session")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise merrors.ValueException("Invalid or expired verification session")
    stored_code = data.get("code")
    submitted = str(code).strip()
    # Constant-time comparison for resistance to timing oracles.
    if not stored_code or not _ct_eq(submitted, stored_code):
        raise merrors.ValueException("Invalid code")
    phone = data.get("phone")
    if not phone:
        raise merrors.ValueException("Invalid or expired verification session")
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
