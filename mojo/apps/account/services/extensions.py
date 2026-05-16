"""
Account extension hooks — dotted-path callables loaded from settings.

Three hooks let consumer apps inject logic into the registration and login
flows without forking framework endpoints. All hooks share the same pattern:
a single dotted-path setting points at one callable, loaded via
mojo.helpers.modules.load_function() and cached.

  PRE_REGISTER_VALIDATOR    — runs before any DB write on register; raise
                              ValueException to reject (400). The plaintext
                              password is NOT in the kwargs AND is popped
                              from request.DATA for the duration of the call
                              so the handler cannot reach it via the request.

  USER_REGISTERED_HANDLER   — runs inside the register transaction.atomic
                              block (or, for OAuth, immediately after the
                              user is created). Raising propagates and
                              rolls back the user row. Handlers must be
                              fast-path or enqueue-and-return.

  USER_LOGIN_HANDLER        — runs at the end of every successful
                              jwt_login(). Wrapped in try/except by the
                              framework — runtime errors are logged and
                              swallowed so a failing analytics handler
                              never locks a user out.

All handlers are invoked with keyword arguments only so future signature
additions don't break consumer handlers.

TEST MODE — when the test-mode gate passes (see mojo.helpers.test_mode for the
defense-in-depth checks: env var + loopback-only + no proxy chain), handler
dotted paths can be overridden per-request via headers so tests run in parallel
without server reloads:
    X-Mojo-Test-Pre-Register-Validator   → overrides PRE_REGISTER_VALIDATOR
    X-Mojo-Test-User-Registered-Handler  → overrides USER_REGISTERED_HANDLER
    X-Mojo-Test-User-Login-Handler       → overrides USER_LOGIN_HANDLER
    X-Mojo-Test-Registration-Extra-Fields → overrides REGISTRATION_EXTRA_FIELDS
                                            (JSON list of allowlisted keys)
    X-Mojo-Test-Require-Group-On-Registration → "0"/"1"
    X-Mojo-Test-Allow-User-Registration  → "0"/"1"

These headers can load arbitrary Python callables — the gate is mandatory and
production never satisfies all four conditions, so this is NOT an RCE vector
in deployed environments.
"""
import json

from mojo.helpers import modules, logit, test_mode as _tm
from mojo.helpers.settings import settings


# Cache resolved callables keyed by setting value, so a settings change
# (including test server_settings overrides) naturally resets the entry.
# Empty string / None setting → no-op (cached as the sentinel _NO_HANDLER).
_NO_HANDLER = object()
_CACHE = {}


# ---------------------------------------------------------------------------
# Test-mode helpers — gated by mojo.helpers.test_mode.is_test_request which
# enforces env var + loopback-only + no proxy chain.
# ---------------------------------------------------------------------------

def _header(request, name):
    if request is None:
        return None
    key = "HTTP_" + name.upper().replace("-", "_")
    return request.META.get(key)


def _resolve_with_override(setting_name, request, header_name):
    """Like _resolve, but consults a test-mode header first when the gate passes.

    Header can load arbitrary callables via load_function — gate MUST be
    enforced before reading the header value.
    """
    if _tm.is_test_request(request):
        h = _header(request, header_name)
        if h is not None:
            # Empty header value means "no handler"
            if not h:
                return _NO_HANDLER
            return _resolve_path(h)
    return _resolve(setting_name)


def _resolve(setting_name):
    """Return the configured callable, _NO_HANDLER, or None on broken config.

    None → broken dotted-path; caller treats as no-op but logs once per
    distinct path value via the cache.
    """
    path = settings.get(setting_name, "")
    return _resolve_path(path) if path else _NO_HANDLER


def _resolve_path(path):
    cached = _CACHE.get(path, Ellipsis)
    if cached is not Ellipsis:
        return cached
    try:
        fn = modules.load_function(path)
    except Exception as exc:
        logit.error("account.extensions", f"failed to load handler {path!r}: {exc}")
        _CACHE[path] = None
        return None
    _CACHE[path] = fn
    return fn


def list_setting_with_header(request, header_name, setting_name, default):
    """Public helper for callers (e.g. on_register's extras allowlist) to read
    a list setting with a JSON-list header override when the test gate passes."""
    if _tm.is_test_request(request):
        h = _header(request, header_name)
        if h is not None:
            try:
                value = json.loads(h)
                if isinstance(value, list):
                    return value
            except (json.JSONDecodeError, TypeError):
                pass
    return settings.get(setting_name, default) or []


def bool_setting_with_header(request, header_name, setting_name, default):
    """Public helper for boolean settings with header override when the test gate passes."""
    if _tm.is_test_request(request):
        h = _header(request, header_name)
        if h is not None:
            return h not in ("0", "false", "False", "")
    return settings.get(setting_name, default, kind="bool")


def run_pre_register_validator(*, email, group, request, extra):
    """Run PRE_REGISTER_VALIDATOR if configured.

    Raises ValueException to reject the registration (caller turns into 400).
    Other exceptions propagate as 500. Plaintext password is intentionally
    not passed — strength check stays framework-side.
    """
    fn = _resolve_with_override(
        "PRE_REGISTER_VALIDATOR", request, "X-Mojo-Test-Pre-Register-Validator")
    if fn is _NO_HANDLER or fn is None:
        return
    fn(email=email, group=group, request=request, extra=extra)


def fire_user_registered(*, user, request, group, source, extra):
    """Fire USER_REGISTERED_HANDLER.

    Runtime exceptions PROPAGATE — the caller's transaction.atomic must roll
    back the user row. Handlers should be fast-path or enqueue-and-return.
    """
    fn = _resolve_with_override(
        "USER_REGISTERED_HANDLER", request, "X-Mojo-Test-User-Registered-Handler")
    if fn is _NO_HANDLER or fn is None:
        return
    fn(user=user, request=request, group=group, source=source, extra=extra)


def fire_user_login(*, user, request, source, is_new_user):
    """Fire USER_LOGIN_HANDLER.

    Runtime exceptions are caught + logged + swallowed. Authentication must
    never break because of a login analytics / SIEM handler hiccup.
    """
    fn = _resolve_with_override(
        "USER_LOGIN_HANDLER", request, "X-Mojo-Test-User-Login-Handler")
    if fn is _NO_HANDLER or fn is None:
        return
    try:
        fn(user=user, request=request, source=source, is_new_user=is_new_user)
    except Exception as exc:
        logit.error("account.extensions",
                    f"USER_LOGIN_HANDLER raised: {exc}")
        try:
            user.report_incident(
                f"USER_LOGIN_HANDLER raised: {exc}",
                "login_handler:error",
                level=4)
        except Exception:
            pass


def _reset_cache_for_tests():
    """Test-only helper to drop cached callables. Production code never calls this."""
    _CACHE.clear()


# Re-export for convenience
__all__ = [
    "run_pre_register_validator",
    "fire_user_registered",
    "fire_user_login",
]
