# Database Reader Routing

> **ASGI connection pooling.** Independently of reader routing, django-mojo
> leaves PostgreSQL connections per-request by default. Native psycopg 3
> pooling is opt-in through `DATABASE_POOL_OPTIONS` or an alias's
> `OPTIONS["pool"]`; `CONN_MAX_AGE` stays `0`, as Django requires for native
> pooling and recommends under ASGI. Explicit per-alias connection settings
> always win. See `DATABASE_POOL_OPTIONS`, `DATABASE_CONN_MAX_AGE`, and
> `DATABASE_CONN_HEALTH_CHECKS` in the [settings reference](../helpers/settings_reference.md).

django-mojo can route safe reads to a database replica without application
changes. Add the reader endpoint to the file-backed startup configuration:

```python
DATABASE_READER_HOST = "my-cluster.cluster-ro-abc123.us-west-2.rds.amazonaws.com"
```

Restart the application after changing this value. It is read while Django's
settings module imports; a database-backed `Setting` row with the same key is
ignored. Removing the line restores the existing single-database behavior:
django-mojo injects no alias, router, or middleware.

Use the Aurora cluster reader endpoint when running on Aurora. AWS maintains
that endpoint across reader instances and directs it to the writer when the
cluster has no replicas. django-mojo does not add health checks or retry a
failed reader query on primary. If the reader endpoint cannot connect, reads
raise Django's normal database connection error until the endpoint recovers or
the setting is removed.

## What is injected

At settings-load time, django-mojo copies `DATABASES["default"]` to a `reader`
alias, changes its `HOST`, and adds the test mirror. Without an explicit pool,
both writer and reader retain per-request connections:

```python
DATABASES["reader"]["TEST"]["MIRROR"] = "default"
DATABASES["reader"]["CONN_MAX_AGE"] = 0
```

It then appends `"mojo.db.router.ReaderRouter"` to `DATABASE_ROUTERS` and
prepends `"mojo.middleware.db_reader.ReaderPinMiddleware"` to `MIDDLEWARE`.
The middleware is outermost so session, authentication, view, and response
middleware database work all share one request routing scope. Existing
database routers retain first say.

Optional file-backed settings customize the derived connection:

```python
DATABASE_READER_PORT = 5432
DATABASE_POOL_OPTIONS = {
    "min_size": 1,
    "max_size": 2,
    "timeout": 5,
    "max_idle": 300,
    "max_lifetime": 1800,
}
```

`DATABASE_POOL_OPTIONS` applies to both writer and reader. Declare an explicit
`DATABASES["reader"]` alias when the reader needs different pool sizing. The
example's 30-minute maximum lifetime periodically replaces connections so an
Aurora reader endpoint can redistribute them. The legacy
`DATABASE_READER_CONN_MAX_AGE` remains available, but setting it opts the
derived reader out of native pooling.

### Pool sizing

The pool is per process and per alias. django-mojo deploys four Uvicorn workers
by default, and applications may also run job, scheduler, MCP, or custom worker
processes. Size the fleet with:

```text
nodes × database-using processes per node × sum(each alias's max_size)
```

Leave database capacity for migrations, background processes, operators, and
failover. The pool size must cover each process's concurrent database users,
and every non-request execution path must close or return its connection.
Psycopg exposes `pool_size`, `pool_available`, and `requests_waiting`; tune from
those measurements rather than raising the cap speculatively.

Realtime ASGI ORM work is bounded separately by file-only
`WS_DATABASE_WORKERS` (default `4` per ASGI process). That worker count limits
simultaneous socket-driven database demand; it does not multiply the pool's
hard connection ceiling. If the pool is smaller, excess realtime DB work waits
for a lease. If it is larger, idle allocated connections still count against
the fleet budget. Checked-out leases are `pool_size - pool_available`; a quiet
process has every allocated connection available and `requests_waiting == 0`.

### External poolers

The native pool connects directly to the configured RDS or Aurora endpoint.
RDS Proxy or PgBouncer can be added later for fleet-wide multiplexing, but they
are a separate layer rather than a replacement for ASGI-safe application
pooling. Transaction-mode poolers require
`DISABLE_SERVER_SIDE_CURSORS = True` and a session-state audit.

django-mojo's DNS and certificate services use session advisory locks across
network operations. PgBouncer transaction mode cannot safely carry those
locks. RDS Proxy preserves correctness by pinning such a client to one database
connection, which reduces multiplexing until that client disconnects. Isolate
or redesign those lock paths before putting them behind a transaction pooler.

An explicitly declared `DATABASES["reader"]` alias is preserved as-is; the
host setting remains the switch that installs routing. You can also declare
`DATABASE_ROUTERS` and `MIDDLEWARE` as lists or tuples. django-mojo rebuilds
them as lists, keeps custom routers before `ReaderRouter`, and inserts its
request middleware only when it is not already present. A configuration with
no valid `DATABASES["default"]` or no `MIDDLEWARE` is left unchanged rather
than breaking startup; `mojo.db.config.LAST_SKIP_REASON` records why reader
routing was skipped.

## Routing decisions

The router chooses in this order:

| Situation | Database |
|---|---|
| Django session or django-mojo account model read | `default` |
| Inside `use_primary()` | `default` |
| A write has pinned the current context | `default` |
| Inside `use_reader()`, outside an atomic block | `reader` |
| Outside an HTTP request scope | `default` |
| Inside `transaction.atomic()` on `default` | `default` |
| Active, safe, unpinned HTTP request | `reader` |
| Any ORM write | `default`, then pin the context |
| Any migration | `default` only |

GET, HEAD, and OPTIONS requests begin unpinned. POST, PUT, PATCH, and DELETE
requests begin pinned because they commonly fetch a row before updating it; a
replica-lagged fetch could otherwise overwrite newer primary data. Any ORM
write during a safe request also pins every later read in that request.

Every model in the central `account` app and every Django session model always
reads from primary, even inside `use_reader()`. This keeps authentication,
authorization, tenant membership, API keys, security posture, and other
account state out of the replica-lag window. It also avoids an intermittent
authentication failure immediately after login, registration, token creation,
or session creation.

`ATOMIC_REQUESTS=True` effectively keeps request reads on primary because the
request executes in an atomic block. Raw SQL is outside Django's router
contract: code using `connections["default"].cursor()` continues to use
`default`, and the router cannot observe a raw write to update its pin.

The accepted consistency trade-off is a separate GET immediately after a
successful write: it can briefly see the replica's older state. Writes,
same-request reads after writes, mutating-request fetches, credentials, and
sessions do not take that path.

## Explicit routing blocks

Background jobs, management commands, shells, and websocket consumers have no
HTTP request scope, so they default to primary. Opt a bounded, read-only block
onto the replica explicitly:

```python
from mojo.db import use_reader

with use_reader():
    rows = ReportRow.objects.filter(active=True)
```

`use_reader()` starts a fresh unpinned sub-scope and restores the caller's
state on exit. Account and session models remain on primary. A write inside it
re-pins subsequent reads, and an atomic block still wins and uses primary.

Force a freshness-critical block to primary with the companion helper:

```python
from mojo.db import use_primary

with use_primary():
    user = User.objects.get(pk=user_id)
```

Both helpers use `ContextVar` tokens, so nested scopes restore correctly across
exceptions and asgiref's sync/async boundaries. A new OS thread starts without
the request scope and therefore defaults to primary. Parallel async tasks each
receive their own context copy; a write pin set in one sibling task is not
visible to another sibling task, so views should not split dependent writes and
reads across concurrent tasks.
