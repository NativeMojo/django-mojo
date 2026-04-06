# Scheduled Tasks — REST API Reference

User-defined recurring or one-off tasks that run at specific times of day on specific days of the week. Each task belongs to the authenticated user — owners manage their own tasks; admins with elevated permissions can access all.

---

## Permissions

| Permission | Grants |
|---|---|
| `owner` | Full CRUD on your own tasks and results |
| `view_scheduled_tasks` | Read all tasks and results (admin) |
| `manage_scheduled_tasks` | Full CRUD on all tasks; delete any result (admin) |

The domain permission `jobs` is also required alongside the fine-grained permission for admin access.

---

## Endpoint Index

| Method | Path | Who can call | Description |
|---|---|---|---|
| GET | `/api/jobs/scheduled_task` | owner, `view_scheduled_tasks` | List your tasks |
| POST | `/api/jobs/scheduled_task` | owner, `manage_scheduled_tasks` | Create a task |
| GET | `/api/jobs/scheduled_task/<id>` | owner, `view_scheduled_tasks` | Get task detail |
| PUT | `/api/jobs/scheduled_task/<id>` | owner, `manage_scheduled_tasks` | Update a task |
| DELETE | `/api/jobs/scheduled_task/<id>` | owner, `manage_scheduled_tasks` | Delete a task |
| GET | `/api/jobs/task_result` | owner, `view_scheduled_tasks` | List execution results |
| GET | `/api/jobs/task_result/<id>` | owner, `view_scheduled_tasks` | Get result detail |

---

## Scheduled Tasks

### List Tasks

**GET** `/api/jobs/scheduled_task`

Returns a paginated list of the authenticated user's tasks (or all tasks for admins).

**Query parameters:**

| Parameter | Example | Description |
|---|---|---|
| `enabled` | `?enabled=true` | Filter by enabled state |
| `task_type` | `?task_type=llm` | Filter by type (`job`, `webhook`, `llm`) |
| `sort` | `?sort=-created` | Sort field, prefix `-` for descending |
| `size` | `?size=25` | Page size (default 20) |
| `start` | `?start=20` | Pagination offset |
| `graph` | `?graph=list` | Response graph (`list` or `default`) |

**Response:**

```json
{
  "status": true,
  "count": 3,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
      "name": "Daily report",
      "enabled": true,
      "run_once": false,
      "task_type": "llm",
      "run_times": ["09:00"],
      "run_days": [0, 1, 2, 3, 4],
      "last_run": "2026-04-06T09:00:12Z",
      "run_count": 5,
      "created": "2026-03-01T14:00:00Z"
    }
  ]
}
```

The `list` graph (shown above) is the default for list requests. Use `?graph=default` to receive all fields including `job_config`, `notify`, `channel`, `max_retries`, `last_error`, and `description`.

---

### Create Task

**POST** `/api/jobs/scheduled_task`

**Request body:**

```json
{
  "name": "Daily report",
  "description": "Generate a morning summary",
  "task_type": "llm",
  "run_times": ["09:00"],
  "run_days": [0, 1, 2, 3, 4],
  "job_config": {
    "system_prompt": "You are a concise analyst.",
    "user_prompt": "Summarize yesterday's activity."
  },
  "notify": ["in_app", "email"],
  "enabled": true,
  "run_once": false,
  "channel": "default",
  "max_retries": 0
}
```

**Required fields:** `name`, `task_type`, `run_times`

**Field rules:**

| Field | Rule |
|-------|------|
| `task_type` | Must be `"job"`, `"webhook"`, or `"llm"` |
| `run_times` | List of `"HH:MM"` strings (24-hour), max 2 entries |
| `run_days` | List of ints 0–6 (Mon=0). Omit or send `[]` for every day |
| `notify` | Subset of `["email", "in_app", "sms", "push"]` |
| `job_config` | Shape depends on `task_type` (see below) |

**job_config by task_type:**

| task_type | Required keys | Optional keys |
|-----------|---------------|---------------|
| `job` | `func` | `payload` |
| `webhook` | `url` | `data` |
| `llm` | `user_prompt` | `system_prompt` |

**Limits:** A user may have at most 10 tasks (configurable server-side).

**Response:** The created task in `default` graph.

```json
{
  "status": true,
  "data": {
    "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    "name": "Daily report",
    "description": "Generate a morning summary",
    "enabled": true,
    "run_once": false,
    "task_type": "llm",
    "run_times": ["09:00"],
    "run_days": [0, 1, 2, 3, 4],
    "job_config": {
      "system_prompt": "You are a concise analyst.",
      "user_prompt": "Summarize yesterday's activity."
    },
    "notify": ["in_app", "email"],
    "channel": "default",
    "max_retries": 0,
    "last_run": null,
    "run_count": 0,
    "last_error": "",
    "created": "2026-04-06T14:00:00Z",
    "modified": "2026-04-06T14:00:00Z"
  }
}
```

---

### Get Task

**GET** `/api/jobs/scheduled_task/<id>`

Returns the full `default` graph for one task. Returns 403 if the task belongs to another user and you lack `view_scheduled_tasks`.

---

### Update Task

**PUT** `/api/jobs/scheduled_task/<id>`

Send any subset of writable fields. Returns the updated task in `default` graph.

```json
{
  "enabled": false
}
```

To pause a task, set `enabled` to `false`. To re-enable it, set `enabled` to `true`.

---

### Delete Task

**DELETE** `/api/jobs/scheduled_task/<id>`

Deletes the task and all associated `TaskResult` records (CASCADE). Returns `{"status": true}`.

---

## Task Results

Execution results are created automatically — they cannot be created or modified via REST.

### List Results

**GET** `/api/jobs/task_result`

Returns a paginated list of results for the authenticated user's tasks.

**Query parameters:**

| Parameter | Example | Description |
|---|---|---|
| `task` | `?task=a1b2c3...` | Filter by task ID |
| `status` | `?status=error` | Filter by status (`success`, `error`) |
| `sort` | `?sort=-created` | Sort field |
| `size` | `?size=50` | Page size |
| `start` | `?start=0` | Pagination offset |
| `graph` | `?graph=default` | Response graph (`list` or `default`) |

**Response:**

```json
{
  "status": true,
  "count": 12,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
      "task_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
      "status": "success",
      "created": "2026-04-06T09:00:14Z"
    }
  ]
}
```

Use `?graph=default` for the full result including `output`, `error`, and `job_id`.

---

### Get Result

**GET** `/api/jobs/task_result/<id>`

Returns one result in `default` graph.

```json
{
  "status": true,
  "data": {
    "id": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
    "task_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    "job_id": "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
    "status": "success",
    "output": "Here is today's summary: ...",
    "error": "",
    "created": "2026-04-06T09:00:14Z"
  }
}
```

---

## Error Responses

| Code | Meaning |
|------|---------|
| 400 | Validation error — invalid `run_times`, `task_type`, `job_config`, or task limit exceeded |
| 403 | Not authenticated, or accessing another user's task without admin permission |
| 404 | Task or result not found |
