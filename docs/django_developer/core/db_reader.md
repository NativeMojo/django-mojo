# Database Reader Routing

> **ASGI connection pooling.** Independently of reader routing, django-mojo
> leaves PostgreSQL connections per-request by default. Native psycopg 3
> pooling is an API-process-only opt-in through strict
> `DATABASE_POOL_OPTIONS`; `CONN_MAX_AGE` stays `0`, as Django requires for
> native pooling and recommends under ASGI. The laboratory supports only the
> exact `default` alias and rejects alias-embedded pool configuration. See
> `DATABASE_POOL_OPTIONS`, `DATABASE_POOL_ALIASES`, and
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
DATABASE_POOL_ALIASES = ["default"]
DATABASE_POOL_API_WORKERS = 4
DATABASE_POOL_NODE_COUNT = 2
```

These values describe a candidate; they do not authorize every process to use
it. The launcher must overwrite `MOJO_PROCESS_ROLE=api` and
`MOJO_PROCESS_LAUNCHER=asgi` before importing Django. Jobs, cron, management,
tests, preflight and missing or contradictory markers strip the candidate and
retain ordinary connections. The reader stays unpooled. The example's
30-minute maximum lifetime periodically replaces writer connections.

For the exact pooled API/ASGI process, django-mojo also injects
`mojo.middleware.database_pool.DatabasePoolErrorMiddleware` exactly once. It
sits immediately inside `CORSMiddleware` when that middleware is present, or
outermost otherwise, and always before authentication. Applications do not
add it manually. Pool activation fails closed if `MIDDLEWARE` is not a list or
tuple.

### Pool sizing

The pool is per API worker. Counts have no framework defaults: deployment must
provide exact values and prove rendered and live topology agree. Size with:

```text
nodes × API workers per node × default.max_size
```

Preflight reads PostgreSQL capacity through the same ordinary `default`
connection and permits at most `floor(max_connections × 0.60)`. It also
subtracts PostgreSQL's superuser and ordinary reserved slots, the current
server-wide connection count, and an explicit observer reserve. The allowed
candidate is the smaller of the 60-percent ceiling and real remaining
headroom. It fails before migrations, host mutation or restart when topology,
destination, ordinary connection status or capacity cannot be proved.
Psycopg exposes `pool_size`, `pool_available`, and `requests_waiting`; tune from
those measurements rather than raising the cap speculatively.

Realtime ASGI ORM work is bounded separately by file-only
`WS_DATABASE_WORKERS` (default `4` per ASGI process). That worker count limits
simultaneous socket-driven database demand; it does not multiply the pool's
hard connection ceiling. If the pool is smaller, excess realtime DB work waits
for a lease. If it is larger, idle allocated connections still count against
the fleet budget. Checked-out leases are `pool_size - pool_available`; a quiet
process has every allocated connection available and `requests_waiting == 0`.

### Identity, telemetry, and bounded failure

An enabled candidate must provide `DATABASE_POOL_IDENTITY` with exact
`project`, `node`, `application`, and `deployment` values. django-mojo derives
a stable PostgreSQL `application_name` containing that identity, process role,
alias, and PID. It is ASCII-only and hash-suffixed when needed to fit
PostgreSQL's 63-byte limit. Missing or ambiguous identity fails API startup;
disabled and non-API processes need none.

The observed PostgreSQL backend times only actual `get_new_connection()` pool
acquisitions. Before ASGI startup completes, the lifespan owner requires the
configured pool and synchronously publishes one valid public-stat snapshot;
startup fails if that evidence cannot be produced. Its sampler then reads
psycopg-pool's public `get_stats()` counters without opening the lazy pool or
acquiring a lease. It atomically writes one mode-0640 JSON snapshot per worker
beneath `MOJO_POOL_TELEMETRY_ROOT`, readable only by the API group and a local
observer identity. Counter deltas tolerate a pool reset. States are:

| State | Meaning |
|---|---|
| `cold` | The lazy pool has allocated no connections. |
| `healthy` / `healthy_idle` | Capacity is available; a full-sized pool with every lease idle is healthy, not exhausted. |
| `busy` | Some allocated leases are in use and some remain available. |
| `saturated` | No lease is available, but no waiter or interval error proves exhaustion. |
| `exhausted` | No lease is available and a waiter, pool error, or observed acquisition timeout exists. |
| `recovering` | The prior sample was exhausted and capacity is available again. |

An acquisition timeout is detected through Django's exception cause chain,
emitted once to the local atomic error file (or stderr), then re-raised
unchanged. HTTP boundaries return bounded `503`; WebSockets close with `1013`.
Those paths never attempt ORM logging, so exhaustion cannot recursively need a
second lease to report the first failure.

The injected HTTP boundary covers session/authentication database access as
well as view and response work. For a JSON request, a pool queue timeout or
rejection returns:

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 1
Content-Type: application/json

{"status":false,"error":"Database temporarily unavailable","code":503}
```

The response contains no exception or database detail. Non-pool exceptions
continue through Django's ordinary error handling, and an explicit HTML
request receives the standard `503` page through the same content-negotiation
path.

Raw ORM-capable threads must use `database_thread_target()` or
`submit_database_work()`. Both enter and leave with
`close_old_connections()`, including exception exits. This rule applies to
threads living inside an API process: process-role gating alone cannot return
their thread-local Django wrappers.

`DATABASE_POOL_LAB_PROBE_ENABLED` is absent/false by default. When an approved
lab candidate enables it, each API process exposes only a mode-0600 Unix socket
under the telemetry root. It can hold a bounded number of that exact worker's
leases, can accept a concurrent cancel command, always returns leases in a
`finally` block, and proves a fresh `SELECT 1` succeeds without restart. It has
no HTTP route and must not be enabled as ordinary production configuration.

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
routing was skipped. `LAST_POOL_PLAN` is an immutable, nonsecret snapshot
captured before role suppression; `LAST_POOL_DIAGNOSTIC` is the bounded startup
disposition.

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
