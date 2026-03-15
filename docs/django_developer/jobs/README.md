# Jobs — Django Developer Reference

The jobs system provides Redis-backed async task processing with persistence, scheduling, and monitoring.

- [Publishing Jobs](publishing.md) — How to enqueue and schedule async jobs
- [Writing Job Functions](writing_jobs.md) — Job function signature, lifecycle, best practices
- [Job Model](job_model.md) — Job database model, status, querying

## Runner Sysinfo

Collect live host system information (CPU, memory, disk, network) from one or all active job runners via the broadcast-execute mechanism.

```django-mojo/mojo/apps/jobs/__init__.py#L1-1
from mojo.apps import jobs

# All active runners
info = jobs.get_sysinfo()

# One specific runner
info = jobs.get_sysinfo(runner_id='runner-host1-abc123')

# Custom timeout (default 5.0s)
info = jobs.get_sysinfo(timeout=10.0)
```

### Return shape

`get_sysinfo()` always returns a list — one entry per responding runner:

```django-mojo/mojo/apps/jobs/__init__.py#L1-1
[
    {
        "runner_id": "runner-host1-abc123",
        "func": "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo",
        "status": "success",       # or "error"
        "timestamp": "2026-03-14T10:00:00.000000",
        "result": {
            "os": { "system": "Linux", "hostname": "host1", ... },
            "cpu_load": 12.5,
            "cpus_load": [10.0, 15.0],
            "memory": { "total": 8589934592, "used": ..., "percent": 42.1 },
            "disk":   { "total": 107374182400, "used": ..., "percent": 38.0 },
            "network": { "tcp_cons": 24, "bytes_sent": ..., "bytes_recv": ... }
        }
    }
]
```

On failure (e.g. `psutil` not installed on the runner), the entry has `status: "error"` and an `error` key instead of `result`.

An empty list `[]` is returned when no runners respond (no live runners, unknown runner ID, or timeout).

### How it works

`get_sysinfo()` uses the existing `broadcast_execute` / `execute_on_runner` control channel mechanism. It calls `mojo.apps.jobs.services.sysinfo_task.collect_sysinfo` in-process on each runner, which calls `mojo.helpers.sysinfo.get_host_info()`. No job record is created and there is no retry — this is a real-time, fire-and-collect operation.

### Requirements

`psutil` must be installed in the runner's Python environment:

```django-mojo/mojo/apps/jobs/__init__.py#L1-1
pip install psutil
```
