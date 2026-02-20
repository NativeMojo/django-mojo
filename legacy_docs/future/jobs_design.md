# Django-MOJO **Jobs** (v2) — Requirements & Design

> Renamed from “Tasks” → **Jobs** for clarity. Core entities: **Job**, **JobEvent**, **JobManager**, **JobEngine** (runner).

---

## 0) Scope (what we’re building)
A **small, reliable background job system for Django**:
- **Fast path** in **Redis** (Streams + ZSETs).
- **Truth & audit** in **Postgres** (lean models).
- **Multiple workers per channel**; plus **broadcast jobs** (every worker runs it).
- **Delayed jobs** (run_at / delay).
- **Retries with backoff**.
- **Cooperative cancel**; **hard-kill** via subprocess + max execution time.
- **Expiration**: jobs auto-fail if not executed by `expires_at`.
- **Local jobs**: simple in-process queue for tiny work.
- **Simple API + decorators**.
- **Runner stays as a daemon**: `JobEngine` is a long-running process with a `__main__` entry point for easy exec and supervision.

No cron/recurring (lives elsewhere). No priorities, no rate limits (by design).

---

## 1) Non-Goals
- Exactly-once delivery (we use at-least-once; handlers must be idempotent).
- Cross-job workflows/dependencies.
- ASGI/WebSocket coupling.
- Fancy metrics stack (we emit to your `metrics.record`).

---

## 2) Glossary
- **Channel**: logical queue name.
- **Broadcast**: a job each runner on a channel must process once.
- **JobEngine**: runner process consuming jobs.
- **Scheduler**: process moving due jobs from ZSET into Streams.

---

## 3) High-Level Architecture

**Redis (runtime, ephemeral)**
- **Streams** per channel:
  - `stream:{channel}` (normal work) with consumer group `cg:{channel}:workers`.
  - `stream:{channel}:broadcast` with **per-runner** group `cg:{channel}:runner:{runner_id}`.
- **ZSET** delays: `sched:{channel}`; score = `run_at` (ms).
- **HASH** per job: `job:{id}` -> status/attempt/cancel/runner_id/expires_at/max_exec.
- **LOCK** for scheduler leadership: `lock:jobs:scheduler` (SET NX PX).

**Postgres (truth + audit)**
- `Job` (header/current state; small).
- `JobEvent` (append-only, small details; retention).

---

## 4) Data Model (Postgres)

```python
# jobs/models.py
from django.db import models

class Job(models.Model):
    id = models.CharField(primary_key=True, max_length=32)  # uuid w/o dashes
    channel = models.CharField(max_length=100, db_index=True)
    func = models.CharField(max_length=255, db_index=True)      # registry key
    payload = models.JSONField(default=dict, blank=True)        # small only

    status = models.CharField(
        max_length=16, db_index=True,
        choices=[('pending','pending'),('running','running'),
                 ('completed','completed'),('failed','failed'),
                 ('canceled','canceled')],
        default='pending'
    )

    # scheduling / retries
    run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attempt = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    backoff_base = models.FloatField(default=2.0)
    backoff_max_sec = models.IntegerField(default=3600)

    # behavior flags
    broadcast = models.BooleanField(default=False, db_index=True)
    cancel_requested = models.BooleanField(default=False)
    max_exec_seconds = models.IntegerField(null=True, blank=True)  # hard limit
    runner_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    # diagnostics (latest only)
    last_error = models.TextField(blank=True, default="")
    stack_trace = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    idempotency_key = models.CharField(max_length=64, null=True, blank=True, unique=True)


class JobEvent(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    channel = models.CharField(max_length=100, db_index=True)
    event = models.CharField(max_length=24, db_index=True)  # created/queued/running/retry/canceled/completed/failed/expired
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    runner_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    attempt = models.IntegerField(default=0)
    details = models.JSONField(default=dict, blank=True)  # keep tiny
```
Notes:
- Keep latest error/trace on `Job`. `JobEvent` is tiny and pruned by retention.

---

## 5) Redis Keys (centralized)

Prefix configurable (`JOBS_REDIS_PREFIX`, default `mojo:jobs`).

```
stream(channel)            -> f"{P}:stream:{channel}"
stream_broadcast(channel)  -> f"{P}:stream:{channel}:broadcast"
group_workers(channel)     -> f"{P}:cg:{channel}:workers"
group_runner(c, rid)       -> f"{P}:cg:{c}:runner:{rid}"
sched(channel)             -> f"{P}:sched:{channel}"
job(job_id)                -> f"{P}:job:{job_id}"
runner_ctl(rid)            -> f"{P}:runner:{rid}:ctl"
runner_hb(rid)             -> f"{P}:runner:{rid}:hb"
scheduler_lock()           -> f"{P}:lock:scheduler"
```

---

## 6) Public API (usable from app code)

```python
# mojo/apps/jobs/__init__.py
def publish(func, payload, *, channel="default", delay=None, run_at=None,
            broadcast=False, max_retries=3, backoff_base=2.0, backoff_max=3600,
            expires_in=None, max_exec_seconds=None, idempotency_key=None) -> str: ...

def publish_local(func, *args, **kwargs): ...

def cancel(job_id: str) -> bool: ...
def status(job_id: str) -> dict: ...     # fast via Redis, DB fallback

def async_job(channel="default", broadcast=False, **defaults):
    """Decorator to register a function as a job by name."""
    ...

def local_async_job(**defaults):
    """Decorator for in-process queue."""
    ...
```

### JobContext passed to handlers
```python
class JobContext:
    job_id: str
    channel: str
    payload: dict

    def should_cancel(self) -> bool: ...
    def set_metadata(self, **kv): ...
    def get_model(self) -> Job: ...       # lazy ORM fetch
```
> We deliberately do not pass the ORM row directly. If needed, call `ctx.get_model()`.

---

## 7) Core Processes

### 7.1 Scheduler (single active)
- Acquire `SET lock:scheduler <uuid> NX PX=5000`; renew every ~2s. Exit on loss.
- Loop per channel:
  - `ZPOPMIN sched:{channel}` while score <= now:
    - Load `job:{id}` (or DB) -> if `expires_at < now`: mark failed (DB+event), skip.
    - Else `XADD stream` (or `stream_broadcast` if broadcast).
- Sleep with jitter (e.g., 200–500ms).
- Call `django.db.close_old_connections()` each loop.

### 7.2 JobEngine (runner daemon; multiple per channel)
- Daemonization: Long-running process with `if __name__ == "__main__": main()` entrypoint. Stays in foreground for supervision (systemd, Docker, k8s); handles SIGTERM/SIGINT for graceful stop.
- Startup:
  - Ensure consumer groups exist:
    - `cg:{channel}:workers` on `stream:{channel}`
    - `cg:{channel}:runner:{rid}` on `stream:{channel}:broadcast`
  - Register heartbeat `runner:{rid}:hb` (TTL; renewed every few seconds).
- Main loop:
  - `XREADGROUP BLOCK` from both streams.
  - For each msg:
    - Extract `job_id`.
    - Read `job:{id}`; if expired, `XACK` and mark failed.
    - Mark running: update Redis hash; optionally DB (`status/runner_id/started_at`).
    - Execute handler:
      - Provide `JobContext`.
      - Call `close_old_connections()` before and after execution.
      - For hard limits: run job in subprocess if `max_exec_seconds` set; soft cancel first; SIGTERM->SIGKILL if overdue.
      - Periodically check `ctx.should_cancel()`.
    - On success: `XACK`, set completed (Redis->DB), `JobEvent('completed')`.
    - On failure: compute backoff + jitter; `XACK`, `ZADD sched:{channel}`; increment attempt (Redis; DB when terminal).
- Control channel `runner:{rid}:ctl`:
  - `ping` -> reply `pong`.
  - `shutdown` -> finish current job, exit gracefully.



### Daemons 

#### **Components**



- **JobEngine (Runner)**

  - Long-running daemon that claims jobs from Redis Streams and executes handlers.
  - Supports cooperative cancel, retries/backoff, hard-kill via subprocess + max_exec_seconds.
  - Emits metrics and heartbeats; responds to control commands (ping/shutdown).

  

- **Scheduler**

  - Moves due jobs from Redis ZSET (sched:{channel}) into Streams.
  - Enforces expires_at at pop time (skip + mark failed if expired).
  - Single active leader via Redis lock (SET lock:scheduler NX PX=… + renew).



#### **Deployment Modes**



1. **Separate daemons (recommended for prod)**

   

   - Run jobs-engine (N replicas) and jobs-scheduler (1 replica).
   - Pros: clear separation, independent scaling/restarts.

   

2. **Combined mode (dev/small installs)**

   

   - jobs-engine --with-scheduler starts a background scheduler thread in the same process.
   - Guarded by the same Redis leadership lock; only one scheduler is active cluster-wide.



#### **CLI / Entry Points**



- python manage.py jobs-engine [--channels default,emails] [--runner-id auto] [--with-scheduler]
- python manage.py jobs-scheduler [--channels default,emails]
- Flags:
  - --channels: comma-separated list to serve/schedule (default: all configured).
  - --with-scheduler: (engine only) spawn scheduler thread.
  - --runner-id: explicit or auto-generated <host>-<pid>-<rand>.



#### **Lifecycle & Control**



- **JobEngine**

  - Startup: ensure consumer groups; start heartbeat key (runner:{id}:hb, TTL); subscribe to control channel (runner:{id}:ctl).
  - Loop: XREADGROUP BLOCK from normal & broadcast streams; execute jobs; close_old_connections() pre/post execution.
  - Signals: SIGTERM/SIGINT → stop accepting new messages, finish current, flush, exit.
  - Control cmds: ping → pong; shutdown → graceful stop.

  

- **Scheduler**

  - Acquire/renew lock every ~2s; on loss → exit.
  - Loop: ZPOPMIN sched:{channel} while score ≤ now; skip expired; XADD due jobs; sleep with jitter; close_old_connections() each loop.

  



#### **Locking & Leadership**

- Key: lock:scheduler (prefix-aware).
- Acquire: SET key <uuid> NX PX=<ttl_ms>.
- Renew: PEXPIRE key <ttl_ms> (only if value matches <uuid> to avoid stealing).
- Fallback: if renew fails, scheduler exits cleanly; another instance can assume leadership.



#### **Health, Metrics, and Introspection**

- **Heartbeats**

  

  - Runner: runner:{id}:hb (TTL), refreshed every JOBS_RUNNER_HEARTBEAT_SEC.
  - Scheduler: jobs.scheduler.leader gauge=1 when holding lock.

  

- **Metrics** (via metrics.record)

  

  - Counters: jobs.published, jobs.completed, jobs.failed, jobs.retried, jobs.expired.
  - Timings: jobs.duration_ms (tags: channel, func).
  - Gauges: per-channel jobs.queue.stream_len, jobs.queue.sched_len, jobs.xpending, jobs.runner.count.

  

- **Manager APIs**

  

  - get_runners(channel): from heartbeats & registration.
  - get_queue_state(channel): XINFO, XPENDING summary, ZCARD.
  - ping(runner_id), shutdown(runner_id, graceful=True).

  

#### **Failure Modes & Recovery**

- **Runner crash**: pending deliveries visible in XPENDING; other runners can XCLAIM after idle timeout.
- **Scheduler crash**: lock expires; another instance takes over; due jobs are still in ZSET.
- **Redis restart/data loss**: on startup, JobEngine reconciles from Postgres (Job rows in pending/running), re-enqueues idempotently.
- **DB write failure on terminal state**: retry with bounded backoff (log on failure); Redis still reflects status; reconcile loop corrects DB later.



#### **Scaling Guidance**

- **Throughput**: scale **JobEngine** replicas; each uses cg:{channel}:workers for work-sharing.
- **Broadcast fan-out**: each runner maintains its own consumer group on stream:{channel}:broadcast.
- **Scheduler**: typically **1** replica; add a warm standby if desired (same command, lock ensures only one active).
- **Channels**: shard heavy workloads into multiple channels if needed (independent Streams/ZSETs).



#### **Configuration (relevant to these components)**



- JOBS_SCHEDULER_LOCK_TTL_MS (default: 5000)
- JOBS_RUNNER_HEARTBEAT_SEC (default: 5)
- JOBS_STREAM_MAXLEN (trimming; default: 100_000)
- JOBS_DEFAULT_EXPIRES_SEC (default: 900; set to 300 if you want 5m)
- JOBS_XPENDING_IDLE_MS (reclaim threshold; e.g., 60000)

---

## 8) Semantics
- Expiration: default 15m (configurable; set to 5m if you want). Enforced at scheduler pop and runner claim; expired -> `failed` with event `expired`.
- Retries: exponential backoff with jitter; terminal when `attempt > max_retries`.
- Cancel: cooperative flag (`job:{id}.cancel=1` + DB `cancel_requested=True`); optional hard-kill only for subprocess jobs beyond `max_exec_seconds`.
- Broadcast: publish with `broadcast=True` -> per-runner consumer groups ensure each runner processes once.
- DB connections: always call `close_old_connections()` at job start/end and in scheduler loop.

---

## 9) JobManager (OO control/inspection)

```python
class JobManager:
    def get_runners(self, channel) -> list[dict]: ...
    def get_queue_state(self, channel) -> dict: ...  # xinfo, xpending, zcard
    def ping(self, runner_id, timeout=2.0) -> bool: ...
    def shutdown(self, runner_id, graceful=True) -> None: ...
    def broadcast(self, channel, func, payload, **opts) -> str: ...
    def job_status(self, job_id) -> dict: ...
```
- `get_queue_state` pulls stream length (`XINFO`), `XPENDING` summary, `ZCARD sched`.
- Heartbeats via `runner:{rid}:hb` TTL.

---

## 10) Metrics (via `metrics.record`)
- Counters: `jobs.published`, `jobs.completed`, `jobs.failed`, `jobs.retried`, `jobs.expired`
- Timings: `jobs.duration_ms` (per func/channel)
- Gauges: `jobs.queue.stream_len`, `jobs.queue.pending`, `jobs.queue.sched`, `jobs.runner.count`, `jobs.scheduler.leader`

---

## 11) Configuration

```python
JOBS_REDIS_URL = "redis://..."
JOBS_REDIS_PREFIX = "mojo:jobs"

JOBS_DEFAULT_EXPIRES_SEC = 900     # set 300 if you want 5m default
JOBS_DEFAULT_MAX_RETRIES = 3
JOBS_DEFAULT_BACKOFF_BASE = 2.0
JOBS_DEFAULT_BACKOFF_MAX = 3600
JOBS_STREAM_MAXLEN = 100_000
JOBS_SCHEDULER_LOCK_TTL_MS = 5000
JOBS_RUNNER_HEARTBEAT_SEC = 5
JOBS_LOCAL_QUEUE_MAXSIZE = 1000
JOBS_PAYLOAD_MAX_BYTES = 16384
```

---

## 12) Built-in Webhook Job
- Name: `webhook.post`
- Payload: `{ "url": "...", "method": "POST", "headers": {...}, "body": {...}, "idempotency_key": "..." }`
- Behavior: 2xx success; 408/429/5xx retry with backoff; redact `Authorization` in logs.

---

## 13) Local Jobs
- In-process queue + single worker thread.
- No retries/hard-kill; for ultra-short work.
- `publish_local()` + `@local_async_job` decorator.

---

## 14) Security & Safety
- Registry key only; no import strings.
- Payload size caps; large data by reference (S3, etc.).
- Idempotency: optional `idempotency_key` (unique in DB).
- Redact secrets in logs/metadata.

---

## 15) Testing
- Unit: enqueue, expiration set, backoff calc, cancel flag.
- Integration: multi-runner contention; scheduler leader failover; expiration at pop/claim; retries; broadcast delivery.
- Chaos: Redis restart (rebuild from DB); runner crash (XPENDING/XCLAIM recovery); DB outage on terminal write.

---

## 16) Implementation Plan

M1 — Core plumbing
- Keys helper; Redis adapter (sync).
- Models (Job, JobEvent).
- Registry + JobContext.
- Publish API (DB + Redis mirror).
- JobEngine runner (Streams claim, execute, retry, cancel).
- Scheduler (lock + ZSET -> Stream).
- `close_old_connections` wrappers.
- JobManager: `status`, `get_queue_state`.

M2 — Controls & broadcast
- Broadcast stream + per-runner groups.
- JobManager: `get_runners`, `ping`, `shutdown`.
- Runner control channel + heartbeat.
- Metrics emissions.

M3 — Extras
- Built-in `webhook.post`.
- Local job queue + decorator.
- Retention job for `JobEvent`.

M4 — Hardening
- Idempotency key support.
- DB write retry on terminal transitions.
- Docs + examples + sample jobs.

---

## 17) Directory Layout

```
mojo/apps/jobs/
  __init__.py            # publish(), decorators, facade
  adapters.py            # Redis client wrapper
  keys.py                # key builders (prefix-aware)
  models.py              # Job, JobEvent
  registry.py            # job registry + @async_job
  context.py             # JobContext
  job_engine.py          # runner daemon (JobEngine)  <-- __main__ entrypoint
  scheduler.py           # scheduler daemon
  manager.py             # JobManager OO API
  builtin_webhook.py     # webhook.post handler
  local_queue.py         # local jobs
```

---

## 18) Acceptance Criteria
- Publish -> executes once on one of N runners.
- Broadcast -> every runner executes once.
- Cancel -> handler exits, state recorded.
- Expiration works (default 15m, configurable).
- Retries/backoff work; terminal failure after max retries.
- Hard-kill works for over-time subprocess jobs.
- Metrics emitted via `metrics.record`.
- JobManager can ping/shutdown, queue state visible.
- JobEngine runs as a daemon with a main() entry point and clean signal handling.
