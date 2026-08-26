"""ASGI-safe execution boundary for synchronous Django database work."""

from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import SyncToAsync
from django.db import close_old_connections


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
        close_old_connections()
        try:
            return super().thread_handler(
                loop, exc_info, task_context, func, *args, **kwargs)
        finally:
            close_old_connections()


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
