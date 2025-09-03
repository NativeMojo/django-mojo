# Jobs System REST API Documentation

## Overview

The Django-MOJO Jobs System provides a powerful distributed background job processing platform with REST APIs for publishing, monitoring, and managing asynchronous tasks. The system supports parallel execution, automatic retries, scheduled jobs, and comprehensive health monitoring.

### Key Features

- **Distributed Execution**: Jobs can run on any worker without pre-registration
- **Parallel Processing**: Thread pool execution for high throughput
- **Scheduled Jobs**: Delay execution or schedule for specific times
- **Automatic Retries**: Configurable retry logic with exponential backoff
- **Health Monitoring**: Real-time visibility into queue health and worker status
- **Job Cancellation**: Cooperative cancellation of running jobs
- **Broadcast Jobs**: Execute the same job on all workers
- **Object-Oriented Actions**: Use MojoModel POST_SAVE_ACTIONS for job operations

### Architecture

The jobs system consists of three main components:

1. **Job Publisher**: Web processes that create jobs
2. **Job Engine**: Worker processes that execute jobs
3. **Scheduler**: Moves scheduled jobs to execution queues

Jobs flow through the system:
```
Web Process → Publish → Redis Queue → Job Engine → Execute Function → Complete
                            ↑
                        Scheduler
```

## Authentication & Permissions

All jobs API endpoints require authentication via standard Django-MOJO auth. Different endpoints require different permissions:

- **Basic Operations**: Any authenticated user can publish and monitor their own jobs
- **`view_jobs`**: View all jobs and system health
- **`manage_jobs`**: Full control including job operations and runner management

## Base URL

All jobs endpoints are prefixed with `/api/jobs/`

---

## Job Operations (Object-Oriented Pattern)

The Jobs system follows Django-MOJO's object-oriented REST pattern using POST_SAVE_ACTIONS. This allows you to perform actions directly on Job objects through the standard CRUD interface.

### Job Model Actions

The Job model supports the following POST_SAVE_ACTIONS:
- `cancel_request`: Cancel a pending or running job
- `retry_request`: Retry a failed or cancelled job
- `get_status`: Get detailed job status
- `publish_job`: Create a new job using this job as a template

### Standard CRUD Operations

```http
GET /api/jobs/job
GET /api/jobs/job/<job_id>
POST /api/jobs/job/<job_id>
DELETE /api/jobs/job/<job_id>
```

All operations use the Job model's RestMeta configuration for permissions and graphs.

### Cancel a Job (OO Pattern)

Cancel a job using the object-oriented pattern:

```http
POST /api/jobs/job/<job_id>
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "cancel_request": true
}
```

**Response:**
```json
{
    "status": true,
    "message": "Job a1b2c3d4e5f6789012345678901234567 cancellation requested",
    "job_id": "a1b2c3d4e5f6789012345678901234567"
}
```

**Note:** Cancellation is cooperative - the job function must check `job.cancel_requested` and return early.

### Retry a Job (OO Pattern)

Retry a failed or cancelled job:

```http
POST /api/jobs/job/<job_id>
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body (immediate retry):**
```json
{
    "retry_request": true
}
```

**Request Body (delayed retry):**
```json
{
    "retry_request": {
        "retry": true,
        "delay": 60
    }
}
```

**Response:**
```json
{
    "status": true,
    "message": "Job retry scheduled",
    "original_job_id": "a1b2c3d4e5f6789012345678901234567",
    "new_job_id": "b2c3d4e5f67890123456789012345678",
    "delayed": true
}
```

### Get Job Status (OO Pattern)

Get detailed status including events and queue position:

```http
POST /api/jobs/job/<job_id>
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "get_status": true
}
```

**Response:**
```json
{
    "status": true,
    "data": {
        "id": "a1b2c3d4e5f6789012345678901234567",
        "status": "running",
        "channel": "emails",
        "func": "myapp.jobs.send_email",
        "created": "2024-01-15T10:45:00Z",
        "started_at": "2024-01-15T10:46:00Z",
        "finished_at": null,
        "attempt": 1,
        "max_retries": 3,
        "last_error": "",
        "metadata": {
            "emails_sent": 42
        },
        "runner_id": "worker-01-1234",
        "cancel_requested": false,
        "duration_ms": 0,
        "is_terminal": false,
        "is_retriable": true,
        "recent_events": [
            {
                "event": "created",
                "at": "2024-01-15T10:45:00Z",
                "runner_id": null,
                "details": {}
            },
            {
                "event": "running",
                "at": "2024-01-15T10:46:00Z",
                "runner_id": "worker-01-1234",
                "details": {}
            }
        ],
        "queue_position": null
    }
}
```

### Publish from Template (OO Pattern)

Create a new job using an existing job as a template:

```http
POST /api/jobs/job/<job_id>
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "publish_job": {
        "payload": {
            "recipients": ["new@example.com"],
            "subject": "Updated subject"
        },
        "delay": 300,
        "channel": "high_priority"
    }
}
```

**Response:**
```json
{
    "status": true,
    "message": "Job published successfully",
    "job_id": "c3d4e5f678901234567890123456789a",
    "template_job_id": "a1b2c3d4e5f6789012345678901234567"
}
```

---

## Direct API Endpoints

While the object-oriented pattern is preferred for consistency with Django-MOJO conventions, direct endpoints are also available for specific operations:

### Publish a New Job

```http
POST /api/jobs/publish
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "func": "myapp.jobs.send_email",
    "payload": {
        "recipients": ["user@example.com"],
        "subject": "Welcome",
        "template": "welcome_email"
    },
    "channel": "emails",
    "delay": 60,
    "max_retries": 3
}
```

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `func` | string | ✅ | Module path to job function (e.g., "myapp.jobs.process_data") |
| `payload` | object | ✅ | JSON data passed to the job function |
| `channel` | string | ❌ | Queue channel (default: "default") |
| `delay` | integer | ❌ | Delay in seconds before execution |
| `run_at` | string | ❌ | ISO datetime to run the job |
| `max_retries` | integer | ❌ | Maximum retry attempts (default: 3) |
| `expires_in` | integer | ❌ | Seconds until job expires (default: 900) |
| `expires_at` | string | ❌ | ISO datetime when job expires |
| `broadcast` | boolean | ❌ | If true, all workers execute the job |
| `max_exec_seconds` | integer | ❌ | Hard execution time limit |
| `idempotency_key` | string | ❌ | Key for exactly-once semantics |

### List Jobs

```http
GET /api/jobs/list?channel=emails&status=pending&limit=50
Authorization: Bearer <token>
```

**Query Parameters:**
- `channel`: Filter by channel name
- `status`: Filter by status (pending, running, completed, failed, canceled, expired)
- `since`: Jobs created after this ISO datetime
- `limit`: Maximum results (default: 100)

### Get Job Events

```http
GET /api/jobs/job/<job_id>/events
Authorization: Bearer <token>
```

---

## Health Monitoring

### Channel Health

Get comprehensive health metrics for a specific channel:

```http
GET /api/jobs/health/emails
Authorization: Bearer <token>
Permission: view_jobs or manage_jobs
```

**Response:**
```json
{
    "status": true,
    "data": {
        "channel": "emails",
        "status": "healthy",
        "messages": {
            "total": 150,
            "unclaimed": 10,
            "pending": 5,
            "scheduled": 25,
            "stuck": 0
        },
        "runners": {
            "active": 3,
            "total": 3
        },
        "stuck_jobs": [],
        "alerts": [],
        "metrics": {
            "jobs_per_minute": 45.2,
            "success_rate": 98.5,
            "avg_duration_ms": 230
        }
    }
}
```

### System Health Overview

```http
GET /api/jobs/health
Authorization: Bearer <token>
Permission: view_jobs or manage_jobs
```

### System Statistics

```http
GET /api/jobs/stats
Authorization: Bearer <token>
Permission: view_jobs or manage_jobs
```

---

## Runner Management

### List Active Runners

```http
GET /api/jobs/runners?channel=emails
Authorization: Bearer <token>
Permission: view_jobs or manage_jobs
```

### Ping Runner

```http
POST /api/jobs/runners/ping
Authorization: Bearer <token>
Permission: manage_jobs
Content-Type: application/json
```

**Request Body:**
```json
{
    "runner_id": "worker-01-1234",
    "timeout": 2.0
}
```

### Shutdown Runner

```http
POST /api/jobs/runners/shutdown
Authorization: Bearer <token>
Permission: manage_jobs
Content-Type: application/json
```

**Request Body:**
```json
{
    "runner_id": "worker-01-1234",
    "graceful": true
}
```

### Broadcast Command

```http
POST /api/jobs/runners/broadcast
Authorization: Bearer <token>
Permission: manage_jobs
Content-Type: application/json
```

**Request Body:**
```json
{
    "command": "status",
    "data": {},
    "timeout": 2.0
}
```

**Valid Commands:**
- `status`: Request status from all runners
- `shutdown`: Shutdown all runners
- `pause`: Pause job processing
- `resume`: Resume job processing
- `reload`: Reload configuration

---

## Writing Job Functions

Job functions are plain Python functions that accept a Django `Job` model:

```python
# myapp/jobs.py

def send_email(job):
    """
    Send email to recipients.
    
    The job parameter is a Django Job model with:
    - job.payload: Dict with job data
    - job.metadata: Dict for storing progress/results
    - job.cancel_requested: Boolean for cooperative cancellation
    """
    recipients = job.payload['recipients']
    subject = job.payload['subject']
    template = job.payload.get('template', 'default')
    
    # Check for cancellation
    if job.cancel_requested:
        job.metadata['cancelled'] = True
        return "cancelled"
    
    # Process the job
    sent_count = 0
    for email in recipients:
        # Check cancellation periodically for long jobs
        if sent_count % 10 == 0 and job.cancel_requested:
            job.metadata['cancelled_at'] = sent_count
            return "cancelled"
        
        send_mail(email, subject, template)
        sent_count += 1
    
    # Store results in metadata
    job.metadata['sent_count'] = sent_count
    job.metadata['completed_at'] = datetime.now().isoformat()
    
    return "completed"
```

**Important Notes:**
- No decorators or registration required
- Function must be importable by workers
- Return a string status (typically "completed", "failed", or "cancelled")
- Use `job.metadata` to store progress and results
- Check `job.cancel_requested` for cooperative cancellation
- Raising an exception triggers retry logic (if configured)

---

## Integration Examples

### Python SDK Example

```python
import requests
import json

class JobsClient:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
    
    def publish_job(self, func, payload, **options):
        """Publish a new job."""
        data = {
            'func': func,
            'payload': payload,
            **options
        }
        
        response = requests.post(
            f'{self.base_url}/api/jobs/publish',
            headers=self.headers,
            json=data
        )
        return response.json()
    
    def cancel_job(self, job_id):
        """Cancel a job using OO pattern."""
        response = requests.post(
            f'{self.base_url}/api/jobs/job/{job_id}',
            headers=self.headers,
            json={'cancel_request': True}
        )
        return response.json()
    
    def retry_job(self, job_id, delay=None):
        """Retry a job using OO pattern."""
        data = {'retry_request': {'retry': True, 'delay': delay}} if delay else {'retry_request': True}
        
        response = requests.post(
            f'{self.base_url}/api/jobs/job/{job_id}',
            headers=self.headers,
            json=data
        )
        return response.json()
    
    def get_job_status(self, job_id):
        """Get detailed job status using OO pattern."""
        response = requests.post(
            f'{self.base_url}/api/jobs/job/{job_id}',
            headers=self.headers,
            json={'get_status': True}
        )
        return response.json()

# Usage
client = JobsClient('https://api.example.com', 'your-token')

# Publish a job
result = client.publish_job(
    'myapp.jobs.process_data',
    {'file_path': '/data/upload.csv'},
    channel='uploads',
    max_retries=5
)
job_id = result['job_id']

# Get status
status = client.get_job_status(job_id)
print(f"Job status: {status['data']['status']}")

# Cancel if needed
if status['data']['status'] == 'running':
    client.cancel_job(job_id)

# Retry if failed
if status['data']['status'] == 'failed':
    client.retry_job(job_id, delay=60)
```

### JavaScript Example

```javascript
class JobsAPI {
    constructor(baseUrl, token) {
        this.baseUrl = baseUrl;
        this.headers = {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
        };
    }

    async publishJob(func, payload, options = {}) {
        const response = await fetch(`${this.baseUrl}/api/jobs/publish`, {
            method: 'POST',
            headers: this.headers,
            body: JSON.stringify({
                func,
                payload,
                ...options
            })
        });
        return response.json();
    }

    async cancelJob(jobId) {
        // Using OO pattern
        const response = await fetch(`${this.baseUrl}/api/jobs/job/${jobId}`, {
            method: 'POST',
            headers: this.headers,
            body: JSON.stringify({ cancel_request: true })
        });
        return response.json();
    }

    async retryJob(jobId, delay = null) {
        // Using OO pattern
        const data = delay 
            ? { retry_request: { retry: true, delay } }
            : { retry_request: true };
        
        const response = await fetch(`${this.baseUrl}/api/jobs/job/${jobId}`, {
            method: 'POST',
            headers: this.headers,
            body: JSON.stringify(data)
        });
        return response.json();
    }

    async getJobStatus(jobId) {
        // Using OO pattern
        const response = await fetch(`${this.baseUrl}/api/jobs/job/${jobId}`, {
            method: 'POST',
            headers: this.headers,
            body: JSON.stringify({ get_status: true })
        });
        return response.json();
    }
}

// Usage
const api = new JobsAPI('https://api.example.com', 'your-token');

// Publish a job
const result = await api.publishJob(
    'myapp.jobs.send_notification',
    { 
        user_id: 123,
        message: 'Your order is ready!'
    },
    { channel: 'notifications' }
);

console.log(`Job published: ${result.job_id}`);

// Check status
const status = await api.getJobStatus(result.job_id);
console.log(`Job status: ${status.data.status}`);

// Cancel if running too long
if (status.data.status === 'running') {
    await api.cancelJob(result.job_id);
}
```

---

## Best Practices

### Job Design

1. **Keep Payloads Small**: Store large data elsewhere and pass references
   ```python
   # Good
   publish('myapp.jobs.process_file', {'file_id': 123})
   
   # Avoid
   publish('myapp.jobs.process_file', {'data': huge_file_content})
   ```

2. **Make Jobs Idempotent**: Jobs may be retried, ensure they can run multiple times safely
   ```python
   def update_user_status(job):
       user_id = job.payload['user_id']
       new_status = job.payload['status']
       
       # Idempotent - setting status multiple times is safe
       User.objects.filter(id=user_id).update(status=new_status)
   ```

3. **Check Cancellation for Long Jobs**: Respect cancellation requests
   ```python
   def process_large_dataset(job):
       items = job.payload['items']
       
       for i, item in enumerate(items):
           if i % 100 == 0 and job.cancel_requested:
               job.metadata['stopped_at'] = i
               return "cancelled"
           
           process_item(item)
   ```

4. **Use Appropriate Channels**: Separate job types into different channels
   - `emails`: Email sending jobs
   - `uploads`: File processing jobs
   - `reports`: Report generation
   - `maintenance`: Cleanup and maintenance tasks

### Object-Oriented Pattern

When using the OO pattern with POST_SAVE_ACTIONS:

1. **Use Standard CRUD Endpoints**: Leverage the Job model's RestMeta configuration
2. **Actions are Idempotent**: Actions can be safely retried
3. **Check Response Status**: Always verify the action was successful
4. **Use Appropriate Permissions**: Actions respect the model's permission configuration

### Error Handling

1. **Let Exceptions Bubble**: The job engine handles retries automatically
   ```python
   def fetch_api_data(job):
       response = requests.get(job.payload['url'])
       response.raise_for_status()  # Will retry on 5xx errors
       return "completed"
   ```

2. **Use Metadata for Error Context**: Store debugging information
   ```python
   def process_order(job):
       try:
           # Process order
           pass
       except ValidationError as e:
           job.metadata['validation_errors'] = str(e)
           raise  # Still retry
   ```

### Performance

1. **Use Thread Pool Size Appropriately**: Configure based on job types
   - I/O bound jobs: Higher thread count (20-50)
   - CPU bound jobs: Lower thread count (5-10)

2. **Batch Operations**: Process multiple items in one job when possible

3. **Monitor Queue Health**: Watch for high unclaimed counts and stuck jobs

### Security

1. **Validate Job Functions**: Ensure job functions exist and are safe
2. **Sanitize Payloads**: Validate input data in job functions
3. **Use Permissions**: Restrict management operations appropriately
4. **Audit Job Execution**: Use JobEvent records for audit trail

---

## Error Handling

All endpoints return standard HTTP status codes with detailed error information:

### 400 Bad Request
```json
{
    "status": false,
    "error": "Invalid payload: must be a JSON object"
}
```

### 401 Unauthorized
```json
{
    "status": false,
    "error": "Authentication required"
}
```

### 403 Forbidden
```json
{
    "status": false,
    "error": "Permission denied: manage_jobs required"
}
```

### 404 Not Found
```json
{
    "status": false,
    "error": "Job not found"
}
```

---

## Configuration

Key settings for the jobs system:

```python
# settings.py

# Redis connection
JOBS_REDIS_URL = "redis://localhost:6379/0"

# Worker configuration
JOBS_ENGINE_MAX_WORKERS = 20          # Thread pool size
JOBS_ENGINE_CLAIM_BUFFER = 2          # Claim up to 2x workers
JOBS_ENGINE_CLAIM_BATCH = 5           # Claims per request

# Job defaults
JOBS_DEFAULT_EXPIRES_SEC = 900        # 15 minutes
JOBS_DEFAULT_MAX_RETRIES = 3
JOBS_DEFAULT_BACKOFF_BASE = 2.0
JOBS_DEFAULT_BACKOFF_MAX = 3600

# Channels
JOBS_CHANNELS = ['default', 'emails', 'uploads', 'reports']

# Limits
JOBS_PAYLOAD_MAX_BYTES = 1048576      # 1MB max payload
```

---

## Support

For additional help:
- Review the [Developer Guide](../future/jobs_refactor.md) for internal implementation details
- Check the [Migration Guide](../future/jobs_migration.md) for upgrading from older versions
- File issues for bugs or feature requests

The jobs system provides a robust foundation for background processing while maintaining simplicity and reliability. The object-oriented pattern with POST_SAVE_ACTIONS ensures consistency with the rest of the Django-MOJO framework while providing powerful job management capabilities.