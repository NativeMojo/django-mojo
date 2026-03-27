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
| `JOBS_CHANNELS` | `["default"]` | List of channels the scheduler monitors |

Configure channels and run separate engine processes per channel:

```python
JOBS_CHANNELS = ['default', 'emails', 'webhooks', 'heavy', 'maintenance']
```

```bash
python manage.py jobs_engine --channels emails --max-workers 20
python manage.py jobs_engine --channels heavy --max-workers 5
```

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
| `JOBS_WEBHOOK_USER_AGENT` | `"Django-MOJO-Webhook/1.0"` | Default User-Agent header |

## Example Configurations

### Minimal (Development)

```python
# Uses all defaults — just need Redis running
JOBS_CHANNELS = ['default']
```

### Standard Production

```python
JOBS_REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
JOBS_CHANNELS = ['default', 'emails', 'webhooks']
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
