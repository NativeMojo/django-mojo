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
"""
from mojo.helpers import modules, logit
from mojo.helpers.settings import settings


# Cache resolved callables keyed by setting value, so a settings change
# (including test server_settings overrides) naturally resets the entry.
# Empty string / None setting → no-op (cached as the sentinel _NO_HANDLER).
_NO_HANDLER = object()
_CACHE = {}


def _resolve(setting_name):
    """Return the configured callable, _NO_HANDLER, or None on broken config.

    None → broken dotted-path; caller treats as no-op but logs once per
    distinct path value via the cache.
    """
    path = settings.get(setting_name, "")
    if not path:
        return _NO_HANDLER
    cached = _CACHE.get(path, Ellipsis)
    if cached is not Ellipsis:
        return cached
    try:
        fn = modules.load_function(path)
    except Exception as exc:
        logit.error("account.extensions",
                    f"failed to load {setting_name}={path!r}: {exc}")
        _CACHE[path] = None
        return None
    _CACHE[path] = fn
    return fn


def run_pre_register_validator(*, email, group, request, extra):
    """Run PRE_REGISTER_VALIDATOR if configured.

    Raises ValueException to reject the registration (caller turns into 400).
    Other exceptions propagate as 500. Plaintext password is intentionally
    not passed — strength check stays framework-side.
    """
    fn = _resolve("PRE_REGISTER_VALIDATOR")
    if fn is _NO_HANDLER or fn is None:
        return
    fn(email=email, group=group, request=request, extra=extra)


def fire_user_registered(*, user, request, group, source, extra):
    """Fire USER_REGISTERED_HANDLER.

    Runtime exceptions PROPAGATE — the caller's transaction.atomic must roll
    back the user row. Handlers should be fast-path or enqueue-and-return.
    """
    fn = _resolve("USER_REGISTERED_HANDLER")
    if fn is _NO_HANDLER or fn is None:
        return
    fn(user=user, request=request, group=group, source=source, extra=extra)


def fire_user_login(*, user, request, source, is_new_user):
    """Fire USER_LOGIN_HANDLER.

    Runtime exceptions are caught + logged + swallowed. Authentication must
    never break because of a login analytics / SIEM handler hiccup.
    """
    fn = _resolve("USER_LOGIN_HANDLER")
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
