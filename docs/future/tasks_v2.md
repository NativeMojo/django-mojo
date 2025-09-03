# Django-MOJO Tasks (v2) — Requirements & Design





## **0) Scope (what we’re building)**





A **small, reliable background job system for Django**:



- **Fast path** in Redis (Streams + ZSETs).
- **Truth & audit** in Postgres (lean models).
- **Multiple workers per channel**; plus **broadcast** jobs (every worker runs it).
- **Delayed jobs** (run_at / delay).
- **Retries with backoff**.
- **Cooperative cancel**; **hard-kill** via subprocess + max execution time.
- **Expiration**: tasks auto-fail if not executed by expires_at.
- **Local tasks**: simple in-process queue for tiny jobs.
- **Simple API + decorators**.





No cron/recurring (you have that elsewhere). No priorities, no rate limits (by design).



------





## **1) Non-Goals**





- Exactly-once delivery (we use at-least-once; handlers must be idempotent).
- Workflows/dependencies.
- WebSockets/ASGI coupling.
- Fancy metrics stack (we emit to your metrics.record only).





------





## **2) Glossary**





- **Channel**: logical queue name.
- **Broadcast**: a job each runner on a channel must process once.
- **Runner**: worker process consuming jobs.
- **Scheduler**: process moving due jobs from ZSET into Streams.





------





## **3) High-Level Architecture**





**Redis (runtime, ephemeral)**



- **Streams** per channel:

  

  - stream:{channel} (normal work) with consumer group cg:{channel}:workers.
  - stream:{channel}:broadcast with **per-runner** group cg:{channel}:runner:{runner_id}.

  

- **ZSET** delays: sched:{channel}; score = run_at (ms).

- **HASH** per task: task:{id} → status/attempt/cancel/runner_id/expires_at/max_exec.

- **LOCK** for scheduler leadership: lock:scheduler (SET NX PX).





**Postgres (truth + audit)**



- Task (header/current state; small).
- TaskEvent (append-only, small details; retention).





------





## **4) Data Model (Postgres)**



```
# tasks/models.py
class Task(models.Model):
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

class TaskEvent(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    channel = models.CharField(max_length=100, db_index=True)
    event = models.CharField(max_length=24, db_index=True)  # created/queued/running/retry/canceled/completed/failed/expired
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    runner_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    attempt = models.IntegerField(default=0)
    details = models.JSONField(default=dict, blank=True)  # keep tiny
```

**Notes**



- We keep **latest** error/trace on Task. Events table is **tiny** and has retention (e.g., 30–90 days).





------





## **5) Redis Keys (centralized)**





Single builder module keys.py (prefix configurable):

```
P = settings.TASKS_REDIS_PREFIX  # e.g., "mojo:tasks"

stream(channel)            -> f"{P}:stream:{channel}"
stream_broadcast(channel)  -> f"{P}:stream:{channel}:broadcast"
group_workers(channel)     -> f"{P}:cg:{channel}:workers"
group_runner(c, rid)       -> f"{P}:cg:{c}:runner:{rid}"
sched(channel)             -> f"{P}:sched:{channel}"
task(task_id)              -> f"{P}:task:{task_id}"
runner_ctl(rid)            -> f"{P}:runner:{rid}:ctl"
runner_hb(rid)             -> f"{P}:runner:{rid}:hb"
scheduler_lock()           -> f"{P}:lock:scheduler"
```



------





## **6) Public API (usable from app code)**



```
# mojo/apps/tasks/__init__.py
def publish(func, payload, *, channel="default", delay=None, run_at=None,
            broadcast=False, max_retries=3, backoff_base=2.0, backoff_max=3600,
            expires_in=None, max_exec_seconds=None, idempotency_key=None) -> str: ...

def publish_local(func, *args, **kwargs): ...

def cancel(task_id: str) -> bool: ...
def status(task_id: str) -> dict: ...     # fast via Redis, DB fallback

def async_task(channel="default", broadcast=False, **defaults):
    """Decorator to register a function as a task by name."""
    ...

def local_async_task(**defaults):
    """Decorator for in-process queue."""
    ...
```



### **TaskContext passed to handlers**



```
class TaskContext:
    task_id: str
    channel: str
    payload: dict

    def should_cancel(self) -> bool: ...
    def set_metadata(self, **kv): ...
    def get_model(self) -> Task: ...       # lazy ORM fetch
```

> We deliberately **do not** pass the ORM row directly. If the job needs it, call ctx.get_model().



------





## **7) Core Processes**







### **7.1 Scheduler (single active)**





- Acquire SET lock:scheduler <uuid> NX PX=5000; renew every 2s. Exit on loss.

- Loop per channel:

  

  - ZPOPMIN sched:{channel} while score ≤ now:

    

    - Load task:{id} (or DB) → if expires_at < now: mark failed (DB+event), **skip**.
    - Else XADD stream (or stream_broadcast if broadcast).

    

  

- Sleep with jitter (e.g., 200–500ms).

- Call django.db.close_old_connections() each loop.







### **7.2 Runner (multiple per channel)**





- Startup:

  

  - Ensure consumer groups exist:

    

    - cg:{channel}:workers on stream:{channel}
    - cg:{channel}:runner:{rid} on stream:{channel}:broadcast

    

  - Register in set:runners:{channel} (optional) and start heartbeat runner:{rid}:hb (TTL).

  

- Main loop:

  

  - XREADGROUP BLOCK from both streams.

  - For each msg:

    

    - Extract task_id.

    - Load task:{id}; if **expired**, XACK and mark failed.

    - Mark **running**: update Redis hash; optionally DB fields (status/runner_id/started_at).

    - **Execute** task function via registry:

      

      - Provide TaskContext.
      - Call close_old_connections() **before** execution and **after**.
      - For hard limits: run job in **subprocess** wrapper if max_exec_seconds set; soft-cancel first; send SIGTERM→SIGKILL if overdue.
      - Periodically check ctx.should_cancel().

      

    - On success:

      

      - XACK, set Redis status=completed; update DB terminal state + TaskEvent(completed).

      

    - On failure:

      

      - Compute delay = min(int(backoff_base ** (attempt+1) + jitter), backoff_max).
      - XACK, increment attempt in Redis; schedule: ZADD sched:{channel} with now + delay.
      - If attempt > max_retries: mark failed (DB+Redis), write TaskEvent(failed).

      

    

  

- Respond to Manager **control** commands via runner:{rid}:ctl:

  

  - ping → reply pong.
  - shutdown → stop after current task (graceful).

  



---

# **JobEngine & Scheduler (Hybrid Model)**







## **Components**





- **JobEngine (Runner)**

  

  - Long-running daemon that claims jobs from Redis Streams and executes handlers.
  - Supports cooperative cancel, retries/backoff, hard-kill via subprocess + max_exec_seconds.
  - Emits metrics and heartbeats; responds to control commands (ping/shutdown).

  

- **Scheduler**

  

  - Moves due jobs from Redis ZSET (sched:{channel}) into Streams.
  - Enforces expires_at at pop time (skip + mark failed if expired).
  - Single active leader via Redis lock (SET lock:scheduler NX PX=… + renew).

  







## **Deployment Modes**





1. **Separate daemons (recommended for prod)**

   

   - Run jobs-engine (N replicas) and jobs-scheduler (1 replica).
   - Pros: clear separation, independent scaling/restarts.

   

2. **Combined mode (dev/small installs)**

   

   - jobs-engine --with-scheduler starts a background scheduler thread in the same process.
   - Guarded by the same Redis leadership lock; only one scheduler is active cluster-wide.

   







## **CLI / Entry Points**





- python manage.py jobs-engine [--channels default,emails] [--runner-id auto] [--with-scheduler]

- python manage.py jobs-scheduler [--channels default,emails]

- Flags:

  

  - --channels: comma-separated list to serve/schedule (default: all configured).
  - --with-scheduler: (engine only) spawn scheduler thread.
  - --runner-id: explicit or auto-generated <host>-<pid>-<rand>.

  







## **Lifecycle & Control**





- **JobEngine**

  

  - Startup: ensure consumer groups; start heartbeat key (runner:{id}:hb, TTL); subscribe to control channel (runner:{id}:ctl).
  - Loop: XREADGROUP BLOCK from normal & broadcast streams; execute jobs; close_old_connections() pre/post execution.
  - Signals: SIGTERM/SIGINT → stop accepting new messages, finish current, flush, exit.
  - Control cmds: ping → pong; shutdown → graceful stop.

  

- **Scheduler**

  

  - Acquire/renew lock every ~2s; on loss → exit.
  - Loop: ZPOPMIN sched:{channel} while score ≤ now; skip expired; XADD due jobs; sleep with jitter; close_old_connections() each loop.

  







## **Locking & Leadership**





- Key: lock:scheduler (prefix-aware).
- Acquire: SET key <uuid> NX PX=<ttl_ms>.
- Renew: PEXPIRE key <ttl_ms> (only if value matches <uuid> to avoid stealing).
- Fallback: if renew fails, scheduler exits cleanly; another instance can assume leadership.







## **Health, Metrics, and Introspection**





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

  







## **Failure Modes & Recovery**





- **Runner crash**: pending deliveries visible in XPENDING; other runners can XCLAIM after idle timeout.
- **Scheduler crash**: lock expires; another instance takes over; due jobs are still in ZSET.
- **Redis restart/data loss**: on startup, JobEngine reconciles from Postgres (Job rows in pending/running), re-enqueues idempotently.
- **DB write failure on terminal state**: retry with bounded backoff (log on failure); Redis still reflects status; reconcile loop corrects DB later.







## **Scaling Guidance**





- **Throughput**: scale **JobEngine** replicas; each uses cg:{channel}:workers for work-sharing.
- **Broadcast fan-out**: each runner maintains its own consumer group on stream:{channel}:broadcast.
- **Scheduler**: typically **1** replica; add a warm standby if desired (same command, lock ensures only one active).
- **Channels**: shard heavy workloads into multiple channels if needed (independent Streams/ZSETs).







## **Configuration (relevant to these components)**





- JOBS_SCHEDULER_LOCK_TTL_MS (default: 5000)
- JOBS_RUNNER_HEARTBEAT_SEC (default: 5)
- JOBS_STREAM_MAXLEN (trimming; default: 100_000)
- JOBS_DEFAULT_EXPIRES_SEC (default: 900; set to 300 if you want 5m)
- JOBS_XPENDING_IDLE_MS (reclaim threshold; e.g., 60000)

------





## **8) Core Semantics**







### **Expiration**





- expires_at: auto-set on publish if not passed. **Default**: 15 minutes (configurable).

  You can set to 5 minutes if you insist—make it a setting: TASKS_DEFAULT_EXPIRES_SEC.

- Enforced at **scheduler pop** and **runner claim**. Expired tasks → status=failed, event=expired.







### **Retries / Backoff**





- Exponential backoff with jitter:

  

  - delay = min(int(backoff_base ** attempt + random()), backoff_max_sec)

  

- Attempt increments on failure; terminal when attempt > max_retries.







### **Cancel**





- cancel(task_id) sets Task.cancel_requested = True and task:{id}.cancel=1, and publishes runner control ping if running.
- **Cooperative** cancel in handlers via ctx.should_cancel().
- **Hard-kill** if subprocess and max_exec_seconds exceeded.







### **Broadcast**





- Publish with broadcast=True → message goes to stream:{channel}:broadcast.
- Each runner has its **own** consumer group; each message is delivered **once per runner**.







### **DB connections**





- Call close_old_connections():

  

  - At **start** of each job execution.
  - At **end** of each job execution.
  - Periodically in scheduler loop.

  





------





## **9) Manager (OO Interface)**



```
class TaskManager:
    def get_runners(self, channel) -> list[dict]: ...
    def get_queue_state(self, channel) -> dict: ...  # xinfo, xpending counts, zcard sched
    def ping(self, runner_id, timeout=2.0) -> bool: ...
    def shutdown(self, runner_id, graceful=True) -> None: ...
    def broadcast(self, channel, func, payload, **opts) -> str: ...
    def task_status(self, task_id) -> dict: ...
```



- get_queue_state pulls:

  

  - Stream length (XINFO STREAM.len)
  - Pending (via XPENDING summary)
  - Scheduled count (ZCARD)

  

- Runners keep a heartbeat key runner:{rid}:hb with TTL (e.g., 10s).





------





## **10) Metrics (via** 

## **metrics.record**

## **)**





Emit:



- Counters: tasks.published, tasks.completed, tasks.failed, tasks.retried, tasks.expired
- Timings: tasks.duration_ms (per func, channel)
- Gauges: tasks.queue.stream_len, tasks.queue.pending, tasks.queue.sched, runner.count, scheduler.leader
- Tag every metric with channel, func (where applicable).





------





## **11) Configuration**



```
TASKS_REDIS_URL = "redis://..."
TASKS_REDIS_PREFIX = "mojo:tasks"

TASKS_DEFAULT_EXPIRES_SEC = 900     # set to 300 if you want 5m default
TASKS_DEFAULT_MAX_RETRIES = 3
TASKS_DEFAULT_BACKOFF_BASE = 2.0
TASKS_DEFAULT_BACKOFF_MAX = 3600
TASKS_STREAM_MAXLEN = 100_000       # Streams trimming
TASKS_SCHEDULER_LOCK_TTL_MS = 5000
TASKS_RUNNER_HEARTBEAT_SEC = 5
TASKS_LOCAL_QUEUE_MAXSIZE = 1000
TASKS_PAYLOAD_MAX_BYTES = 16384
```



------





## **12) Built-in Webhook Task**





**Name:** webhook.post

**Payload:**

```
{
  "url": "https://...",
  "method": "POST",
  "headers": {"Idempotency-Key":"..."},
  "body": {"..."}
}
```

**Behavior:**



- 2xx → success.
- 408/429/5xx → retry with backoff.
- Timeouts and connection errors → retry.
- Redact Authorization in logs/metadata.





------





## **13) Local Tasks**





- In-process queue + single worker thread.
- Best-effort only: **no retries, no kill**.
- publish_local(func, *args, **kwargs) or @local_async_task().





------





## **14) Security & Safety**





- **Registry key** only; no dynamic import strings.
- Enforce **payload size** cap; store big blobs externally and pass a reference.
- **Idempotency**: optional idempotency_key enforced unique.
- Don’t log secrets; redact headers like Authorization.





------





## **15) Testing Strategy**





- Unit:

  

  - Enqueue → ZSET/Stream write, expiration set, idempotency.
  - Backoff calculation.
  - Cancel flag behavior in handler stub.

  

- Integration:

  

  - Multi-runner contention; at-least-once semantics.
  - Scheduler leader election failover.
  - Expiration at scheduler and runner.
  - Hard-kill path with subprocess over-time.
  - Broadcast delivery: each runner processes once.

  

- Chaos:

  

  - Redis restart (runners rebuild from DB).
  - Runner crash during running (XPENDING/XCLAIM recovery).
  - DB outage during terminal state (retry DB write loop).

  





------





## **16) Implementation Plan**





**Milestone 1 — Core plumbing**



- Keys helper; Redis adapter (sync).
- Models (Task, TaskEvent).
- Registry + TaskContext.
- Publish API (DB + Redis mirror).
- Runner (Streams claim, execute, retry, cancel).
- Scheduler (lock + ZSET → Stream).
- close_old_connections wrappers.
- Minimal Manager: status, get_queue_state.





**Milestone 2 — Controls & broadcast**



- Broadcast stream + per-runner groups.
- Manager: get_runners, ping, shutdown.
- Runner control channel + heartbeat.
- Metrics emissions.





**Milestone 3 — Extras**



- Built-in webhook.post.
- Local task queue + decorator.
- Admin/CLI: retry/expire sweepers (optional).
- Retention job for TaskEvent.





**Milestone 4 — Hardening**



- Idempotency key support.
- DB write retry on terminal transitions.
- Docs + examples + sample tasks.





------





## **17) Directory Layout**



```
mojo/apps/tasks/
  __init__.py            # publish(), decorators, facade
  adapters.py            # Redis client wrapper
  keys.py                # key builders (prefix-aware)
  models.py              # Task, TaskEvent
  registry.py            # task registry + @async_task
  context.py             # TaskContext
  runner.py              # management command run_worker
  scheduler.py           # management command run_scheduler
  manager.py             # TaskManager OO API
  builtin_webhook.py     # webhook.post handler
  local_queue.py         # local tasks
```



------





## **18) Acceptance Criteria (cut-and-dry)**





- Can **publish** a job with delay/run_at and see it execute once by one of N runners.
- Can **publish broadcast** and see **every runner** execute exactly once.
- Can **cancel** a running job; handler detects cancel and exits; terminal state recorded.
- **Expiration** skips stale jobs (failed/expired).
- **Retries with backoff** happen until max_retries, then job fails.
- **Hard-kill** terminates over-time subprocess jobs.
- /metrics (your emitter) shows counters/gauges increasing appropriately.
- Manager can **ping** and **shutdown** a runner, and get **queue state**.





------





## **19) Blunt trade-offs & risks**





- At-least-once means handler **must** be idempotent. If you ignore that, you’ll get duplicates.
- Hard-kill requires subprocess execution; don’t try to kill threads.
- Streams are reliable but **not a database**. On Redis loss, rebuild from DB (we provide startup reconciliation).





------



That’s the full plan. If you want, I can stub the runner.py and scheduler.py loops and the TaskContext so your team can start coding immediately.