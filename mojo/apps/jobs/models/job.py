"""
Job and JobEvent models for the jobs system.
"""
from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates
from typing import Optional, Dict, Any


class Job(models.Model, MojoModel):
    """
    Represents a background job in the system.
    Stores current state and metadata for job execution.
    """

    # Primary identifier - UUID without dashes
    id = models.CharField(primary_key=True, max_length=32, editable=False)

    # Job targeting
    channel = models.CharField(max_length=100,
                              help_text="Logical queue/channel name")
    func = models.CharField(max_length=255,
                           help_text="Registry key for the job function")
    payload = models.JSONField(default=dict, blank=True,
                              help_text="Job arguments/data (keep small)")

    # Current status
    status = models.CharField(
        max_length=16,
        choices=[
            ('pending', 'Pending'),
            ('running', 'Running'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('canceled', 'Canceled'),
            ('expired', 'Expired')
        ],
        default='pending',
        help_text="Current job status"
    )

    # Scheduling & timing
    run_at = models.DateTimeField(null=True, blank=True,
                                 help_text="When to run this job (null = immediate)")
    expires_at = models.DateTimeField(null=True, blank=True,
                                      help_text="Job expires if not run by this time")

    # Retry configuration
    attempt = models.IntegerField(default=0,
                                 help_text="Current attempt number")
    max_retries = models.IntegerField(default=0,
                                      help_text="Maximum retry attempts")
    backoff_base = models.FloatField(default=2.0,
                                     help_text="Base for exponential backoff")
    backoff_max_sec = models.IntegerField(default=3600,
                                          help_text="Maximum backoff in seconds")

    # Behavior flags
    broadcast = models.BooleanField(default=False,
                                   help_text="If true, all runners execute this job")
    cancel_requested = models.BooleanField(default=False,
                                          help_text="Cooperative cancel flag")
    max_exec_seconds = models.IntegerField(null=True, blank=True,
                                           help_text="Hard execution time limit")

    # Runner tracking
    runner_id = models.CharField(max_length=64, null=True, blank=True,
                                help_text="ID of runner currently executing")

    # Error diagnostics (latest only)
    last_error = models.TextField(blank=True, default="",
                                 help_text="Latest error message")
    stack_trace = models.TextField(blank=True, default="",
                                  help_text="Latest stack trace")

    # Additional metadata
    metadata = models.JSONField(default=dict, blank=True,
                               help_text="Custom metadata from job execution")

    # Timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True,
                                      help_text="When job execution started")
    finished_at = models.DateTimeField(null=True, blank=True,
                                       help_text="When job execution finished")

    # Idempotency support
    idempotency_key = models.CharField(max_length=64, null=True, blank=True,
                                       unique=True,
                                       help_text="Optional key for exactly-once semantics")

    class Meta:
        db_table = 'jobs_job'
        indexes = [
            models.Index(fields=['channel']),
            models.Index(fields=['func']),
            models.Index(fields=['status']),
            models.Index(fields=['run_at']),
            models.Index(fields=['expires_at']),
            models.Index(fields=['broadcast']),
            models.Index(fields=['runner_id']),
            models.Index(fields=['created']),
            models.Index(fields=['modified']),
            models.Index(fields=['channel', 'status']),
            models.Index(fields=['status', 'run_at']),
            models.Index(fields=['runner_id', 'status']),
        ]
        ordering = ['-created']

    class RestMeta:
        # Permissions - jobs system specific permissions
        VIEW_PERMS = ['view_jobs', 'manage_jobs']
        SAVE_PERMS = ['manage_jobs']
        DELETE_PERMS = ['manage_jobs']
        POST_SAVE_ACTIONS = ["cancel_request", "retry_request", "get_status", "publish_job"]

        # Graphs for different use cases
        GRAPHS = {
            'default': {
                'extra': ['duration_ms'],
                'fields': [
                    'id', 'channel', 'func', 'status',
                    'created', 'modified', 'attempt',
                    'started_at', 'finished_at', 'run_at'
                ]
            },
            'detail': {
                'extra': ['duration_ms'],
                'fields': [
                    'id', 'channel', 'func', 'payload', 'status',
                    'run_at', 'expires_at', 'attempt', 'max_retries',
                    'broadcast', 'cancel_requested', 'max_exec_seconds',
                    'runner_id', 'last_error', 'metadata'
                    'created', 'modified', 'started_at', 'finished_at'
                ]
            },
            'status': {
                'fields': [
                    'id', 'status', 'runner_id', 'attempt',
                    'started_at', 'finished_at', 'last_error'
                ]
            },
            'admin': {
                'fields': '__all__',
                'exclude': ['stack_trace']  # Stack traces can be large
            }
        }

    def __str__(self):
        return f"Job {self.id} ({self.func}@{self.channel}): {self.status}"

    @property
    def is_terminal(self) -> bool:
        """Check if job is in a terminal state."""
        return self.status in ('completed', 'failed', 'canceled', 'expired')

    @property
    def is_retriable(self) -> bool:
        """Check if job can be retried."""
        return self.status == 'failed' and self.attempt < self.max_retries

    @property
    def duration_ms(self) -> int:
        """Calculate job execution duration in milliseconds."""
        if self.started_at and self.finished_at:
            delta = self.finished_at - self.started_at
            return int(delta.total_seconds() * 1000)
        return 0

    @property
    def is_expired(self) -> bool:
        """Check if job has expired."""
        return self.expires_at and dates.utcnow() > self.expires_at

    def check_cancel_requested(self) -> bool:
        """
        Sync the cancel_requested field from the database and return updated value.

        This method refreshes the cancel_requested field from the database to get
        the most current cancellation status, useful for long-running jobs that
        need to check for cancellation requests during execution.

        Returns:
            bool: Current cancel_requested value from database
        """
        self.refresh_from_db(fields=['cancel_requested'])
        return self.cancel_requested

    def on_action_cancel_request(self, value):
        """
        Cancel this job via REST API action.

        Args:
            value: Boolean indicating if cancellation is requested

        Returns:
            dict: Response indicating success/failure
        """
        if not value:
            return {'status': False, 'error': 'cancel_request must be true'}

        from mojo.apps.jobs.services import JobActionsService
        return JobActionsService.cancel_job(self)

    def on_action_retry_request(self, value):
        """
        Retry this failed/cancelled job via REST API action.

        Args:
            value: Can be boolean True or dict with 'delay' key for delayed retry

        Returns:
            dict: Response indicating success/failure with new job_id
        """
        # Parse value - can be boolean or dict with delay
        delay = None
        if isinstance(value, dict):
            if not value.get('retry'):
                return {'status': False, 'error': 'retry_request must be true or {retry: true, delay: N}'}
            delay = value.get('delay')
        elif not value:
            return {'status': False, 'error': 'retry_request must be true or {retry: true, delay: N}'}

        from mojo.apps.jobs.services import JobActionsService
        return JobActionsService.retry_job(self, delay=delay)

    def on_action_get_status(self, value):
        """
        Get detailed status of this job via REST API action.

        Args:
            value: Boolean (should be true)

        Returns:
            dict: Detailed job status information
        """
        if not value:
            return {'status': False, 'error': 'get_status must be true'}

        from mojo.apps.jobs.services import JobActionsService
        return JobActionsService.get_job_status(self)

    def on_action_publish_job(self, value):
        """
        Publish a new job using this job as a template via REST API action.

        Args:
            value: Dict with optional overrides for the new job:
                - func: Override function path
                - payload: Override payload
                - channel: Override channel
                - delay: Delay in seconds
                - run_at: Specific run time
                - max_retries: Override max retries
                - broadcast: Override broadcast flag

        Returns:
            dict: Response with new job ID
        """
        if not isinstance(value, dict):
            return {'status': False, 'error': 'publish_job must be a dict with job parameters'}

        from mojo.apps.jobs.services import JobActionsService
        return JobActionsService.publish_job_from_template(self, value)

    def add_log(self, message: str, kind: str = 'info', meta: Optional[dict] = None):
        """
        Append a log entry for this job.

        Args:
            message: Log message text
            kind: One of 'debug','info','warn','error' (default: 'info')
            meta: Optional small dict for structured context
        """
        # Normalize kind to known values
        kind_norm = (kind or 'info').lower()
        if kind_norm not in ('debug', 'info', 'warn', 'error'):
            kind_norm = 'info'

        # Persist log entry
        JobLog.objects.create(
            job=self,
            channel=self.channel,
            kind=kind_norm,
            message=str(message),
            meta=meta or {}
        )

        # Touch modified for easier tracking
        self.save(update_fields=['modified'])

        return True


class JobEvent(models.Model, MojoModel):
    """
    Append-only audit log for job state transitions and events.
    Kept minimal for efficient storage and querying.
    """

    # Link to parent job
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='events')

    # Denormalized for efficient queries
    channel = models.CharField(max_length=100, db_index=True)

    # Event type
    event = models.CharField(
        max_length=24,
        db_index=True,
        choices=[
            ('created', 'Created'),
            ('queued', 'Queued'),
            ('scheduled', 'Scheduled'),
            ('running', 'Running'),
            ('retry', 'Retry'),
            ('canceled', 'Canceled'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('expired', 'Expired'),
            ('claimed', 'Claimed'),
            ('released', 'Released')
        ],
        help_text="Event type"
    )

    # When it happened
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Who/what triggered it
    runner_id = models.CharField(max_length=64, null=True, blank=True, db_index=True,
                                help_text="Runner that generated this event")

    # Context
    attempt = models.IntegerField(default=0,
                                 help_text="Attempt number at time of event")

    # Small details only - avoid large payloads
    details = models.JSONField(default=dict, blank=True,
                              help_text="Event-specific details (keep minimal)")

    # Standard timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = 'jobs_jobevent'
        indexes = [
            models.Index(fields=['job', '-at']),
            models.Index(fields=['channel', 'event', '-at']),
            models.Index(fields=['runner_id', '-at']),
            models.Index(fields=['-at']),  # For retention queries
        ]
        ordering = ['-at']

    class RestMeta:
        # Permissions - restricted to system users only
        VIEW_PERMS = ['manage_jobs', 'view_jobs']
        SAVE_PERMS = []  # Events are system-created only
        DELETE_PERMS = ['manage_jobs']

        # Graphs
        GRAPHS = {
            'default': {
                'fields': [
                    'id', 'event', 'at', 'runner_id', 'attempt', 'details'
                ]
            },
            'detail': {
                'fields': [
                    'id', 'job_id', 'channel', 'event', 'at',
                    'runner_id', 'attempt', 'details'
                ]
            },
            'timeline': {
                'fields': [
                    'event', 'at', 'runner_id', 'details'
                ]
            }
        }

    def __str__(self):
        return f"JobEvent {self.event} for {self.job_id} at {self.at}"


class JobLog(models.Model, MojoModel):
    """
    Append-only log entries for individual jobs with optional structured context.
    Useful for partial failures (e.g., per-recipient send outcomes).
    """

    # Link to parent job
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='logs')

    # Denormalized channel for efficient filtering
    channel = models.CharField(max_length=100, db_index=True)

    # When it happened
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    # Log kind/severity
    kind = models.CharField(
        max_length=16,
        db_index=True,
        choices=[
            ('debug', 'Debug'),
            ('info', 'Info'),
            ('warn', 'Warn'),
            ('error', 'Error'),
        ],
        default='info',
        help_text="Log level/kind"
    )

    # Message content
    message = models.TextField(help_text="Log message")

    # Optional structured metadata (keep small)
    meta = models.JSONField(default=dict, blank=True, help_text="Optional structured context")

    # Standard timestamps
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = 'jobs_joblog'
        ordering = ['-created']
        indexes = [
            models.Index(fields=['job', '-created']),
            models.Index(fields=['channel', 'kind', '-created']),
            models.Index(fields=['-created']),
        ]

    class RestMeta:
        VIEW_PERMS = ['manage_jobs', 'view_jobs']
        SAVE_PERMS = []  # Logs should be written via add_log / system actions
        DELETE_PERMS = ['manage_jobs']
        GRAPHS = {
            'default': {
                'fields': ['id', 'job_id', 'created', 'kind', 'message']
            },
            'detail': {
                'fields': ['id', 'job_id', 'channel', 'created', 'kind', 'message', 'meta']
            }
        }

    def __str__(self):
        return f"JobLog {self.kind} for {self.job_id} at {self.created}"
