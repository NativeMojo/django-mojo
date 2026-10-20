"""
A project's own rule for who may take part in a live topic (maestro #8874).

One question -- "may this user take part in `topic`?" -- asked after
django-mojo's own verdict, and only when that verdict allows:

- every realtime subscribe (`User.on_realtime_can_subscribe`), which is also
  the chat delivery re-check on an open socket;
- the client chat frames: send, edit, react, typing, read and flag;
- the REST room reads `room/messages`, `room/online`, `room/members` and
  `POST room/read`, and the reuse of an existing DM through `POST dm`.

Hook: `REALTIME_SUBSCRIBE_HANDLER = "myapp.realtime.can_subscribe"` in the
Django settings FILE, called as `handler(user, topic, default)` and allowing
only when it returns literal True. It is read with `settings.get_static`,
never from a database Setting row, so nobody holding `manage_settings` can
point it at importable code.

It can only narrow: when `default` refuses, the hook is never called, and
nothing it returns grants access django-mojo refused. Unset, every answer is
`default`, exactly as before. It fails closed: a load error, a non-callable,
an exception or any return other than True refuses, for every topic it is
asked about. Server code -- `send_message(client_authored=False)`, rooms and
memberships created through the ORM, the automatic own-topic subscribe and
topics a server hook subscribes a socket to -- never passes through here.

`allow_all` and `deny_all` ship for composition.
"""
from mojo.helpers import logit, modules
from mojo.helpers.settings import settings

logger = logit.get_logger("realtime", "realtime.log")

HANDLER_KEY = "REALTIME_SUBSCRIBE_HANDLER"

# Sentinel for the keyword-only test seams.
_UNSET = object()
# A configured hook that could not be loaded: refuse, never fall back.
_BROKEN = object()


def allow_all(user, topic, default):
    """Allow whatever django-mojo allows: the same as having no hook."""
    return True


def deny_all(user, topic, default):
    """Refuse every topic the hook is asked about."""
    return False


def _handler(*, handler=_UNSET, path=_UNSET):
    """Return the configured hook, None when none is set, or _BROKEN.

    `handler` and `path` are keyword-only test seams: `handler` is the callable
    itself, `path` a dotted path to load. Passing None for either means "no
    hook". Production passes neither and reads the settings file.
    """
    if handler is not _UNSET:
        if handler is None:
            return None
        return handler if callable(handler) else _BROKEN
    configured = path if path is not _UNSET else settings.get_static(HANDLER_KEY, None)
    if not configured:
        return None
    if callable(configured):
        return configured
    try:
        # Exception, not ImportError: the target module can raise anything
        # at import time.
        func = modules.load_function(str(configured))
    except Exception as e:
        logger.error(f"realtime: could not load {HANDLER_KEY} '{configured}': {e}")
        return _BROKEN
    if not callable(func):
        logger.error(f"realtime: {HANDLER_KEY} '{configured}' is not callable")
        return _BROKEN
    return func


def allows(user, topic, default, *, handler=_UNSET, path=_UNSET):
    """Allow `user` on `topic` when django-mojo's `default` allows it and the
    project's hook, if one is set, agrees.

    With no hook, `default` comes back as it was given, so the built-in checks
    answer exactly as before. A refusing `default` never reaches the hook.
    """
    func = _handler(handler=handler, path=path)
    if func is None:
        return default
    if not default:
        return False
    if func is _BROKEN:
        return False
    try:
        allowed = func(user, topic, default)
    except Exception as e:
        logger.error(f"realtime: {HANDLER_KEY} raised for {topic}, refusing: {e}")
        return False
    return allowed is True
