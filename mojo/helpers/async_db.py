"""Connection boundaries for synchronous Django database work off the request cycle."""

from contextlib import contextmanager
from functools import wraps

from django.db import close_old_connections


@contextmanager
def database_connection_boundary():
    """Drop dead or expired connections before the work and return them after.

    Django does this around every HTTP request (``request_started`` /
    ``request_finished``); websocket frames and executor threads fire neither
    signal, so their work needs the same boundary explicitly.
    """
    close_old_connections()
    try:
        yield
    finally:
        close_old_connections()


def database_thread_target(func):
    """Wrap a thread or executor callable in ``database_connection_boundary``."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with database_connection_boundary():
            return func(*args, **kwargs)
    return wrapped
