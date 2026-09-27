# Async Database Boundary

`mojo.helpers.async_db` gives synchronous ORM work that runs off the HTTP
request cycle the same connection lifecycle Django applies around every
request. Websocket frames and executor threads fire neither
`request_started` nor `request_finished`, so without it a thread keeps its
first connection forever — including after the database server has closed it.

```python
from mojo.helpers.async_db import database_connection_boundary, database_thread_target

# Around a block on the current thread
with database_connection_boundary():
    user = User.objects.filter(pk=user_id).first()

# Around a callable handed to a thread or executor
result = await loop.run_in_executor(None, database_thread_target(load_report), report_id)
```

`close_old_connections()` runs before the work and again in `finally`:

- Before: a connection that errored and is no longer usable, or has passed
  `CONN_MAX_AGE`, is closed, so the work gets a fresh one.
- After: with `CONN_MAX_AGE = 0` (the framework default) the connection is
  closed; with a psycopg pool (`DATABASE_POOL_OPTIONS`) it is returned to the
  pool, even when the work raised.

Cost: with `CONN_MAX_AGE = 0`, every wrapped call that queries the database
opens one connection, the same as an HTTP request. A wrapped call that makes
no query opens nothing. With a pool the cost is a pool lease.

The wrapped work must be self-contained: finish its transactions and return
plain data, not a lazy queryset or cursor that would query later on another
thread.

Cancelling the awaiter does not stop a function already running in a worker
thread. Its connection is returned when the function really exits.

The realtime app uses this for bearer authentication and every hook and
permission check it runs in an executor. See
[Realtime Architecture](../realtime/architecture.md#database-connections).
