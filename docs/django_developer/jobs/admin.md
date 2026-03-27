# Jobs Admin & Control — Django Developer Reference

REST endpoints for monitoring and managing the jobs system. All require `view_jobs` or `manage_jobs` permission.

Base path: `/api/jobs/`

## Health & Monitoring

### GET /api/jobs/health

Overview health for all channels.

**Response**: Dict of channel → health status with `status` field (`healthy`, `warning`, `critical`).

### GET /api/jobs/health/`<channel>`

Detailed health for a single channel.

**Response**: Queue stats, in-flight count, runner count, alert flags.

### GET /api/jobs/stats

System-wide statistics snapshot.

**Response**: Channel metrics, runner details, total counts.

### GET /api/jobs/status/`<job_id>`

Quick status check for a single job.

**Response**: Job status dict (same as `jobs.status()` in Python).

## Job Control

### POST /api/jobs/cancel

Cancel a job.

**Body**: `{"job_id": "..."}`

Sets `cancel_requested=True`. Running jobs must check via `check_cancel_requested()`.

### POST /api/jobs/retry

Retry a failed job.

**Body**: `{"job_id": "...", "delay": 60}` (delay optional, in seconds)

Resets the job to `pending` and re-publishes it.

## CRUD Endpoints

### Jobs

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs/job` | List jobs (supports `status`, `channel`, `func`, `runner_id`, `broadcast` filters) |
| GET | `/api/jobs/job/<id>` | Get job detail |

Query params for list: `status`, `channel`, `func`, `runner_id`, `broadcast`, `dr_start`, `dr_end`, `sort`, `size`, `start`, `graph`.

Available graphs: `default`, `detail`, `status`, `admin`.

### Job Events

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs/event` | List events (filter by `job_id`, `channel`, `event`) |
| GET | `/api/jobs/event/<id>` | Get event detail |

### Job Logs

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs/logs` | List logs (filter by `job_id`, `channel`, `kind`) |
| GET | `/api/jobs/logs/<id>` | Get log detail |

## Runner Management

### GET /api/jobs/runners

List active runners with heartbeat data.

**Query params**: `channel` (optional filter).

### POST /api/jobs/runners/ping

Ping a runner.

**Body**: `{"runner_id": "...", "timeout": 2.0}`

### POST /api/jobs/runners/shutdown

Gracefully shut down a runner.

**Body**: `{"runner_id": "...", "graceful": true}`

### POST /api/jobs/runners/broadcast

Broadcast a command to all runners.

**Body**: `{"command": "status"}` — commands: `status`, `shutdown`, `pause`, `resume`, `reload`.

### GET /api/jobs/runners/sysinfo

Collect system info from all runners.

**Query params**: `timeout` (default 5.0).

**Response**: List of dicts with `os`, `cpu_load`, `memory`, `disk`, `network` per runner. Requires `psutil` on runners.

### GET /api/jobs/runners/sysinfo/`<runner_id>`

System info from a specific runner.

## Control Endpoints

Administrative operations. All under `/api/jobs/control/`.

### GET /api/jobs/control/config

Current jobs configuration (channels, limits, timeouts).

### GET /api/jobs/control/channels

Discover channels from registered Redis streams.

### GET /api/jobs/control/queue-sizes

Queue sizes for all channels (queued, in-flight, scheduled counts).

### POST /api/jobs/control/clear-stuck

Re-queue jobs stuck in-flight longer than threshold.

**Body**: `{"channel": "default", "idle_threshold_ms": 60000}`

### POST /api/jobs/control/manual-reclaim

Manually reclaim all pending jobs for a channel.

**Body**: `{"channel": "default"}`

### POST /api/jobs/control/clear-queue

Clear entire queue for a channel. Destructive.

**Body**: `{"channel": "default", "confirm": "yes"}`

### POST /api/jobs/control/purge

Purge old job data from PostgreSQL.

**Body**: `{"days_old": 30, "status": "completed", "dry_run": true}`

- `days_old` (required): Delete jobs older than this
- `status` (optional): Only purge jobs with this status
- `dry_run` (optional): Preview without deleting

### POST /api/jobs/control/reset-failed

Reset failed jobs to pending for re-execution.

**Body**: `{"channel": "default", "since": "2026-03-01T00:00:00Z", "limit": 100}`

All params optional.

### POST /api/jobs/control/rebuild-scheduled

Rebuild Redis scheduled ZSETs from pending DB jobs with future `run_at`.

**Body**: `{"channel": "default", "limit": 1000}`

Useful after Redis data loss.

### POST /api/jobs/control/cleanup-consumers

Clean up Redis stream consumer groups.

**Body**: `{"channel": "default", "destroy_empty_groups": true}`

### POST /api/jobs/control/force-scheduler-lead

Force release the scheduler lock. Use only if the scheduler is stuck.

### POST /api/jobs/control/test

Publish a test job.

**Body**: `{"channel": "default", "delay": 0}`

## Python Manager API

For programmatic access, use the `JobManager`:

```python
from mojo.apps.jobs.manager import get_manager

mgr = get_manager()

# Queue state
state = mgr.get_queue_state("default")
# {"queued_count": 5, "inflight_count": 2, "scheduled_count": 10, "runners": [...]}

# Channel health
health = mgr.get_channel_health("default")

# System stats
stats = mgr.get_stats()

# Clear stuck jobs
result = mgr.clear_stuck_jobs("default", idle_threshold_ms=60000)

# Pause/resume a channel
mgr.pause_channel("maintenance")
mgr.resume_channel("maintenance")

# Cancel a job
mgr.cancel_job(job_id)

# Retry a failed job
mgr.retry_job(job_id, delay=60)
```
