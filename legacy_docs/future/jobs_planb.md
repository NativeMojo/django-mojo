# django-mojo/docs/future/jobs_planb.md
# Jobs Plan B — KISS Queue (Redis List + Processing ZSET + Scheduled ZSET)

This document proposes a simpler, more robust job queue design that replaces Redis Streams with a classic queue pattern:

- Immediate queue per channel: Redis List (RPUSH/BRPOP)
- In-flight tracking per channel: Redis ZSET (visibility timeout)
- Scheduled (delayed) per channel: Redis ZSET (timestamp scores)

The goals are:
- Accurate counts with trivial queries
- Crash safety with straightforward requeue logic
- Minimal moving parts (KISS)
- Predictable, debuggable behavior in dev and prod


## TL;DR: Why Plan B?

- It makes “what’s queued vs in-flight vs scheduled” exact and cheap:
  - queued_count = LLEN jobs:queue:{channel}
  - inflight_count = ZCARD jobs:processing:{channel}
  - scheduled_count = ZCARD jobs:sched:{channel}

- Workers don’t “poll” Redis; they block on BRPOP. The reaper and scheduler do small periodic work.

- Crash safety follows the well-known “visibility timeout” pattern: if a worker dies, the reaper requeues stale processing entries.

- You don’t fight stream semantics (last-delivered-id, historical retained entries) just to get correct counts.


## Data Model (Redis Keys)

Per-channel keys (mojo:jobs prefix omitted for brevity):

- List queue (immediate):
  - jobs:queue:{channel}
  - Publish: RPUSH jobs:queue:{channel} job_id
  - Engine: BRPOP jobs:queue:{channel} (with timeout)

- In-flight tracking (visibility timeout):
  - jobs:processing:{channel} — ZSET with score = last_seen_ms (or claimed_at_ms)
  - On claim: ZADD jobs:processing:{channel} now_ms job_id
  - On completion: ZREM jobs:processing:{channel} job_id

- Scheduled (delayed):
  - jobs:sched:{channel} — ZSET score = run_at_ms
  - (Optional) jobs:sched_broadcast:{channel} if you still need broadcast delivery semantics
  - Scheduler: ZPOPMIN due jobs and RPUSH to jobs:queue:{channel}

Optional/global:
- jobs:lock:reaper:{channel} — short lock (SET NX PX) used by the reaper when scanning a channel
- jobs:lock:scheduler — leadership lock (as we already have)


## Engine Flow

1) Claim
- BRPOP on jobs:queue:{channel} with a small timeout (e.g., 1–5s)
- On success, obtain job_id, add to jobs:processing:{channel}:
  - ZADD jobs:processing:{channel} now_ms job_id
- Load Job row from DB; set status='running', started_at, runner_id, attempt++

2) Execute handler(job)

3) Complete / Fail / Retry
- Success:
  - Update DB: status='completed', finished_at
  - ZREM jobs:processing:{channel} job_id
- Failure with retry:
  - Calculate run_at = now + backoff
  - DB: status='pending', run_at
  - ZREM jobs:processing:{channel} job_id
  - ZADD jobs:sched:{channel} run_at_ms job_id
- Failure (terminal):
  - DB: status='failed', finished_at, last_error, stack_trace
  - ZREM jobs:processing:{channel} job_id

Notes:
- No ACK semantics needed; queue item is removed on BRPOP.
- The ZSET “processing” is the single source of truth for in-flight jobs in Redis.


## Scheduler Flow

- Leader lock (already implemented)
- Every loop:
  - For each channel:
    - jobs:sched:{channel}: ZPOPMIN up to N
    - For each (job_id, score):
      - If score > now_ms: reinsert and break (ZADD)
      - Else: RPUSH jobs:queue:{channel} job_id
      - (Optional) record JobEvent('queued'), with scheduled_at from score

Notes:
- Keep the 2-ZSET design only if you truly need “broadcast” later; otherwise one ZSET per channel is enough.
- You can add jitter/sleep between ZPOPMIN calls as we do today.


## Reaper (Visibility Timeout)

- Periodically (e.g., every 5–10s), per channel:
  - Acquire jobs:lock:reaper:{channel} (SET NX PX 3000) — avoid multi-reaper races
  - ZRANGEBYSCORE jobs:processing:{channel} -inf now_ms - timeout_ms
    - For stale job_id in result:
      - Requeue: RPUSH jobs:queue:{channel} job_id
      - ZREM jobs:processing:{channel} job_id
      - (Optional) increment an “reclaimed” metric / write JobEvent('retry', reason='reaper_timeout')
- Timeout can be a setting, e.g., JOBS_VISIBILITY_TIMEOUT_MS = 30000

Notes:
- Reaper can run inside the engine process (a background thread) or in the scheduler process (single leader).
- It’s the single mechanism that replaces Streams’ PEL reclaim complexity.


## Counts and Stats (Accurate and Cheap)

For each channel:
- queued_count = LLEN jobs:queue:{channel}  (O(1))
- inflight_count = ZCARD jobs:processing:{channel}  (O(1))
- scheduled_count = ZCARD jobs:sched:{channel} (+ broadcast ZSET if kept)

Total counts:
- totals.queued = sum queued_count per channel
- totals.inflight = sum inflight_count
- totals.scheduled = sum scheduled_count
- totals.running (DB) = Job.objects.filter(status='running').count()
  - running_active = Job count with runner_id in alive runners
  - running_stale = running - running_active

No stream-length heuristics needed.


## Crash Safety and Exactly-Once Behavior

- The “at least once” delivery model applies: a job may be delivered again if:
  - A worker dies after claim but before completion
  - The reaper requeues a stale in-flight entry
- Idempotency is still the job handler’s responsibility (same as today).
- DB remains source of truth (Job row), and JobEvent logs provide audit trails.


## Efficiency & Polling Behavior

- Engine workers do NOT poll:
  - They block on BRPOP with a short timeout, wake to handle signals/stop, then block again. This is efficient in Redis.
- Scheduler does small ZPOPMIN batches with jittered sleeps (same as today).
- Reaper does small ZSET scans periodically per channel (bounded by the number of stale entries).

Redis op complexity:
- RPUSH/BRPOP: O(1)
- LLEN/ZCARD: O(1)
- ZADD/ZREM: O(log N)
- ZPOPMIN: O(log N) per popped item
- ZRANGEBYSCORE for reaper: O(log N + M), where M is number of stale entries found

Compared to Streams:
- You avoid XRANGE/XINFO equivalence math entirely for counts.
- Claim/execute flows are simpler and have fewer edge-cases.


## Feature Summary

- Pros:
  - Simple model, exact counts, easy to reason about and debug
  - Crash safety with a small, explicit reaper
  - No dependence on consumer-group semantics for correctness
  - Myriad tools (LLEN/ZCARD) give you instant visibility into queue health

- Cons:
  - You lose Streams’ PEL/reclaim mechanisms; reaper replaces them
  - “Broadcast” is not natural with a list queue; implement separately or defer


## API/Code Changes (High-Level)

- Publish:
  - Immediate: RPUSH jobs:queue:{channel} job_id
  - Delayed: ZADD jobs:sched:{channel} run_at_ms
  - DB insert and JobEvent('created'/'scheduled') unchanged
  - Validate channel against JOBS_CHANNELS as we do now

- Engine:
  - Replace XREADGROUP claim with BRPOP
  - On claim: ZADD jobs:processing:{channel} now_ms job_id
  - On completion: ZREM from processing
  - On failure/retry: ZREM processing, ZADD sched, DB updates
  - Add a small thread that acts as reaper (or let scheduler do it per channel)

- Scheduler:
  - For each channel, ZPOPMIN due entries and RPUSH to queue
  - Optional reaper can live here (single leader), or in engine

- Manager.get_queue_state:
  - queued_count = LLEN queue
  - inflight_count = ZCARD processing
  - scheduled_count = ZCARD sched
  - db_running from DB, same as now
  - Clean and accurate without scanning Streams

- Tools:
  - clear_channel(channel) now deletes: queue list, processing ZSET, sched ZSET
  - recover_stale_running can be simplified (optional), or rely on reaper

- Optional:
  - Provide manager.trim_queue(channel, maxlen) to keep lists bounded
  - Expose reaper metrics (reclaimed count)


## Migration Plan

- Phase 0: Feature flag
  - Add JOBS_QUEUE_MODE = 'streams' | 'lists'
  - Implement Plan B under 'lists' while keeping old code under 'streams' for rollback

- Phase 1: Implement Plan B primitives
  - Adapter helpers for RPUSH/BRPOP, ZADD/ZREM processing, ZADD sched
  - Wire publish() to call the right backend by JOBS_QUEUE_MODE
  - Engine claim/execute/complete using BRPOP + processing ZSET path
  - Scheduler move-due using sched ZSET path
  - Manager stats using LLEN/ZCARD path

- Phase 2: Side-by-side testing in dev
  - Flip JOBS_QUEUE_MODE='lists' in dev
  - Run integration tests and manual flows
  - Confirm counts, reaper behavior, scheduler timing, crash recovery

- Phase 3: Roll out to staging, then prod
  - Flip setting, monitor
  - Keep Streams code for one release in case rollback needed, then remove

- Phase 4: Cleanup
  - Remove Streams-specific stats and helpers when we’re confident
  - Update docs & dashboards to reflect queued/inflight/scheduled counts from lists/zsets


## Open Questions & Decisions

- Broadcast:
  - If you still need “every runner executes this job once,” we must implement broadcast separately (e.g., Pub/Sub event with per-runner markers) or drop the feature. The critical path queue should remain list-based.

- Idempotency:
  - Same as today — use idempotency keys in job-level logic when needed.

- Per-channel priorities:
  - We already prioritize 'priority' by the order we BRPOP channels (check channels_ordered with 'priority' first).

- macOS dev:
  - Avoid fork-based daemonization (spawn/exec the foreground worker; we can keep a PID file/logfile wrapper). On Linux, use systemd/supervisor.

- Metrics:
  - LLEN/ZCARD are cheap; keep real-time stats simple. Add a few counters (reclaimed, published, scheduled) as needed.


## Example Pseudocode

Engine main loop:
```python
channels = order_channels_with_priority_first(...)
while running:
    # BRPOP across channels with a timeout (simulate multi-queue pop)
    job_id, ch = brpop_one(channels, timeout=2)
    if not job_id:
        continue
    zadd(f"jobs:processing:{ch}", now_ms(), job_id)

    job = Job.objects.get(id=job_id)
    mark_running(job, runner_id, attempt+1)

    try:
        handler = load_handler(job.func)
        handler(job)
        mark_completed(job)
        zrem(f"jobs:processing:{ch}", job_id)
    except Retry as r:
        run_at = calc_backoff(...)
        mark_pending_with_run_at(job, run_at)
        zrem(f"jobs:processing:{ch}", job_id)
        zadd(f"jobs:sched:{ch}", epoch_ms(run_at), job_id)
    except Exception as e:
        mark_failed(job, e)
        zrem(f"jobs:processing:{ch}", job_id)
```

Reaper (in a background thread or scheduler loop):
```python
if acquire_lock(f"jobs:lock:reaper:{ch}", px=3000):
    cutoff = now_ms() - VISIBILITY_TIMEOUT_MS
    stale = zrangebyscore(f"jobs:processing:{ch}", -inf, cutoff)
    for job_id in stale:
        # put it back for reprocessing
        zrem(f"jobs:processing:{ch}", job_id)
        rpush(f"jobs:queue:{ch}", job_id)
        event(job_id, 'retry', reason='reaper_timeout')
```

Scheduler move-due:
```python
while True:
    batch = zpopmin(f"jobs:sched:{ch}", count=10)
    if not batch: break
    not_due = {}
    for job_id, score in batch:
        if score > now_ms():
            not_due[job_id] = score
        else:
            rpush(f"jobs:queue:{ch}", job_id)
            event(job_id, 'queued', scheduled_at=score)
    if not_due:
        zadd(f"jobs:sched:{ch}", not_due)
        break
    sleep_jitter(...)
```


## Final Recommendation

- Move to the List + Processing ZSET + Scheduled ZSET design (Plan B). It’s simpler, more robust, and gives you exact, cheap counts with LLEN/ZCARD — no streams heuristics, no last-delivered-id wrangling.
- Short-term (if you need relief while building Plan B), you can add side-counters to Streams and a periodic reconciliation task — but that preserves Streams complexity. Plan B eliminates it.

Once we agree, I’ll produce the concrete patches (feature-flagged) for publish, engine, scheduler, manager stats, plus a compact reaper, and we can test it in dev this week.