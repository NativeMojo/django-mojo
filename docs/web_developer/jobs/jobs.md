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
