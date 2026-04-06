"""
ScheduledTask model — user-defined recurring or one-off jobs.
"""
import uuid
from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit


TASK_TYPE_CHOICES = [
    ('job', 'Job'),
    ('webhook', 'Webhook'),
    ('llm', 'LLM'),
]


class ScheduledTask(models.Model, MojoModel):
    """
    A user-defined scheduled task that runs at specific times of day
    on specific days of the week.

    Schedule format:
        run_times: list of "HH:MM" strings, max 2 (e.g. ["09:00", "17:00"])
        run_days: list of weekday ints 0-6 (Mon=0), empty = every day

    Task types:
        job: publishes a job with func + payload
        webhook: publishes a webhook POST
        llm: runs an LLM prompt and stores result
    """

    id = models.CharField(primary_key=True, max_length=32, editable=False)

    user = models.ForeignKey(
        "account.User", on_delete=models.CASCADE,
        related_name="scheduled_tasks",
        help_text="User who owns this task"
    )

    name = models.CharField(max_length=255, help_text="Human-readable label")
    description = models.TextField(blank=True, default="",
                                   help_text="Optional description")

    enabled = models.BooleanField(default=True, db_index=True,
                                  help_text="Whether this task is active")
    run_once = models.BooleanField(default=False,
                                   help_text="Auto-disable after first execution")

    task_type = models.CharField(
        max_length=16,
        choices=TASK_TYPE_CHOICES,
        help_text="Type of task to execute"
    )

    # Schedule — simple times + days, not cron syntax
    run_times = models.JSONField(
        default=list,
        help_text='List of "HH:MM" strings, max 2 (e.g. ["09:00", "17:00"])'
    )
    run_days = models.JSONField(
        default=list,
        help_text="List of weekday ints 0-6 (Mon=0). Empty = every day (Mon-Sun)"
    )

    # Task configuration — varies by task_type
    job_config = models.JSONField(
        default=dict,
        help_text="Type-specific config: job={func, payload}, webhook={url, data}, llm={system_prompt, user_prompt}"
    )

    # Notification — opt-in per task
    notify = models.JSONField(
        default=list,
        help_text='Opt-in notification channels: ["email", "in_app", "sms", "push"]'
    )

    # Job execution settings
    channel = models.CharField(max_length=100, default="default",
                               help_text="Job channel for published jobs")
    max_retries = models.IntegerField(default=0,
                                      help_text="Max retries for published jobs")

    # Tracking
    last_run = models.DateTimeField(null=True, blank=True,
                                    help_text="When this task last ran")
    run_count = models.IntegerField(default=0,
                                    help_text="Total number of executions")
    last_error = models.TextField(blank=True, default="",
                                  help_text="Last execution error")

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = "jobs_scheduledtask"
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["enabled", "-created"]),
            models.Index(fields=["user", "-created"]),
        ]

    class RestMeta:
        OWNER_FIELD = "user"
        VIEW_PERMS = ["jobs", "view_scheduled_tasks", "owner"]
        SAVE_PERMS = ["jobs", "manage_scheduled_tasks", "owner"]
        DELETE_PERMS = ["jobs", "manage_scheduled_tasks", "owner"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "name", "description", "enabled", "run_once",
                    "task_type", "run_times", "run_days", "job_config",
                    "notify", "channel", "max_retries",
                    "last_run", "run_count", "last_error",
                    "created", "modified",
                ]
            },
            "list": {
                "fields": [
                    "id", "name", "enabled", "run_once", "task_type",
                    "run_times", "run_days", "last_run", "run_count",
                    "created",
                ]
            },
        }

    def __str__(self):
        return f"ScheduledTask {self.id} ({self.name}): {self.task_type}"

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = uuid.uuid4().hex
        self._validate()
        super().save(*args, **kwargs)

    def _validate(self):
        """Validate run_times and run_days format."""
        from mojo.helpers.settings import settings

        # Validate run_times
        if not isinstance(self.run_times, list):
            raise ValueError("run_times must be a list")
        if len(self.run_times) > 2:
            raise ValueError("run_times cannot have more than 2 entries")
        for t in self.run_times:
            if not isinstance(t, str) or len(t) != 5 or t[2] != ":":
                raise ValueError(f"Invalid time format: {t}. Use HH:MM")
            try:
                h, m = int(t[:2]), int(t[3:])
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid time value: {t}")

        # Validate run_days
        if not isinstance(self.run_days, list):
            raise ValueError("run_days must be a list")
        for d in self.run_days:
            if not isinstance(d, int) or not (0 <= d <= 6):
                raise ValueError(f"Invalid weekday: {d}. Must be 0-6 (Mon=0)")

        # Validate task_type
        valid_types = [c[0] for c in TASK_TYPE_CHOICES]
        if self.task_type not in valid_types:
            raise ValueError(f"Invalid task_type: {self.task_type}")

        # Validate notify channels
        valid_channels = ["email", "in_app", "sms", "push"]
        if not isinstance(self.notify, list):
            raise ValueError("notify must be a list")
        for ch in self.notify:
            if ch not in valid_channels:
                raise ValueError(f"Invalid notify channel: {ch}")

        # Validate max tasks per user
        max_per_user = settings.get("SCHEDULED_TASK_MAX_PER_USER", 10)
        if not self.pk:
            existing = ScheduledTask.objects.filter(user=self.user).count()
            if existing >= max_per_user:
                raise ValueError(f"Maximum of {max_per_user} scheduled tasks per user")

    def get_run_times_for_hour(self, hour):
        """
        Return list of (hour, minute) tuples that match the given hour.

        Args:
            hour: int, 0-23

        Returns:
            list of (hour, minute) tuples
        """
        matches = []
        for t in self.run_times:
            h, m = int(t[:2]), int(t[3:])
            if h == hour:
                matches.append((h, m))
        return matches

    def matches_day(self, weekday):
        """
        Check if this task runs on the given weekday.

        Args:
            weekday: int, 0-6 (Mon=0)

        Returns:
            bool
        """
        if not self.run_days:
            return True  # empty = every day
        return weekday in self.run_days
