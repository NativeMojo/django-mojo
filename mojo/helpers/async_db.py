"""Thread and ASGI boundaries for synchronous Django database work."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps

from asgiref.sync import SyncToAsync
from django.db import close_old_connections


@contextmanager
def database_connection_boundary():
    """Return every connection owned by the current thread on every exit."""
    close_old_connections()
    try:
        yield
    finally:
        close_old_connections()


def database_thread_target(func):
    """Wrap a raw thread/executor callable in Django connection hygiene."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with database_connection_boundary():
            return func(*args, **kwargs)
    return wrapped


def submit_database_work(executor, func, *args, **kwargs):
    """Submit one ORM-capable unit without leaking its worker-thread wrapper."""
    return executor.submit(database_thread_target(func), *args, **kwargs)


class DatabaseSyncToAsync(SyncToAsync):
    """Run one self-contained synchronous database unit in a chosen executor.

    This mirrors Channels' ``database_sync_to_async`` lifecycle without making
    Channels a dependency. The callable must fully materialize its result: no
    model, queryset, cursor, transaction, or connection-owned object may cross
    back to the event loop.
    """

    def __init__(self, func, executor):
        super().__init__(func, thread_sensitive=False, executor=executor)

    def thread_handler(self, loop, exc_info, task_context, func, *args, **kwargs):
        with database_connection_boundary():
            return super().thread_handler(
                loop, exc_info, task_context, func, *args, **kwargs)


class DatabaseExecutor:
    """A bounded executor dedicated to database-capable synchronous work."""

    def __init__(self, max_workers, thread_name_prefix="mojo-realtime-db"):
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )

    async def run(self, func, *args, **kwargs):
        return await DatabaseSyncToAsync(func, self._executor)(*args, **kwargs)

    def shutdown(self, wait=True, cancel_futures=False):
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
