# Async Database Boundary

`mojo.helpers.async_db` provides the small sync-to-async database boundary used
by realtime ASGI code. It has the same connection lifecycle as Channels'
`database_sync_to_async`, without requiring Channels:

```python
from mojo.helpers.async_db import DatabaseExecutor

executor = DatabaseExecutor(max_workers=4)
result = await executor.run(load_report, report_id)
```

`close_old_connections()` runs in the worker immediately before the callable
and again in `finally`. With Django's native psycopg pool, the final close
returns that worker's checked-out lease even when the callable raises.

The callable must be a complete synchronous database unit. Finish its
transactions, consume cursors and lazy querysets, and return plain materialized
data. Never return a model, queryset, cursor, transaction, connection wrapper,
or another object whose behavior depends on the worker thread.

## Cancellation

Cancelling the async awaiter does not stop Python already running in its worker
thread. The database lease remains checked out until the synchronous callable
really exits and the boundary's `finally` runs. Code that can block must have
its own bounded timeout; monitoring must not declare a lease returned merely
because the awaiter received `CancelledError`.

`DatabaseExecutor.shutdown()` exists for a process shutdown or for an executor
owned by a test. Do not shut down the realtime module's process-wide executor
while the ASGI process is serving traffic.
