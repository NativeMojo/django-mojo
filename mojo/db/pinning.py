"""Context-local state for primary/reader database routing."""

from contextlib import contextmanager
from contextvars import ContextVar


_ACTIVE = ContextVar("mojo_db_reader_active", default=False)
_PINNED = ContextVar("mojo_db_reader_pinned", default=False)
_FORCED = ContextVar("mojo_db_reader_forced", default=None)


def activate(pinned=False):
    """Begin a routed request scope and return tokens for ``deactivate``."""
    active_token = _ACTIVE.set(True)
    pinned_token = _PINNED.set(pinned)
    return active_token, pinned_token


def deactivate(tokens):
    """Restore the context state that preceded ``activate``."""
    active_token, pinned_token = tokens
    _PINNED.reset(pinned_token)
    _ACTIVE.reset(active_token)


def pin():
    """Route later reads in this context to the primary database."""
    _PINNED.set(True)


def is_active():
    """Return whether the current context is an HTTP routing scope."""
    return _ACTIVE.get()


def is_pinned():
    """Return whether the current context must read from primary."""
    return _PINNED.get()


def forced_database():
    """Return the explicit routing hint for the current context, if any."""
    return _FORCED.get()


@contextmanager
def use_reader():
    """Route a bounded read-only block to the reader when it is safe."""
    forced_token = _FORCED.set("reader")
    pinned_token = _PINNED.set(False)
    try:
        yield
    finally:
        _PINNED.reset(pinned_token)
        _FORCED.reset(forced_token)


@contextmanager
def use_primary():
    """Route a bounded block to the primary database."""
    token = _FORCED.set("primary")
    try:
        yield
    finally:
        _FORCED.reset(token)
