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

    Precedence: explicit `seconds` arg > X-Mojo-Test-Fresh-Auth-Window (test
    requests only) > FRESH_AUTH_WINDOW setting (default 0 = off).
    """
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
