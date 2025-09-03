"""
Job and JobEvent models for the jobs system.
"""
from django.db import models
from mojo.models import MojoModel


class Job(models.Model, MojoModel):
    """
    Represents a background job in the system.
    Stores current state and metadata for job execution.
    """

    # Primary identifier - UUID without dashes
    id = models.CharField(primary_key=True, max_length=32, editable=False)

    # Job targeting
    channel = models.CharField(max_length=100, db_index=True,
                              help_text="Logical queue/channel name")
    func = models.CharField(max_length=255, db_index=True,
                           help_text="Registry key for the job function")
    payload = models.JSONField(default=dict, blank=True,
                              help_text="Job arguments/data (keep small)")

    # Current status
    status = models.CharField(
        max_length=16,
        db_index=True,
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
    run_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                 help_text="When to run this job (null = immediate)")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                      help_text="Job expires if not run by this time")

    # Retry configuration
    attempt = models.IntegerField(default=0,
                                 help_text="Current attempt number")
    max_retries = models.IntegerField(default=3,
                                      help_text="Maximum retry attempts")
    backoff_base = models.FloatField(default=2.0,
                                     help_text="Base for exponential backoff")
    backoff_max_sec = models.IntegerField(default=3600,
                                          help_text="Maximum backoff in seconds")

    # Behavior flags
    broadcast = models.BooleanField(default=False, db_index=True,
                                   help_text="If true, all runners execute this job")
    cancel_requested = models.BooleanField(default=False,
                                          help_text="Cooperative cancel flag")
    max_exec_seconds = models.IntegerField(null=True, blank=True,
                                           help_text="Hard execution time limit")

    # Runner tracking
    runner_id = models.CharField(max_length=64, null=True, blank=True, db_index=True,
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
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
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
            models.Index(fields=['channel', 'status']),
            models.Index(fields=['status', 'run_at']),
            models.Index(fields=['runner_id', 'status']),
        ]
        ordering = ['-created']

    class RestMeta:
        # Permissions - restricted to system users only by default
        VIEW_PERMS = ['system']
        SAVE_PERMS = ['system']
        DELETE_PERMS = ['system']

        # Graphs for different use cases
        GRAPHS = {
            'default': {
                'fields': [
                    'id', 'channel', 'func', 'status',
                    'created', 'modified', 'attempt'
                ]
            },
            'detail': {
                'fields': [
                    'id', 'channel', 'func', 'payload', 'status',
                    'run_at', 'expires_at', 'attempt', 'max_retries',
                    'broadcast', 'cancel_requested', 'max_exec_seconds',
                    'runner_id', 'last_error', 'metadata',
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
        VIEW_PERMS = ['system']
        SAVE_PERMS = []  # Events are system-created only
        DELETE_PERMS = ['system']

        # Graphs
        GRAPHS = {
            'default': {
                'fields': [
                    'id', 'event', 'at', 'runner_id', 'attempt'
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
