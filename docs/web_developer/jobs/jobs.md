# Job API — REST API Reference

## Permissions Required

- `view_taskqueue` or `manage_users`

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/jobs/job` | List jobs |
| GET | `/api/jobs/job/<id>` | Get job |
| POST | `/api/jobs/job/<id>` | Update job (cancel, retry) |

## List Jobs

**GET** `/api/jobs/job`

```
GET /api/jobs/job?status=failed&sort=-created&size=20
GET /api/jobs/job?channel=emails&status=pending
```

**Response:**

```json
{
  "status": true,
  "count": 5,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": "a1b2c3d4...",
      "func": "myapp.services.email.send_welcome",
      "channel": "default",
      "status": "failed",
      "created": "2024-01-15T10:00:00Z",
      "run_at": null
    }
  ]
}
```

## Get Job Detail

**GET** `/api/jobs/job/<id>`

```json
{
  "status": true,
  "data": {
    "id": "a1b2c3d4...",
    "func": "myapp.services.email.send_welcome",
    "channel": "default",
    "status": "failed",
    "created": "2024-01-15T10:00:00Z",
    "started_at": "2024-01-15T10:00:01Z",
    "completed_at": "2024-01-15T10:00:02Z",
    "error": "User matching query does not exist.",
    "metadata": {},
    "result": null
  }
}
```

## Cancel a Job

**POST** `/api/jobs/job/<id>`

```json
{
  "action": "cancel"
}
```

## Available Graphs

| Graph | Fields |
|---|---|
| `list` | id, func, channel, status, created, run_at |
| `default` | All fields including result, error, metadata |

## Filtering

| Filter | Example | Description |
|---|---|---|
| `status` | `?status=failed` | Job status |
| `channel` | `?channel=emails` | Queue channel |
| `func` | `?func=myapp.services.email.send_welcome` | Job function |
| `dr_start`/`dr_end` | Date range on `created` |

## Runner Sysinfo

Collect live host system information from one or all active job runners.

### Permissions Required

- `manage_jobs` or `view_jobs`

### All Runners

**GET** `/api/jobs/runners/sysinfo`

Optional query parameter: `timeout` (float, seconds, default `5.0`).

```
GET /api/jobs/runners/sysinfo
GET /api/jobs/runners/sysinfo?timeout=10.0
```

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
      "timestamp": "2026-03-14T10:00:00.000000",
      "result": {
        "os": { "system": "Linux", "hostname": "host1", "release": "6.1.0" },
        "cpu_load": 12.5,
        "cpus_load": [10.0, 15.0],
        "memory": { "total": 8589934592, "used": 3604480000, "percent": 42.1 },
        "disk":   { "total": 107374182400, "used": 40802189312, "percent": 38.0 },
        "network": { "tcp_cons": 24, "bytes_sent": 1048576, "bytes_recv": 2097152 }
      }
    }
  ]
}
```

Returns an empty `data` list when no runners respond within the timeout.

### Specific Runner

**GET** `/api/jobs/runners/sysinfo/<runner_id>`

Optional query parameter: `timeout` (float, seconds, default `5.0`).

```
GET /api/jobs/runners/sysinfo/runner-host1-abc123
```

**Response (200):**

```json
{
  "status": true,
  "data": {
    "runner_id": "runner-host1-abc123",
    "func": "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo",
    "status": "success",
    "timestamp": "2026-03-14T10:00:00.000000",
    "result": {
      "os": { "system": "Linux", "hostname": "host1", "release": "6.1.0" },
      "cpu_load": 12.5,
      "cpus_load": [10.0, 15.0],
      "memory": { "total": 8589934592, "used": 3604480000, "percent": 42.1 },
      "disk":   { "total": 107374182400, "used": 40802189312, "percent": 38.0 },
      "network": { "tcp_cons": 24, "bytes_sent": 1048576, "bytes_recv": 2097152 }
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

### Error Reply Shape

When a runner encounters an error executing the sysinfo function (e.g. `psutil` not installed), the reply entry has `status: "error"` instead of `"success"`, and an `error` field in place of `result`:

```json
{
  "runner_id": "runner-host1-abc123",
  "status": "error",
  "error": "psutil is not installed. Install it with: pip install psutil",
  "timestamp": "2026-03-14T10:00:00.000000"
}
```
