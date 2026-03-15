# Jobs — REST API Reference

The jobs system provides background task processing with Redis-backed queuing and Postgres persistence. This reference covers every endpoint available under `/api/jobs/`.

---

## Permissions

| Permission | Grants |
|---|---|
| `view_jobs` | Read access to jobs, events, logs, health, runners, stats |
| `manage_jobs` | All read access plus cancel, retry, broadcast, shutdown |

Both `view_jobs` and `manage_jobs` grant read access. Write/action endpoints require `manage_jobs` specifically.

---

## Endpoint Index

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/jobs/job` | `view_jobs` | List jobs |
| GET | `/api/jobs/job/<id>` | `view_jobs` | Get job detail |
| POST | `/api/jobs/job/<id>` | `manage_jobs` | Perform action on a job |
| GET | `/api/jobs/event` | `view_jobs` | List job events |
| GET | `/api/jobs/event/<id>` | `view_jobs` | Get event detail |
| GET | `/api/jobs/logs` | `view_jobs` | List job logs |
| GET | `/api/jobs/logs/<id>` | `view_jobs` | Get log detail |
| GET | `/api/jobs/status/<job_id>` | `view_jobs` | Quick status check for a job |
| POST | `/api/jobs/cancel` | `manage_jobs` | Cancel a job |
| POST | `/api/jobs/retry` | `manage_jobs` | Retry a failed or canceled job |
| GET | `/api/jobs/health` | `view_jobs` | Health overview for all channels |
| GET | `/api/jobs/health/<channel>` | `view_jobs` | Health detail for one channel |
| GET | `/api/jobs/stats` | `view_jobs` | System-wide statistics |
| GET | `/api/jobs/runners` | `view_jobs` | List active runners |
| POST | `/api/jobs/runners/ping` | `manage_jobs` | Ping a runner |
| POST | `/api/jobs/runners/shutdown` | `manage_jobs` | Shut down a runner |
| POST | `/api/jobs/runners/broadcast` | `manage_jobs` | Broadcast a command to all runners |
| GET | `/api/jobs/runners/sysinfo` | `view_jobs` | Host system info from all runners |
| GET | `/api/jobs/runners/sysinfo/<runner_id>` | `view_jobs` | Host system info from one runner |

---

## Jobs

### List Jobs

**GET** `/api/jobs/job`

Returns a paginated list of jobs. Supports filtering, sorting, and graph selection.

**Query parameters:**

| Parameter | Example | Description |
|---|---|---|
| `status` | `?status=failed` | Filter by status (`pending`, `running`, `completed`, `failed`, `canceled`, `expired`) |
| `channel` | `?channel=email` | Filter by channel name |
| `func` | `?func=myapp.tasks.send_email` | Filter by job function path |
| `runner_id` | `?runner_id=runner-host1-abc` | Filter by runner |
| `broadcast` | `?broadcast=true` | Filter broadcast jobs |
| `dr_start` | `?dr_start=2024-01-01` | Created-at range start (ISO date or datetime) |
| `dr_end` | `?dr_end=2024-01-31` | Created-at range end |
| `sort` | `?sort=-created` | Sort field (prefix `-` for descending) |
| `size` | `?size=50` | Page size (default 20) |
| `start` | `?start=40` | Offset for pagination |
| `graph` | `?graph=status` | Response graph (`default`, `status`, `detail`, `admin`) |

**Example requests:**

```
GET /api/jobs/job?status=failed&sort=-created&size=20
GET /api/jobs/job?channel=email&status=pending
GET /api/jobs/job?dr_start=2024-01-01&dr_end=2024-01-31&status=completed
GET /api/jobs/job?graph=status&status=running
```

**Response:**

```json
{
  "status": true,
  "count": 142,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
      "channel": "email",
      "func": "myapp.tasks.send_welcome_email",
      "payload": { "user_id": 42 },
      "status": "failed",
      "run_at": null,
      "expires_at": null,
      "attempt": 1,
      "max_retries": 3,
      "broadcast": false,
      "cancel_requested": false,
      "max_exec_seconds": null,
      "runner_id": "runner-host1-abc123",
      "last_error": "User matching query does not exist.",
      "metadata": {},
      "created": "2024-01-15T10:00:00Z",
      "modified": "2024-01-15T10:00:03Z",
      "started_at": "2024-01-15T10:00:01Z",
      "finished_at": "2024-01-15T10:00:03Z",
      "duration_ms": 2000
    }
  ]
}
```

---

### Get Job Detail

**GET** `/api/jobs/job/<id>`

Returns full detail for a single job.

```
GET /api/jobs/job/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
GET /api/jobs/job/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4?graph=status
```

**Response (default graph):**

```json
{
  "status": true,
  "data": {
    "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    "channel": "email",
    "func": "myapp.tasks.send_welcome_email",
    "payload": { "user_id": 42 },
    "status": "failed",
    "run_at": null,
    "expires_at": null,
    "attempt": 1,
    "max_retries": 3,
    "broadcast": false,
    "cancel_requested": false,
    "max_exec_seconds": null,
    "runner_id": "runner-host1-abc123",
    "last_error": "User matching query does not exist.",
    "metadata": {},
    "created": "2024-01-15T10:00:00Z",
    "modified": "2024-01-15T10:00:03Z",
    "started_at": "2024-01-15T10:00:01Z",
    "finished_at": "2024-01-15T10:00:03Z",
    "duration_ms": 2000
  }
}
```

**Response (status graph):**

```json
{
  "status": true,
  "data": {
    "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    "status": "running",
    "runner_id": "runner-host1-abc123",
    "attempt": 1,
    "started_at": "2024-01-15T10:00:01Z",
    "finished_at": null,
    "last_error": ""
  }
}
```

---

### Job Field Reference

| Field | Type | Description |
|---|---|---|
| `id` | string | 32-character UUID (no dashes) |
| `channel` | string | Queue channel this job runs on |
| `func` | string | Dotted Python path to the job function |
| `payload` | object | Arguments passed to the job function |
| `status` | string | See [Job Status Values](#job-status-values) |
| `run_at` | datetime\|null | Scheduled run time; `null` means run immediately |
| `expires_at` | datetime\|null | Job will not run after this time |
| `attempt` | integer | How many times the job has been attempted |
| `max_retries` | integer | Maximum automatic retry attempts |
| `broadcast` | boolean | If `true`, all runners execute this job |
| `cancel_requested` | boolean | Cooperative cancel flag (checked by long-running jobs) |
| `max_exec_seconds` | integer\|null | Hard execution time limit in seconds |
| `runner_id` | string\|null | ID of the runner currently (or last) executing the job |
| `last_error` | string | Error message from the most recent failed attempt |
| `metadata` | object | Custom data set by the job function during execution |
| `created` | datetime | When the job was created |
| `modified` | datetime | When the job record was last updated |
| `started_at` | datetime\|null | When execution began |
| `finished_at` | datetime\|null | When execution completed (success or failure) |
| `duration_ms` | integer | Execution time in milliseconds (0 if not finished) |

---

### Job Status Values

| Status | Meaning |
|---|---|
| `pending` | Queued, waiting to be claimed by a runner |
| `running` | Currently being executed by a runner |
| `completed` | Finished successfully |
| `failed` | Execution threw an exception; may be retried |
| `canceled` | Canceled before or during execution |
| `expired` | Not claimed before `expires_at` was reached |

A job in `completed`, `failed`, `canceled`, or `expired` is terminal and will not run again unless explicitly retried.

---

### Available Graphs

| Graph | Fields returned |
|---|---|
| `default` | All standard fields including `payload`, `last_error`, `metadata`, `duration_ms` |
| `detail` | Same as `default` |
| `status` | `id`, `status`, `runner_id`, `attempt`, `started_at`, `finished_at`, `last_error` |
| `admin` | All fields (except `stack_trace`) — requires `manage_jobs` |

---

### Job Actions (POST)

Actions are performed by POSTing to `/api/jobs/job/<id>` with an action field in the request body. Requires `manage_jobs`.

#### Cancel a Job

Requests cancellation of a job. The behavior depends on the job's current state:

- **`pending` / `scheduled`** — canceled immediately.
- **`running` (runner alive)** — sets the cooperative cancel flag; the job function is expected to check `job.check_cancel_requested()` and exit gracefully.
- **`running` (runner dead)** — force-canceled immediately.
- **Terminal state** — returns an error; terminal jobs cannot be canceled.

**Request:**

```json
POST /api/jobs/job/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

{
  "cancel_request": true
}
```

**Response:**

```json
{
  "status": true,
  "message": "Job a1b2c3d4... canceled",
  "job_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "forced": false
}
```

When `forced` is `true`, the runner was unresponsive and the cancel was applied directly. When `false`, the runner received a cooperative cancel signal.

**Error (already terminal):**

```json
{
  "status": false,
  "error": "Cannot cancel job in completed state"
}
```

---

#### Retry a Job

Re-enqueues a `failed`, `canceled`, or `expired` job. A new attempt is scheduled using the same function, payload, and channel.

**Request (immediate retry):**

```json
POST /api/jobs/job/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

{
  "retry_request": true
}
```

**Request (delayed retry):**

```json
POST /api/jobs/job/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

{
  "retry_request": { "retry": true, "delay": 300 }
}
```

`delay` is in seconds.

**Response:**

```json
{
  "status": true,
  "message": "Job retry scheduled",
  "original_job_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "new_job_id": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
  "delayed": true
}
```

**Error (not retriable):**

```json
{
  "status": false,
  "error": "Cannot retry job in running state"
}
```

---

#### Get Detailed Status

Returns extended status information including recent events and queue position for pending jobs.

**Request:**

```json
POST /api/jobs/job/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

{
  "get_status": true
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    "status": "failed",
    "channel": "email",
    "func": "myapp.tasks.send_welcome_email",
    "created": "2024-01-15T10:00:00Z",
    "started_at": "2024-01-15T10:00:01Z",
    "finished_at": "2024-01-15T10:00:03Z",
    "attempt": 1,
    "max_retries": 3,
    "last_error": "User matching query does not exist.",
    "metadata": {},
    "runner_id": "runner-host1-abc123",
    "cancel_requested": false,
    "duration_ms": 2000,
    "is_terminal": true,
    "is_retriable": true,
    "recent_events": [
      { "event": "failed", "at": "2024-01-15T10:00:03Z", "runner_id": "runner-host1-abc123", "details": {} },
      { "event": "running", "at": "2024-01-15T10:00:01Z", "runner_id": "runner-host1-abc123", "details": {} },
      { "event": "created", "at": "2024-01-15T10:00:00Z", "runner_id": null, "details": {} }
    ]
  }
}
```

When the job is `pending` and scheduled with a `run_at`, the response also includes a `queue_position` field indicating where it sits in the channel's scheduled queue.

---

#### Publish from Template

Publishes a new job using the current job as a template. Useful for cloning a job with optional overrides.

**Request:**

```json
POST /api/jobs/job/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

{
  "publish_job": {
    "payload": { "user_id": 99 },
    "delay": 60
  }
}
```

All fields are optional. Fields not provided inherit from the template job.

| Override field | Description |
|---|---|
| `func` | Override the job function path |
| `payload` | Override the job payload |
| `channel` | Override the queue channel |
| `delay` | Delay in seconds before running |
| `run_at` | Explicit ISO datetime to run at |
| `max_retries` | Override max retries |
| `broadcast` | Override broadcast flag |

**Response:**

```json
{
  "status": true,
  "message": "Job published successfully",
  "job_id": "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
  "template_job_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
}
```

---

### Quick Status Endpoint

**GET** `/api/jobs/status/<job_id>`

Lightweight read-only status check that returns the job's core state fields without requiring a graph parameter.

```
GET /api/jobs/status/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
```

**Response:**

```json
{
  "status": true,
  "data": {
    "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    "status": "completed",
    "channel": "email",
    "func": "myapp.tasks.send_welcome_email",
    "created": "2024-01-15T10:00:00Z",
    "started_at": "2024-01-15T10:00:01Z",
    "finished_at": "2024-01-15T10:00:03Z",
    "attempt": 1,
    "last_error": "",
    "metadata": {}
  }
}
```

**Response (not found — 404):**

```json
{
  "status": false,
  "error": "Job not found"
}
```

---

### Cancel (standalone endpoint)

**POST** `/api/jobs/cancel`

Cancels a job by ID. Body parameter, not URL segment. Requires `manage_jobs`.

**Request:**

```json
{
  "job_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
}
```

**Response:**

```json
{
  "status": true,
  "message": "Job a1b2c3d4... cancellation requested"
}
```

---

### Retry (standalone endpoint)

**POST** `/api/jobs/retry`

Retries a failed or canceled job by ID. Optionally delays the retry. Requires `manage_jobs`.

**Request:**

```json
{
  "job_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "delay": 120
}
```

`delay` is optional (seconds). Omit for an immediate retry.

**Response:**

```json
{
  "status": true,
  "message": "Job retry scheduled",
  "original_job_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "new_job_id": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
  "delayed": true
}
```

**Response (not found — 404):**

```json
{
  "status": false,
  "error": "Job not found"
}
```

---

## Job Events

Events are append-only audit records automatically created by the system at each job state transition. They cannot be created or modified via the API.

### List Events

**GET** `/api/jobs/event`

**Query parameters:**

| Parameter | Example | Description |
|---|---|---|
| `job_id` | `?job_id=a1b2c3d4...` | Filter by job |
| `channel` | `?channel=email` | Filter by channel |
| `event` | `?event=failed` | Filter by event type |
| `runner_id` | `?runner_id=runner-host1-abc` | Filter by runner |
| `dr_start` / `dr_end` | Date range on `at` |
| `sort` | `?sort=-at` | Sort field |
| `graph` | `?graph=timeline` | Response graph |

**Response:**

```json
{
  "status": true,
  "count": 6,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": 1001,
      "event": "completed",
      "at": "2024-01-15T10:00:03Z",
      "runner_id": "runner-host1-abc123",
      "attempt": 1,
      "details": {}
    }
  ]
}
```

### Event Types

| Event | When it fires |
|---|---|
| `created` | Job was published |
| `queued` | Job was placed onto the Redis stream |
| `scheduled` | Job was placed in the scheduled queue (has `run_at`) |
| `claimed` | A runner took ownership |
| `running` | Execution began |
| `completed` | Execution finished successfully |
| `failed` | Execution threw an exception |
| `retry` | Job has been re-enqueued after failure |
| `canceled` | Job was canceled |
| `expired` | Job expired before being claimed |
| `released` | Runner released the job back to the queue |

### Event Graphs

| Graph | Fields |
|---|---|
| `default` | `id`, `event`, `at`, `runner_id`, `attempt`, `details` |
| `detail` | `id`, `job_id`, `channel`, `event`, `at`, `runner_id`, `attempt`, `details` |
| `timeline` | `event`, `at`, `runner_id`, `details` |

---

## Job Logs

Job log entries are structured messages written by the job function itself during execution using `job.add_log()`. They cannot be created via the API.

### List Logs

**GET** `/api/jobs/logs`

**Query parameters:**

| Parameter | Example | Description |
|---|---|---|
| `job_id` | `?job_id=a1b2c3d4...` | Filter by job |
| `channel` | `?channel=email` | Filter by channel |
| `kind` | `?kind=error` | Filter by severity (`debug`, `info`, `warn`, `error`) |
| `sort` | `?sort=-created` | Sort field |
| `graph` | `?graph=detail` | Response graph |

**Response:**

```json
{
  "status": true,
  "count": 3,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": 501,
      "job_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
      "created": "2024-01-15T10:00:02Z",
      "kind": "info",
      "message": "Sent email to user@example.com"
    }
  ]
}
```

### Log Graphs

| Graph | Fields |
|---|---|
| `default` | `id`, `job_id`, `created`, `kind`, `message` |
| `detail` | `id`, `job_id`, `channel`, `created`, `kind`, `message`, `meta` |

---

## Health

Health endpoints report the operational state of job queues. No jobs are modified.

### All Channels Overview

**GET** `/api/jobs/health`

Returns aggregated health status across all configured channels.

**Response:**

```json
{
  "status": true,
  "data": {
    "overall_status": "warning",
    "totals": {
      "unclaimed": 14,
      "pending": 2,
      "stuck": 1,
      "runners": 3
    },
    "channels": {
      "default": { "...": "see single-channel response below" },
      "email":   { "...": "..." },
      "priority": { "...": "..." }
    }
  }
}
```

`overall_status` is the worst status across all channels: `healthy`, `warning`, or `critical`.

---

### Single Channel Health

**GET** `/api/jobs/health/<channel>`

```
GET /api/jobs/health/email
```

**Response:**

```json
{
  "status": true,
  "data": {
    "channel": "email",
    "status": "healthy",
    "messages": {
      "total": 120,
      "unclaimed": 8,
      "pending": 2,
      "scheduled": 5,
      "stuck": 0
    },
    "runners": {
      "active": 2,
      "total": 2
    },
    "stuck_jobs": [],
    "alerts": []
  }
}
```

**Channel status values:**

| Status | Condition |
|---|---|
| `healthy` | No issues detected |
| `warning` | Unclaimed > 100, or any stuck jobs present |
| `critical` | Unclaimed > 500, more than 10 stuck jobs, or no runners with pending messages |

**Stuck jobs** are jobs in `running` state whose runner has stopped sending heartbeats. Up to 10 are listed in `stuck_jobs`.

---

## Stats

**GET** `/api/jobs/stats`

Returns a system-wide snapshot of queue sizes and database counts.

**Response:**

```json
{
  "status": true,
  "data": {
    "channels": {
      "default": {
        "stream_length": 42,
        "pending_count": 3,
        "scheduled_count": 5,
        "db_running": 3
      },
      "email": {
        "stream_length": 18,
        "pending_count": 1,
        "scheduled_count": 2,
        "db_running": 1
      }
    },
    "runners": [
      {
        "runner_id": "runner-host1-abc123",
        "channels": ["default", "email"],
        "jobs_processed": 4821,
        "jobs_failed": 12,
        "started": "2024-01-15T08:00:00Z",
        "last_heartbeat": "2024-01-15T10:05:58Z",
        "alive": true
      }
    ],
    "totals": {
      "pending": 60,
      "queued": 60,
      "inflight": 4,
      "running": 4,
      "running_active": 4,
      "running_stale": 0,
      "completed": 98432,
      "failed": 214,
      "scheduled": 7,
      "runners_active": 2
    },
    "scheduler": {
      "active": true,
      "lock_holder": "runner-host1-abc123"
    }
  }
}
```

**Totals field reference:**

| Field | Description |
|---|---|
| `pending` / `queued` | Jobs in Redis waiting to be claimed (alias of each other) |
| `inflight` | Jobs currently claimed by a runner in Redis |
| `running` | Jobs with `status=running` in the database |
| `running_active` | Running jobs whose runner is still alive |
| `running_stale` | Running jobs whose runner has gone away (potential stuck jobs) |
| `completed` | All-time completed job count in the database |
| `failed` | All-time failed job count in the database |
| `scheduled` | Jobs in the scheduled queue across all channels |
| `runners_active` | Number of runners with a live heartbeat |

---

## Runners

### List Runners

**GET** `/api/jobs/runners`

Returns all runners with their current heartbeat data. An optional `channel` filter narrows results to runners serving that channel.

```
GET /api/jobs/runners
GET /api/jobs/runners?channel=email
```

**Response:**

```json
{
  "status": true,
  "count": 2,
  "data": [
    {
      "id": "runner-host1-abc123",
      "runner_id": "runner-host1-abc123",
      "channels": ["default", "email"],
      "jobs_processed": 4821,
      "jobs_failed": 12,
      "started": "2024-01-15T08:00:00Z",
      "last_heartbeat": "2024-01-15T10:05:58Z",
      "alive": true
    },
    {
      "id": "runner-host2-def456",
      "runner_id": "runner-host2-def456",
      "channels": ["priority"],
      "jobs_processed": 1203,
      "jobs_failed": 2,
      "started": "2024-01-15T08:00:05Z",
      "last_heartbeat": "2024-01-15T10:05:55Z",
      "alive": true
    }
  ]
}
```

`alive` is `false` when the runner's last heartbeat is older than 3× the heartbeat interval (default: 15 seconds). Dead runners remain visible until their heartbeat key expires in Redis.

---

### Ping a Runner

**POST** `/api/jobs/runners/ping`

Sends a live ping to a specific runner and waits for its response. Useful for confirming a runner is truly responsive, not just alive on paper.

**Request:**

```json
{
  "runner_id": "runner-host1-abc123",
  "timeout": 2.0
}
```

`timeout` is optional (seconds, default `2.0`).

**Response:**

```json
{
  "status": true,
  "runner_id": "runner-host1-abc123",
  "responsive": true
}
```

`responsive` is `false` if the runner did not reply within the timeout.

---

### Shut Down a Runner

**POST** `/api/jobs/runners/shutdown`

Sends a shutdown command to a specific runner. The runner will finish its current job (if any) and then exit.

**Request:**

```json
{
  "runner_id": "runner-host1-abc123",
  "graceful": true
}
```

`graceful` is optional (boolean, default `true`).

**Response:**

```json
{
  "status": true,
  "message": "Shutdown command sent to runner runner-host1-abc123"
}
```

This is a fire-and-forget command. A `status: true` response means the command was dispatched, not that the runner has exited. Poll `/api/jobs/runners` to confirm the runner is gone.

---

### Broadcast Command

**POST** `/api/jobs/runners/broadcast`

Sends a command to all active runners and collects their replies.

**Request:**

```json
{
  "command": "status",
  "data": {},
  "timeout": 2.0
}
```

| Field | Required | Description |
|---|---|---|
| `command` | Yes | One of `status`, `shutdown`, `pause`, `resume`, `reload` |
| `data` | No | Optional command data dict |
| `timeout` | No | Seconds to wait for replies (default `2.0`) |

**Response:**

```json
{
  "status": true,
  "command": "status",
  "responses_count": 2,
  "responses": [
    {
      "runner_id": "runner-host1-abc123",
      "channels": ["default", "email"],
      "jobs_processed": 4821,
      "jobs_failed": 12,
      "started": "2024-01-15T08:00:00Z",
      "timestamp": "2024-01-15T10:06:00Z"
    },
    {
      "runner_id": "runner-host2-def456",
      "channels": ["priority"],
      "jobs_processed": 1203,
      "jobs_failed": 2,
      "started": "2024-01-15T08:00:05Z",
      "timestamp": "2024-01-15T10:06:00Z"
    }
  ]
}
```

If a runner does not reply before the timeout it will simply be absent from `responses`. `responses_count` reflects the number of replies actually received.

**Error (invalid command):**

```json
{
  "status": false,
  "error": "Invalid command. Must be one of: status, shutdown, pause, resume, reload"
}
```

---

## Runner Sysinfo

Collects live host system information (CPU, memory, disk, network) directly from the runner processes. Each runner executes the sysinfo function in-process and replies with its host's current metrics. No job record is created.

> **Requirement:** `psutil` must be installed in the runner environment (`pip install psutil`).

---

### All Runners

**GET** `/api/jobs/runners/sysinfo`

Requests sysinfo from every active runner simultaneously and returns all replies.

```
GET /api/jobs/runners/sysinfo
GET /api/jobs/runners/sysinfo?timeout=10.0
```

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `timeout` | `5.0` | Seconds to wait for replies (float) |

**Response:**

```json
{
  "status": true,
  "count": 2,
  "data": [
    {
      "runner_id": "runner-host1-abc123",
      "func": "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo",
      "status": "success",
      "timestamp": "2024-01-15T10:06:00.123456",
      "result": {
        "time": 1705316760.123,
        "datetime": "2024-01-15T10:06:00.123456",
        "os": {
          "system": "Linux",
          "version": "#1 SMP Debian 6.1.0",
          "hostname": "host1",
          "release": "6.1.0-18-amd64",
          "processor": "x86_64",
          "machine": "x86_64"
        },
        "boot_time": 1705230000.0,
        "cpu_load": 12.5,
        "cpus_load": [10.0, 15.0, 11.2, 13.8],
        "cpu": {
          "count": 4,
          "freq": { "current": 2400.0, "min": 400.0, "max": 3600.0 }
        },
        "memory": {
          "total": 8589934592,
          "used": 3604480000,
          "available": 4985454592,
          "percent": 42.1
        },
        "disk": {
          "total": 107374182400,
          "used": 40802189312,
          "free": 66571993088,
          "percent": 38.0
        },
        "network": {
          "tcp_cons": 24,
          "bytes_sent": 1048576,
          "bytes_recv": 2097152,
          "packets_sent": 8192,
          "packets_recv": 16384,
          "errin": 0,
          "errout": 0,
          "dropin": 0,
          "dropout": 0
        },
        "users": []
      }
    },
    {
      "runner_id": "runner-host2-def456",
      "func": "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo",
      "status": "success",
      "timestamp": "2024-01-15T10:06:00.198765",
      "result": { "...": "same shape as above" }
    }
  ]
}
```

Returns `"count": 0` and `"data": []` when no runners respond within the timeout.

---

### Specific Runner

**GET** `/api/jobs/runners/sysinfo/<runner_id>`

Requests sysinfo from a single runner.

```
GET /api/jobs/runners/sysinfo/runner-host1-abc123
GET /api/jobs/runners/sysinfo/runner-host1-abc123?timeout=10.0
```

**Response (200):**

```json
{
  "status": true,
  "data": {
    "runner_id": "runner-host1-abc123",
    "func": "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo",
    "status": "success",
    "timestamp": "2024-01-15T10:06:00.123456",
    "result": {
      "time": 1705316760.123,
      "datetime": "2024-01-15T10:06:00.123456",
      "os": {
        "system": "Linux",
        "hostname": "host1",
        "release": "6.1.0-18-amd64",
        "processor": "x86_64",
        "machine": "x86_64",
        "version": "#1 SMP Debian 6.1.0"
      },
      "boot_time": 1705230000.0,
      "cpu_load": 12.5,
      "cpus_load": [10.0, 15.0, 11.2, 13.8],
      "cpu": {
        "count": 4,
        "freq": { "current": 2400.0, "min": 400.0, "max": 3600.0 }
      },
      "memory": {
        "total": 8589934592,
        "used": 3604480000,
        "available": 4985454592,
        "percent": 42.1
      },
      "disk": {
        "total": 107374182400,
        "used": 40802189312,
        "free": 66571993088,
        "percent": 38.0
      },
      "network": {
        "tcp_cons": 24,
        "bytes_sent": 1048576,
        "bytes_recv": 2097152,
        "packets_sent": 8192,
        "packets_recv": 16384,
        "errin": 0,
        "errout": 0,
        "dropin": 0,
        "dropout": 0
      },
      "users": []
    }
  }
}
```

**Response (404) — runner unknown or timed out:**

```json
{
  "status": false,
  "error": "Runner runner-host1-abc123 did not respond"
}
```

A 404 means either the runner ID does not exist or the runner did not reply within the timeout. Increase `timeout` before assuming the runner is unresponsive.

---

### Sysinfo Result Field Reference

| Field | Type | Description |
|---|---|---|
| `time` | float | Unix timestamp on the runner host |
| `datetime` | string | ISO datetime on the runner host |
| `boot_time` | float | Unix timestamp when the host last booted |
| `os.system` | string | OS name (`Linux`, `Darwin`, `Windows`) |
| `os.hostname` | string | Hostname of the runner host |
| `os.release` | string | OS release version |
| `os.processor` | string | Processor architecture |
| `os.machine` | string | Machine type |
| `os.version` | string | Full OS version string |
| `cpu_load` | float | Overall CPU usage percent (0–100) |
| `cpus_load` | array | Per-core CPU usage percents |
| `cpu.count` | integer | Number of logical CPUs |
| `cpu.freq` | object\|null | CPU frequency in MHz: `current`, `min`, `max` |
| `memory.total` | integer | Total RAM in bytes |
| `memory.used` | integer | Used RAM in bytes |
| `memory.available` | integer | Available RAM in bytes |
| `memory.percent` | float | RAM usage percent |
| `disk.total` | integer | Root filesystem size in bytes |
| `disk.used` | integer | Used disk space in bytes |
| `disk.free` | integer | Free disk space in bytes |
| `disk.percent` | float | Disk usage percent |
| `network.tcp_cons` | integer | Number of established TCP connections |
| `network.bytes_sent` | integer | Cumulative bytes sent since boot |
| `network.bytes_recv` | integer | Cumulative bytes received since boot |
| `network.packets_sent` | integer | Cumulative packets sent since boot |
| `network.packets_recv` | integer | Cumulative packets received since boot |
| `network.errin` | integer | Input errors |
| `network.errout` | integer | Output errors |
| `network.dropin` | integer | Inbound packets dropped |
| `network.dropout` | integer | Outbound packets dropped |
| `users` | array | Currently logged-in users on the host |

---

### Sysinfo Error Reply

When a runner encounters an error collecting sysinfo (for example, `psutil` is not installed), the reply has `status: "error"` and an `error` field in place of `result`:

```json
{
  "runner_id": "runner-host1-abc123",
  "func": "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo",
  "status": "error",
  "error": "psutil is not installed. Install it with: pip install psutil",
  "timestamp": "2024-01-15T10:06:00.123456"
}
```

On the all-runners endpoint, error replies appear alongside successful ones in the `data` array — always check `data[n].status` before reading `data[n].result`.

---

## Common Error Responses

All endpoints return a consistent error envelope on failure.

**400 Bad Request** — invalid parameters or illegal operation:

```json
{
  "status": false,
  "error": "Cannot cancel job in completed state"
}
```

**401 / 403 Unauthorized** — missing or insufficient credentials:

```json
{
  "status": false,
  "error": "unauthorized"
}
```

**404 Not Found** — resource does not exist or did not respond:

```json
{
  "status": false,
  "error": "Job not found"
}
```
