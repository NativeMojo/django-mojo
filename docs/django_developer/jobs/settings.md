# Jobs Settings — Django Developer Reference

All settings for the `mojo.apps.jobs` system. Add to your Django `settings.py`.

## Runtime Defaults vs Settings File

The `mojo/apps/jobs/settings.py` file is a reference showing example configurations. The **actual runtime defaults** are set in `mojo/apps/jobs/__init__.py` via `settings.get_static()`. The tables below show the real runtime defaults.

## Job Defaults

| Setting | Default | Description |
|---------|---------|-------------|
| `JOBS_DEFAULT_CHANNEL` | `"default"` | Default channel when none specified |
| `JOBS_DEFAULT_EXPIRES_SEC` | `900` (15 min) | Default job expiration in seconds |
| `JOBS_DEFAULT_MAX_RETRIES` | `0` | Default max retry attempts (no retries unless specified) |
| `JOBS_DEFAULT_BACKOFF_BASE` | `2.0` | Exponential backoff base (delay = base^attempt) |
| `JOBS_DEFAULT_BACKOFF_MAX` | `3600` (1 hr) | Max seconds between retries |
| `JOBS_PAYLOAD_MAX_BYTES` | `16384` (16KB) | Max payload size — publish raises `ValueError` if exceeded |

## Channels

| Setting | Default | Description |
|---------|---------|-------------|
| `JOBS_CHANNELS` | `DEFAULT_CHANNELS` (see below) | Channels this box **consumes** |
| `JOBS_ALLOWED_CHANNELS` | unset (monitor mode) | User channels this deployment **publishes** to — set identically on every box. **Setting it (even to `[]`) turns enforcement on** |
| `JOBS_HOSTNAME_CHANNEL` | `True` | Also consume the engine's box-direct channel (named after its runner id) |

`JOBS_CHANNELS` is a **consume** list. Publishing is gated separately: a
channel is *declared* when it is a framework channel (`DEFAULT_CHANNELS`), a
channel this box consumes, a declared user channel (`JOBS_ALLOWED_CHANNELS`),
or a box-direct channel ending `-engine`. Enforced publishing with both
`delay` and `run_at` omitted also accepts the exact id of a current runner
whose positive-TTL heartbeat advertises that same direct channel; explicit
safe runner ids do not need the suffix. With `JOBS_ALLOWED_CHANNELS` set,
anything else raises `ValueError`, queues nothing, and files a
`jobs:rejected_channel` incident (one per channel per hour) naming the channel
and the publishing function; unreadable or invalid heartbeat data does not
authorize the live-runner exception. With the setting unset (monitor mode —
the default), the publish still routes as named and a
`jobs:undeclared_channel` incident reports what to declare. Delayed jobs and
`ScheduledTask.channel` keep the static rules. A declared channel is routed
**verbatim** — consumed here or not — which is how a box hands work to another
box's dedicated channel. See [Publishing — Channels](publishing.md#channels).

The default is `mojo.apps.jobs.DEFAULT_CHANNELS` — every channel the framework
itself publishes to, so an unconfigured deployment runs all framework jobs:

```python
DEFAULT_CHANNELS = ['default', 'priority', 'cleanup', 'incident_handlers',
                    'renditions', 'certs', 'webhooks', 'webhook_fanout']
```

Set it explicitly to dedicate a box, declare the deployment's user channels
once, and run engines per channel:

```python
# every box
JOBS_ALLOWED_CHANNELS = ['emails', 'heavy']

# this box
JOBS_CHANNELS = ['default', 'emails', 'heavy']
```

```bash
python -m mojo.apps.jobs.cli engine start --channels emails
python -m mojo.apps.jobs.cli engine start --channels heavy --runner-id heavy-engine
```

`--channels` overrides `JOBS_CHANNELS` for that process; `--runner-id` gives a
second engine on the same box its own identity, pidfile, and box-direct
channel. Any safe runner id is valid. Without `-engine`, enforced publishers
can target it only with `delay` and `run_at` omitted and while its exact
heartbeat is live and self-advertises that channel.

### Upgrade note — channel routing changed

`publish()` used to reroute any channel missing from `JOBS_CHANNELS` onto
`default`, silently. Now a **declared** channel is routed verbatim, and an
undeclared one is either reported (monitor mode) or refused (enforced) —
**there is no flag day**: a deployment that has not set
`JOBS_ALLOWED_CHANNELS` upgrades into monitor mode, where every publish
keeps working and undeclared channels surface as `jobs:undeclared_channel`
incidents. What to do when upgrading:

- If you set `JOBS_CHANNELS` by hand, framework jobs now ride their own
  queues — keep the `DEFAULT_CHANNELS` entries for the features you run
  (`renditions` for fileman, `certs` for dnsman certificates — or your
  `DNSMAN_CERT_SYNC_CHANNEL` override, which also needs declaring —
  `incident_handlers`, `webhooks`, `webhook_fanout`, `cleanup`, `priority`)
  in some engine's consume list.
- Watch for `jobs:undeclared_channel` incidents — they name every channel
  your code publishes to that needs declaring (including
  `ScheduledTask.channel` values). Add them to `JOBS_ALLOWED_CHANNELS`, set
  identically on every box.
- Watch for `jobs:degraded_broadcast` incidents too — a broadcast whose runner
  roster could not be proven, so fleet-wide work may have reached fewer nodes
  than intended. Usual causes are a node whose clock runs more than one
  heartbeat window ahead, a runner that exited without deregistering, or Redis
  latency. See [publishing.md](publishing.md#how-a-broadcast-resolves-its-roster).
- Once the setting exists, enforcement is on: an undeclared publish raises
  `ValueError` with a `jobs:rejected_channel` incident and queues nothing,
  and an undeclared `ScheduledTask.channel` fails at save.

Either way misconfigurations are loud, not silent, and an
allowed-but-unconsumed queue still raises `jobs:unconsumed_channel` within
~5 minutes. Queued jobs still expire after `JOBS_DEFAULT_EXPIRES_SEC`.

### Channel names

Enforced: letters, digits, `_`, `.` and `-`, 1–100 characters.
`publish()` raises `ValueError` on anything else, and `ScheduledTask.save()`
rejects a bad or undeclared `channel` at write time. Colons are excluded
because the engine recovers the channel by splitting the queue key on `:`;
whitespace and control characters because a channel name reaches log lines
and incident titles.

The `-engine` suffix is reserved by convention for box-direct channels (every
engine consumes a channel named after its runner id): any channel ending in
`-engine` is implicitly publishable, so do not name ordinary work queues with
that suffix. It is not required for an explicit runner id: exact live runner
ids are accepted for immediate work through their heartbeat. This dynamic
proof does not apply to delayed jobs or `ScheduledTask.channel`; declare those
targets statically.

The allowlist also keeps the channel set bounded — each distinct channel
creates its own `jobs.published.<channel>` metric slug, and those slugs are
not pruned.

## Engine Configuration

Controls the job engine (runner) behavior.

| Setting | Default | Description |
|---------|---------|-------------|
| `JOBS_ENGINE_MAX_WORKERS` | `10` | Thread pool size per engine instance |
| `JOBS_ENGINE_CLAIM_BUFFER` | `2` | Claim multiplier (can claim up to `max_workers * buffer` jobs) |
| `JOBS_ENGINE_CLAIM_BATCH` | `5` | Max jobs to claim in one request |
| `JOBS_ENGINE_READ_TIMEOUT` | `100` | Redis XREADGROUP timeout in milliseconds |

## Redis Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `JOBS_REDIS_URL` | `"redis://localhost:6379/0"` | Redis connection URL |
| `JOBS_REDIS_PREFIX` | `"mojo:jobs"` | Prefix for all Redis keys |
| `JOBS_STREAM_MAXLEN` | `100000` | Max messages per Redis stream (approximate trimming) |
| `JOBS_LOCAL_QUEUE_MAXSIZE` | `1000` | Max local in-process queue size (for `publish_local`) |

Set `JOBS_REDIS_PREFIX` explicitly in your settings file. The settings
helper's attribute access returns `None` for missing keys, so the
`"mojo:jobs"` fallback in `JobKeys` never actually fires — an unset value
yields literal `None:`-rooted keys (consistent on both sides, so it works,
but not what you want).

### Pub/Sub channels and `REDIS_PUBSUB_PREFIX`

`JOBS_REDIS_PREFIX` covers storage keys and the per-runner control channel.
Three Pub/Sub channels are deliberately rooted at the **literal** `mojo:jobs`
regardless of a custom prefix (pre-existing wire behavior, preserved —
the broadcast channel is a rendezvous between independently restarted
processes and must not move on upgrade):

- `mojo:jobs:runners:broadcast` — global control/execute broadcasts
- `mojo:jobs:replies:{token}` / `mojo:jobs:ping:{token}` — one-shot reply
  channels (the name travels inside the message)

Separately, the file-static `REDIS_PUBSUB_PREFIX` (default `""`) prefixes
**every** Pub/Sub channel — runner ctl, broadcast, replies, ping — as
`{REDIS_PUBSUB_PREFIX}:{name}`. It exists for test-checkout isolation
(Redis Pub/Sub ignores database numbers); `bin/create_testproject` derives
a per-checkout value. Leave it unset in production. See
`testit/Isolation.md` — "Messaging isolation".

## Timeouts & Heartbeats

| Setting | Default | Description |
|---------|---------|-------------|
| `JOBS_IDLE_TIMEOUT_MS` | `60000` (1 min) | Consider job stuck after this many ms idle |
| `JOBS_XPENDING_IDLE_MS` | `60000` (1 min) | Reclaim jobs idle for this long |
| `JOBS_RUNNER_HEARTBEAT_SEC` | `5` | Heartbeat interval for runner liveness detection |
| `JOBS_SCHEDULER_LOCK_TTL_MS` | `5000` (5s) | Scheduler leadership lock TTL (single-leader pattern) |

## Webhook Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `JOBS_WEBHOOK_MAX_RETRIES` | `5` | Default max retries for webhook jobs |
| `JOBS_WEBHOOK_DEFAULT_TIMEOUT` | `30` | Default HTTP request timeout (seconds) |
| `JOBS_WEBHOOK_MAX_TIMEOUT` | `300` | Maximum allowed webhook timeout (seconds) |
| `JOBS_WEBHOOK_USER_AGENT` | `"Django-MOJO-Webhook/1.0"` | Outbound `User-Agent`; override to avoid advertising the framework. A caller-supplied `User-Agent` in `publish_webhook(headers=...)` still wins. |

The outbound signature **header name** is also configurable, via the framework-wide
`WEBHOOK_SIGNATURE_HEADER` setting (default `"X-Mojo-Signature"`) — not a `JOBS_*`
key, since inbound verification honors it too. See
[Webhook Signing](../account/webhook_signing.md).

## Example Configurations

### Minimal (Development)

```python
# Uses all defaults — JOBS_CHANNELS already defaults to DEFAULT_CHANNELS, so
# every framework channel is consumed; just need Redis running.
```

### Standard Production

```python
JOBS_REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
# Extends DEFAULT_CHANNELS with a custom "emails" channel — see the upgrade
# note above; omitting a DEFAULT_CHANNELS entry here means nothing consumes it.
JOBS_CHANNELS = ['default', 'priority', 'cleanup', 'incident_handlers',
                 'renditions', 'certs', 'webhooks', 'webhook_fanout', 'emails']
# Declared user channels — same value on every box, so any box may publish
# to "emails" whether or not it consumes it.
JOBS_ALLOWED_CHANNELS = ['emails']
JOBS_DEFAULT_MAX_RETRIES = 3
JOBS_DEFAULT_EXPIRES_SEC = 1800  # 30 minutes
JOBS_ENGINE_MAX_WORKERS = 20
```

### High Throughput

```python
JOBS_ENGINE_MAX_WORKERS = 50
JOBS_ENGINE_CLAIM_BUFFER = 3
JOBS_ENGINE_CLAIM_BATCH = 20
JOBS_STREAM_MAXLEN = 500000
JOBS_PAYLOAD_MAX_BYTES = 102400  # 100KB
```

### Low Latency

```python
JOBS_ENGINE_READ_TIMEOUT = 10
JOBS_ENGINE_CLAIM_BATCH = 2
JOBS_RUNNER_HEARTBEAT_SEC = 2
```

### Reliability-Focused

```python
JOBS_DEFAULT_MAX_RETRIES = 5
JOBS_DEFAULT_EXPIRES_SEC = 3600  # 1 hour
JOBS_IDLE_TIMEOUT_MS = 300000    # 5 minutes
JOBS_DEFAULT_BACKOFF_MAX = 7200  # 2 hours
```
