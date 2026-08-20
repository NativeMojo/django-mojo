"""
Step-up ("recent authentication") freshness checks.

Sensitive operations can require that the caller's JWT was minted from a genuine
authentication event within the last `FRESH_AUTH_WINDOW` seconds. The login flows
stamp an `auth_time` claim (see `jwt_login`); a silent token refresh carries that
original value forward unchanged, so freshness reflects the real last login.

Design:
  - Stamping `auth_time` is unconditional; only enforcement here is gated.
  - `FRESH_AUTH_WINDOW` default 0 => disabled (full bypass), so upgrades are inert
    until an operator opts in.
  - Only JWT ("bearer") callers are gated. API-key / other auth bypass — they are
    machine credentials with no interactive login to be "recent".
  - A missing `auth_time` (legacy token minted before this shipped) is treated as
    stale when a window is enabled — fail-closed, forcing one re-auth.
"""
import time

from mojo import errors as merrors
from mojo.apps.account.utils.jwtoken import JWToken
from mojo.helpers import test_mode as _tm
from mojo.helpers.settings import settings


def resolve_window(request=None, seconds=None):
    """The freshness window in seconds. <= 0 means disabled.

    Precedence: FRESH_AUTH_ENFORCE=False (kills every window, including a
    hard-coded one) > explicit `seconds` arg > X-Mojo-Test-Fresh-Auth-Window
    (test requests only) > FRESH_AUTH_WINDOW setting (default 0 = off).

    FRESH_AUTH_ENFORCE exists because `seconds` beats the global setting, and
    roughly twenty endpoints pass an explicit 600. That made step-up auth
    unconditional in practice: FRESH_AUTH_WINDOW=0 reads like an off switch and
    silently was not one, so an operator being re-prompted for their password
    every ten minutes had no supported way to stop it.

    It defaults to True — the current behaviour — and turning it off is a real
    reduction in security, not a cosmetic setting: it removes the re-auth
    prompt from deploy-key minting, API-key rotation, capacity changes and
    domain purchases. Set it false only where the session itself is already
    strongly protected.
    """
    if not settings.get("FRESH_AUTH_ENFORCE", True, kind="bool"):
        return 0
    if seconds is not None:
        return int(seconds)
    if request is not None and _tm.is_test_request(request):
        raw = request.META.get("HTTP_X_MOJO_TEST_FRESH_AUTH_WINDOW")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return settings.get("FRESH_AUTH_WINDOW", 0, kind="int")


def token_auth_time(request):
    """Return the `auth_time` (epoch seconds) from the request's JWT, or None.

    Decodes without signature verification — the middleware already validated the
    token to authenticate the request; here we only need the claim value.
    """
    auth_token = getattr(request, "auth_token", None)
    if not auth_token or not getattr(auth_token, "token", None):
        return None
    try:
        payload = JWToken().decode(auth_token.token, validate=False)
    except Exception:
        return None
    at = payload.get("auth_time")
    if at is None:
        return None
    try:
        return int(at)
    except (TypeError, ValueError):
        return None


def is_fresh(request, seconds=None):
    """True if the request's authentication is recent enough (or the gate is off)."""
    if request is None:
        return True
    window = resolve_window(request, seconds)
    if window <= 0:
        return True  # feature disabled — full bypass
    # Only interactive JWT logins carry auth_time; API-key/other auth bypass.
    if getattr(request, "bearer", None) != "bearer":
        return True
    at = token_auth_time(request)
    if at is None:
        return False  # legacy/missing claim — fail closed
    return (int(time.time()) - at) <= window


def require_fresh(request, seconds=None):
    """Raise ReauthRequiredException (HTTP 440) when authentication is too stale."""
    if not is_fresh(request, seconds):
        raise merrors.ReauthRequiredException()
