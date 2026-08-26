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

The same lifecycle is required for any raw thread that can touch the ORM,
including a background thread living inside an API process. Wrap a thread
target directly:

```python
from threading import Thread

from mojo.helpers.async_db import database_thread_target

thread = Thread(target=database_thread_target(write_audit_batch), daemon=True)
thread.start()
```

For an existing `ThreadPoolExecutor`, submit the complete database unit through
the companion helper:

```python
from mojo.helpers.async_db import submit_database_work

future = submit_database_work(executor, build_report, report_id)
```

`database_connection_boundary()` is the lower-level context manager used by
both helpers. Prefer the wrappers unless a callable already owns a larger
explicit entry/exit boundary. Process-role gating does not clean up Django's
thread-local connection wrapper; every ORM-capable thread needs one of these
boundaries.

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
