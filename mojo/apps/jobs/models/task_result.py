"""
TaskResult model — stores the output of scheduled task executions.
"""
import uuid
from django.db import models
from mojo.models import MojoModel


class TaskResult(models.Model, MojoModel):
    """
    Stores the output of a scheduled task execution.
    Owner-scoped and read-only via REST.
    """

    id = models.CharField(primary_key=True, max_length=32, editable=False)

    task = models.ForeignKey(
        "jobs.ScheduledTask", on_delete=models.CASCADE,
        related_name="results",
        help_text="The scheduled task that produced this result"
    )

    user = models.ForeignKey(
        "account.User", on_delete=models.CASCADE,
        related_name="task_results",
        help_text="User who owns this result (denormalized from task)"
    )

    job = models.ForeignKey(
        "jobs.Job", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="task_results",
        help_text="The job that produced this result"
    )

    status = models.CharField(
        max_length=16,
        choices=[
            ('success', 'Success'),
            ('error', 'Error'),
        ],
        help_text="Execution result status"
    )

    output = models.TextField(blank=True, default="",
                              help_text="Result content (LLM response, webhook status, etc.)")
    error = models.TextField(blank=True, default="",
                             help_text="Error message if failed")

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        db_table = "jobs_taskresult"
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["task", "-created"]),
            models.Index(fields=["user", "-created"]),
        ]

    class RestMeta:
        OWNER_FIELD = "user"
        VIEW_PERMS = ["jobs", "view_scheduled_tasks", "owner"]
        SAVE_PERMS = []  # read-only
        DELETE_PERMS = ["jobs", "manage_scheduled_tasks"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "task_id", "job_id", "status",
                    "output", "error", "created",
                ]
            },
            "list": {
                "fields": [
                    "id", "task_id", "status", "created",
                ]
            },
        }

    def __str__(self):
        return f"TaskResult {self.id} ({self.status}) for task {self.task_id}"

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = uuid.uuid4().hex
        super().save(*args, **kwargs)
