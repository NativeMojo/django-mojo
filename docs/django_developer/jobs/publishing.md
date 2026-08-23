# Publishing Jobs — Django Developer Reference

## Import

```python
from mojo.apps import jobs
```

## publish()

Enqueue a job for async execution by a runner.

```python
job_id = jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
```

### Full Signature

```python
jobs.publish(
    func,                    # str module path (preferred) or callable
    payload=None,            # dict — persisted to PostgreSQL, not Redis
    *,
    channel="default",       # queue channel name
    delay=None,              # int seconds from now
    run_at=None,             # datetime — schedule for specific time
    broadcast=False,         # True = all runners execute this job
    max_retries=None,        # int — override default (0)
    backoff_base=None,       # float — override default (2.0)
    backoff_max=None,        # int seconds — override default (3600)
    expires_in=None,         # int seconds until expiration
    expires_at=None,         # datetime — specific expiration time
    max_exec_seconds=None,   # int — execution time limit (advisory)
    idempotency_key=None,    # str — prevent duplicate execution
)
```

**Returns**: Job ID string (32-char UUID without dashes).

**Raises**: `ValueError` for invalid params, `RuntimeError` on publish failure.

### Parameter Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `func` | str or callable | *required* | Dotted module path to job function |
| `payload` | dict | `None` | Input data (stored in DB, passed as `job.payload`) |
| `channel` | str | `"default"` | Queue channel — routes to specific workers |
| `delay` | int | `None` | Seconds from now to execute |
| `run_at` | datetime | `None` | Specific UTC time to execute |
| `broadcast` | bool | `False` | Execute on ALL runners for this channel |
| `max_retries` | int | `0` | Max retry attempts on failure |
| `backoff_base` | float | `2.0` | Exponential backoff base (delay = base^attempt) |
| `backoff_max` | int | `3600` | Max seconds between retries |
| `expires_in` | int | `None` | Seconds until job expires if not executed |
| `expires_at` | datetime | `None` | Specific expiration time |
| `max_exec_seconds` | int | `None` | Execution time limit (advisory — not enforced by engine) |
| `idempotency_key` | str | `None` | Unique key — duplicate publishes are silently ignored |

### String Path (Preferred)

Always use string paths for the `func` argument:

```python
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
jobs.publish("myapp.services.export.generate_report", {"report_id": 7})
jobs.publish("myapp.services.cleanup.purge_expired", {"days_old": 30})
```

String paths are cleaner, avoid circular imports, and make it obvious where the code lives.

### Examples

```python
# Basic job
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})

# Delayed job (run in 5 minutes)
jobs.publish("myapp.services.reminder.send", {"user_id": 42}, delay=300)

# Scheduled job (specific time)
from mojo.helpers import dates
jobs.publish(
    "myapp.services.report.generate",
    {"report_id": 7},
    run_at=dates.add(dates.utcnow(), hours=1),
)

# With retries and backoff
jobs.publish(
    "myapp.services.payment.charge",
    {"payment_id": 55},
    max_retries=5,
    backoff_base=2.0,
    backoff_max=3600,
)

# Idempotent (won't create duplicate)
jobs.publish(
    "myapp.services.billing.invoice",
    {"user_id": 42, "month": "2026-03"},
    idempotency_key="invoice_42_2026_03",
)

# Broadcast to all runners
jobs.publish(
    "myapp.services.cache.clear_local",
    {"prefix": "user_*"},
    broadcast=True,
)

# Specific channel with expiration
jobs.publish(
    "myapp.services.export.generate_csv",
    {"export_id": 12},
    channel="heavy",
    expires_in=1800,  # expire if not picked up in 30 min
)
```

## publish_local()

Execute a job in a thread in the current process. No runner needed — useful for dev/testing.

```python
jobs.publish_local("myapp.services.email.send_welcome", {"user_id": 42})
```

### Signature

```python
jobs.publish_local(
    func,           # str module path or callable
    *args,          # positional args (payload dict)
    run_at=None,    # datetime — sleep until this time
    delay=None,     # int seconds — sleep before executing
    **kwargs,       # additional keyword args
)
```

**Returns**: Pseudo job ID string (for compatibility).

The function is imported and called directly in a new thread. If `delay` or `run_at` is set, the thread sleeps first.

## publish_webhook()

Publish an HTTP POST webhook as a job with automatic retries.

```python
from mojo.apps.jobs import publish_webhook

job_id = publish_webhook(
    url="https://api.partner.com/webhooks/order",
    data={"order_id": 99, "event": "created"},
)
```

Full reference: [webhooks.md](webhooks.md)

### Signature

```python
publish_webhook(
    url,                     # str — target URL (must start with http:// or https://)
    data,                    # dict — JSON data to POST
    *,
    group=None,              # account.Group or int id — when set, the handler
                             #   signs at delivery (see "Signed webhooks" below)
    headers=None,            # dict — additional HTTP headers
    channel="webhooks",      # str — job channel
    delay=None,              # int seconds
    run_at=None,             # datetime
    timeout=30,              # int seconds — HTTP request timeout
    max_retries=None,        # int — default 5 for webhooks
    backoff_base=None,       # float — default 2.0
    backoff_max=None,        # int — default 3600
    expires_in=None,         # int seconds
    expires_at=None,         # datetime
    idempotency_key=None,    # str
    webhook_id=None,         # str — custom identifier for tracking
)
```

**Returns**: Job ID string.

**Raises**: `ValueError` for invalid URL or non-serializable data, `RuntimeError` on failure.

Internally creates a job that calls `mojo.apps.jobs.handlers.webhook.post_webhook`.

### Signed webhooks

Pass `group=<account.Group or id>` to have the job handler sign the outbound body at delivery time:

```python
jobs.publish_webhook(
    url=receiver_url,
    data={"event": "verification_complete", "customer_id": 42},
    group=customer.group,
)
```

The handler:

1. Stores **only** `sign_group_id` in the queue — the secret never enters the payload.
2. At delivery, canonicalizes the body: `json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")`.
3. Computes the signature header `X-Mojo-Signature: <hex>` (name configurable via the `WEBHOOK_SIGNATURE_HEADER` setting) keyed on the Group's webhook secret (auto-minted on first use).
4. Sends those exact bytes via `requests.post(..., data=body_bytes)` — signature and wire bytes are guaranteed identical.

If the Group has been deleted between publish and delivery, the handler returns `'failed'` with `error_type='sign_group_missing'` — no retry, no silent unsigned send.

Retries re-sign with the current secret, so an in-flight job that hits a rotation is delivered with the new signature.

Full spec: [Webhook Signing](../account/webhook_signing.md).

## broadcast_execute()

Execute a function on ALL active runners without creating a Job record. This is a real-time control-channel operation.

```python
results = jobs.broadcast_execute(
    "myapp.services.cache.clear_all",
    data={"prefix": "user_*"},
    collect_replies=True,
    timeout=5.0,
)
```

### Signature

```python
jobs.broadcast_execute(
    func_path,              # str — dotted path to function
    data=None,              # dict — passed to the function
    timeout=2.0,            # float seconds — wait for responses
    collect_replies=False,  # bool — True to gather return values
)
```

**Returns**: List of dicts, one per responding runner:
```python
[
    {"runner_id": "runner-host1-abc", "func": "...", "status": "success", "result": {...}},
    {"runner_id": "runner-host2-def", "func": "...", "status": "error", "error": "..."},
]
```

Empty list if no runners respond.

### Use Cases

- Cache invalidation across all runners
- Config reload
- Collecting system info (`jobs.get_sysinfo()` uses this internally)

## Channels

A channel is a named queue. **A declared channel gets the job exactly as
named — never rerouted** — and it does not have to be one this box consumes.
That is the whole point: it is how one box gives work to another.

```python
# Lands on "sites" even if this box's JOBS_CHANNELS does not include it —
# as long as "sites" is declared in JOBS_ALLOWED_CHANNELS.
jobs.publish("myapp.services.deploy.run", {"site_id": 7}, channel="sites")
```

A channel may be published to when it is any of:

- a framework channel (`mojo.apps.jobs.DEFAULT_CHANNELS`) — always allowed,
- a channel **this box consumes** (`JOBS_CHANNELS`),
- a declared user channel (`JOBS_ALLOWED_CHANNELS` — one list, set the same
  on every box),
- a box-direct channel ending in `-engine` (see
  [Targeting one specific engine](#targeting-one-specific-engine)).

What happens to anything else depends on whether the deployment has opted
into enforcement — **setting `JOBS_ALLOWED_CHANNELS` (any list, even `[]`) is
the opt-in**:

- **Enforced** (setting present): `publish()` raises `ValueError`, creates
  **no** job, and files a `jobs:rejected_channel` incident naming the channel
  and the publishing function (suppressed to one event per channel per hour).
  A developer publishing a job knows the channel at code-writing time, so
  declaring it is one settings line — and a typo fails at the call site
  instead of stranding work on a queue nobody consumes.
- **Monitor** (setting absent — the default, and what every existing
  deployment upgrades into): the job still routes exactly as named, and a
  `jobs:undeclared_channel` incident (same suppression) tells you which
  channel to declare. Nothing breaks on upgrade; the incidents write your
  channel list for you. Declare it and you get enforcement.

The name itself is also validated — letters, digits, `_`, `.` and `-`, up to
100 characters — because it becomes a Redis key, a metric slug and an
incident title; anything else raises `ValueError`.

`JOBS_CHANNELS` is a **consume** list: what this box's engine pulls from. Run
engines per channel with the jobs CLI:

```bash
python -m mojo.apps.jobs.cli engine start --channels emails
python -m mojo.apps.jobs.cli engine start --channels heavy --runner-id heavy-engine
```

`--channels` overrides `JOBS_CHANNELS` for that process, which is what lets a box
consume a narrower set than it publishes to. `--runner-id` gives a second engine
on the same host its own identity and pidfile.

### Cross-box routing

An API box that also runs an engine, handing deploys to a dedicated worker box.
Both boxes share the same declaration:

```python
# Every box — the deployment's user channels, declared once
JOBS_ALLOWED_CHANNELS = ["sites"]
```

```python
# API box — publishes to "sites", never consumes it
JOBS_CHANNELS = ["default"]
```

```python
# Worker box — consumes only "sites"
JOBS_CHANNELS = ["sites"]
```

```bash
# ...or without touching settings on the worker box:
python -m mojo.apps.jobs.cli engine start --channels sites
```

Nothing on the API box can claim a `sites` job, so the work runs where it must.
(Publishing to a channel the box itself consumes needs no
`JOBS_ALLOWED_CHANNELS` entry — the consume list is part of the allow union —
but declare shared channels once, identically everywhere, and the topology
stays obvious.)

### Targeting one specific engine

Every engine also consumes a channel named after its **runner id** — by
default the hostname (lowercased, `.`/`_` → `-`) plus `-engine`, the same id
you see in its heartbeat, pidfile and logs. Channels ending in `-engine` are
implicitly allowed — hostnames vary per deployment and cannot live in a
hand-written list — so you can address a single engine with no configuration
at all:

```python
jobs.publish("myapp.services.cache.purge", {}, channel="web-01-engine")
```

A second engine started with `--runner-id heavy-engine` gets its own direct
channel `heavy-engine`. A mistyped host channel passes the allowlist (the
suffix is the rule) and is caught by the unconsumed-channel incident below.
Set `JOBS_HOSTNAME_CHANNEL = False` to opt an engine out of consuming its
direct channel — note that this also disables broadcast fan-out for that
engine, since the fan-out addresses exactly those channels.

### How a broadcast resolves its roster

`broadcast=True` fans out one ordinary job per live runner, so the roster
read decides who receives fleet-wide work. It uses the **exact** reader,
`get_runners_bounded`, which queries one dedicated per-channel index
primary-only and raises rather than returning a short list — not
`get_runners`, which SCANs the shared keyspace and swallows every error into
an empty list. An empty list is indistinguishable from "no runners", and that
is how a Redis blip used to turn fleet-wide work back into a unicast.

When the roster cannot be proven, `publish()` never raises — a raising
publish inside the cron dispatcher would skip every later scheduled function
that minute. Instead it:

- files a suppressed `jobs:degraded_broadcast` incident naming the channel,
  the publisher, and the fault (at most one per channel per hour, and dropped
  entirely if Redis itself is unreachable, so an outage cannot become a
  flood);
- falls back to `get_runners` **only** for `runner_roster_invalid` and
  `runner_roster_overflow`, where Redis answered promptly and the cheap reader
  is likely to return the better roster. For `runner_roster_timeout` it does
  not fall back at all: answering "Redis is slow" with an unbounded keyspace
  scan on the process pool, whose socket timeout defaults to 60 seconds, is
  worse than not answering;
- logs the two outcomes differently. "No live runners" and "roster
  UNREADABLE" are not the same event.

The bound is 512 runners per channel; past that the roster counts as
unreadable rather than being silently truncated. The likeliest real causes of
an unprovable roster are a node whose clock runs more than one heartbeat
window ahead, and a runner that exited without deregistering.

`edge`'s `convergence.publish_pool` hand-rolls the same fan-out for pool
generations and shares this exact policy.

### When nobody is listening

Declared is not the same as consumed: you can allowlist `emails` and still
forget to run an engine for it. A job published to an allowed channel no
engine consumes waits on its queue, and within about five minutes the
framework raises a `jobs:unconsumed_channel` incident naming the channel and
its depth — so a missing worker (or a typo'd `-engine` channel) surfaces as an
alert rather than as work that silently ran in the wrong place. Queued jobs
still expire after `JOBS_DEFAULT_EXPIRES_SEC`, so treat that incident as
actionable.

The alert does not repeat every cycle: an unchanged backlog is re-reported only
when its depth changes or after an hour, and if many channels are orphaned at
once (engines down fleet-wide) the five deepest are named individually and the
rest are collapsed into one summary event.

A channel with a live consumer is never reported — a backlog there is a capacity
question, not a routing mistake.

### Framework channels

`JOBS_CHANNELS` defaults to `mojo.apps.jobs.DEFAULT_CHANNELS`, which covers every
channel the framework publishes to (`default`, `priority`, `cleanup`,
`incident_handlers`, `renditions`, `certs`, `webhooks`, `webhook_fanout`) — so an
unconfigured deployment runs all of it. If you set `JOBS_CHANNELS` explicitly,
see the [upgrade note](settings.md#upgrade-note--explicit-jobs_channels).

The `webhook_fanout` channel is used by the framework's `WebhookSubscription` fan-out dispatcher (see [account — Webhook Subscriptions](../account/webhook_subscriptions.md)). It executes the DB query + per-row enqueue step; individual HTTP deliveries run on the `webhooks` channel. Keeping them on separate channels prevents fan-out coordination work from competing with HTTP delivery slots under load.

Default channel is `"default"`.

## Payload Best Practices

Payloads are persisted to PostgreSQL. Max size is **16KB** by default (`JOBS_PAYLOAD_MAX_BYTES`).

```python
# Good — pass IDs, fetch in job
jobs.publish("myapp.services.order.process", {"order_id": 42})

# Bad — large objects in payload
jobs.publish("myapp.services.order.process", {"order": huge_dict})
```

Always pass identifiers and fetch the data inside the job function.

## Other Functions

### status()

```python
info = jobs.status(job_id)
# Returns dict: {id, status, channel, func, created, started_at, finished_at, attempt, last_error, metadata}
# Returns None if not found
```

### cancel()

```python
success = jobs.cancel(job_id)
# Returns True if cancel requested, False if not found or already terminal
```

Sets `cancel_requested=True` on the job. The running function must check via `job.check_cancel_requested()`.

### get_runners()

```python
runners = jobs.get_runners(channel=None)
# Returns list of dicts with runner info and heartbeat data
```

Fleet safety paths use `jobs.get_runners_bounded(channel, limit=128,
timeout=1.0)`. Engines maintain one timestamped runner index per consumed
channel, so this bounded form reads at most `limit + 1` recent ids from the
Redis primary and pipelines only their heartbeat documents; it does not scan
unrelated Redis keys or count runners on other channels. Missing, malformed,
mismatched, stale, or implausibly future-dated declarations, overflow, and
timeout raise rather than returning a possibly incomplete roster. Registry keys
have a bounded expiry so channels abandoned by crashed runners do not
accumulate. Existing `get_runners()` callers are unchanged. The legacy
`max_scan_pages` argument remains accepted by the bounded form for compatibility
but no longer affects discovery.

### get_sysinfo()

```python
info = jobs.get_sysinfo(runner_id=None, timeout=5.0)
# Returns list of dicts with CPU, memory, disk, network info per runner
```

Requires `psutil` installed on runners.

### broadcast_command()

```python
responses = jobs.broadcast_command("status", timeout=2.0)
# Commands: "status", "shutdown", "pause", "resume"
```

### ping()

```python
alive = jobs.ping("runner-host1-abc123", timeout=2.0)
# Returns True/False
```
