from django.db import models
from django.contrib.auth import get_user_model
from mojo.models import MojoModel
import json


class TaskLog(models.Model, MojoModel):
    """
    Comprehensive logging model for tracking task lifecycle events and state changes.

    This model tracks all task events including creation, state transitions,
    completion, errors, and cancellation for audit and monitoring purposes.
    """

    # Task identification
    task_id = models.CharField(max_length=32, db_index=True, help_text="Task UUID without dashes")
    task_function = models.CharField(max_length=255, db_index=True, help_text="Full function path (module.function)")
    task_channel = models.CharField(max_length=100, db_index=True, default="default", help_text="Task channel name")

    # Event tracking
    EVENT_TYPES = [
        ('created', 'Task Created'),
        ('status_change', 'Status Changed'),
        ('started', 'Task Started'),
        ('completed', 'Task Completed'),
        ('error', 'Task Error'),
        ('cancelled', 'Task Cancelled'),
        ('expired', 'Task Expired'),
        ('retry', 'Task Retry'),
    ]

    event_type = models.CharField(max_length=20, choices=EVENT_TYPES, db_index=True, help_text="Type of event being logged")

    # Status tracking
    TASK_STATUSES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('error', 'Error'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
    ]

    status = models.CharField(max_length=20, choices=TASK_STATUSES, db_index=True, help_text="Current task status")
    previous_status = models.CharField(max_length=20, choices=TASK_STATUSES, null=True, blank=True, help_text="Previous task status")

    # Task execution details
    runner_hostname = models.CharField(max_length=255, null=True, blank=True, db_index=True, help_text="Hostname of the runner executing the task")
    thread_id = models.BigIntegerField(null=True, blank=True, help_text="Thread ID executing the task")

    # Timing information
    task_created_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="When the task was originally created")
    task_started_at = models.DateTimeField(null=True, blank=True, help_text="When the task started executing")
    task_completed_at = models.DateTimeField(null=True, blank=True, help_text="When the task completed")
    duration_seconds = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, help_text="Task execution duration in seconds")

    # Error handling
    error_message = models.TextField(null=True, blank=True, help_text="Error message if task failed")
    error_traceback = models.TextField(null=True, blank=True, help_text="Full error traceback")

    # Task data (stored as JSON)
    task_data = models.JSONField(null=True, blank=True, help_text="Task data and parameters")
    result_data = models.JSONField(null=True, blank=True, help_text="Task result data")

    # Metadata
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="When the task expires")
    retry_count = models.IntegerField(default=0, help_text="Number of retry attempts")
    max_retries = models.IntegerField(default=0, help_text="Maximum retry attempts allowed")

    # Audit fields
    created = models.DateTimeField(auto_now_add=True, db_index=True, help_text="When this log entry was created")
    modified = models.DateTimeField(auto_now=True, help_text="When this log entry was last modified")
    user = models.ForeignKey("account.User", on_delete=models.SET_NULL, null=True, blank=True, help_text="User who triggered the task")

    class Meta:
        ordering = ['-created']
        indexes = [
            models.Index(fields=['task_id', '-created']),
            models.Index(fields=['task_function', '-created']),
            models.Index(fields=['task_channel', 'status']),
            models.Index(fields=['event_type', '-created']),
            models.Index(fields=['status', '-created']),
            models.Index(fields=['runner_hostname', '-created']),
        ]

    class RestMeta:
        VIEW_PERMS = ["system", "admin", "view_task_logs"]
        SAVE_PERMS = ["system"]  # Only system can create task logs
        DELETE_PERMS = ["admin"]  # Only admins can delete task logs

        GRAPHS = {
            "basic": {
                "fields": [
                    "id", "task_id", "event_type", "status", "previous_status",
                    "created", "task_function", "task_channel"
                ]
            },
            "detailed": {
                "fields": [
                    "id", "task_id", "task_function", "task_channel", "event_type",
                    "status", "previous_status", "runner_hostname", "thread_id",
                    "task_created_at", "task_started_at", "task_completed_at",
                    "duration_seconds", "error_message", "expires_at",
                    "retry_count", "max_retries", "created", "modified"
                ],
                "related": {
                    "user": {
                        "fields": ["id", "username", "email"]
                    }
                }
            },
            "with_data": {
                "fields": [
                    "id", "task_id", "task_function", "task_channel", "event_type",
                    "status", "previous_status", "task_data", "result_data",
                    "error_message", "error_traceback", "duration_seconds",
                    "created", "runner_hostname"
                ]
            },
            "errors": {
                "fields": [
                    "id", "task_id", "task_function", "task_channel",
                    "error_message", "error_traceback", "created",
                    "runner_hostname", "retry_count"
                ]
            },
            "timeline": {
                "fields": [
                    "id", "task_id", "event_type", "status", "previous_status",
                    "created", "duration_seconds", "error_message"
                ]
            }
        }

    def __str__(self):
        return f"TaskLog({self.task_id}, {self.event_type}, {self.status})"

    def save(self, *args, **kwargs):
        """Override save to ensure proper data handling."""
        # Ensure task_data is properly serialized
        if self.task_data and isinstance(self.task_data, str):
            try:
                self.task_data = json.loads(self.task_data)
            except (json.JSONDecodeError, TypeError):
                pass

        if self.result_data and isinstance(self.result_data, str):
            try:
                self.result_data = json.loads(self.result_data)
            except (json.JSONDecodeError, TypeError):
                pass

        super().save(*args, **kwargs)

    @classmethod
    def log_task_created(cls, task_data, user=None):
        """Log task creation event."""
        from django.utils import timezone

        return cls.objects.create(
            task_id=task_data.get('id'),
            task_function=task_data.get('function'),
            task_channel=task_data.get('channel', 'default'),
            event_type='created',
            status='pending',
            task_data=task_data.get('data'),
            task_created_at=timezone.datetime.fromtimestamp(task_data.get('created', 0), tz=timezone.utc) if task_data.get('created') else None,
            expires_at=timezone.datetime.fromtimestamp(task_data.get('expires', 0), tz=timezone.utc) if task_data.get('expires') else None,
            user=user
        )

    @classmethod
    def log_status_change(cls, task_data, new_status, previous_status=None, runner_hostname=None, error_message=None):
        """Log task status change."""
        from django.utils import timezone

        log_data = {
            'task_id': task_data.get('id'),
            'task_function': task_data.get('function'),
            'task_channel': task_data.get('channel', 'default'),
            'event_type': 'status_change',
            'status': new_status,
            'previous_status': previous_status,
            'runner_hostname': runner_hostname,
            'error_message': error_message
        }

        # Add timing data if available
        if task_data.get('started_at'):
            log_data['task_started_at'] = timezone.datetime.fromtimestamp(task_data.get('started_at'), tz=timezone.utc)
        if task_data.get('completed_at'):
            log_data['task_completed_at'] = timezone.datetime.fromtimestamp(task_data.get('completed_at'), tz=timezone.utc)
        if task_data.get('elapsed_time'):
            log_data['duration_seconds'] = task_data.get('elapsed_time')
        if task_data.get('_thread_id'):
            log_data['thread_id'] = task_data.get('_thread_id')

        return cls.objects.create(**log_data)

    @classmethod
    def log_task_error(cls, task_data, error_message, error_traceback=None, runner_hostname=None):
        """Log task error event."""
        return cls.log_status_change(
            task_data=task_data,
            new_status='error',
            previous_status=task_data.get('status'),
            runner_hostname=runner_hostname,
            error_message=error_message
        ).update(
            event_type='error',
            error_traceback=error_traceback
        )

    @classmethod
    def get_task_timeline(cls, task_id):
        """Get chronological timeline of all events for a task."""
        return cls.objects.filter(task_id=task_id).order_by('created')

    @classmethod
    def get_failed_tasks(cls, hours=24):
        """Get tasks that failed in the last N hours."""
        from django.utils import timezone
        from datetime import timedelta

        cutoff = timezone.now() - timedelta(hours=hours)
        return cls.objects.filter(
            event_type='error',
            created__gte=cutoff
        ).order_by('-created')

    @classmethod
    def get_channel_stats(cls, channel, hours=24):
        """Get statistics for a specific channel."""
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models import Count, Avg

        cutoff = timezone.now() - timedelta(hours=hours)
        stats = cls.objects.filter(
            task_channel=channel,
            created__gte=cutoff
        ).aggregate(
            total_tasks=Count('task_id', distinct=True),
            total_events=Count('id'),
            avg_duration=Avg('duration_seconds')
        )

        # Get status breakdown
        status_counts = cls.objects.filter(
            task_channel=channel,
            created__gte=cutoff,
            event_type='status_change'
        ).values('status').annotate(count=Count('id'))

        stats['status_breakdown'] = {item['status']: item['count'] for item in status_counts}
        return stats
