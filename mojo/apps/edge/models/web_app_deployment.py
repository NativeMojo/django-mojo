from django.db import models

from mojo.models import MojoModel


STATUS_QUEUED = "queued"
STATUS_DEPLOYING = "deploying"
STATUS_LIVE = "live"
STATUS_ROLLING_BACK = "rolling_back"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_DEPLOYING, STATUS_ROLLING_BACK)
TERMINAL_STATUSES = (
    STATUS_LIVE, STATUS_ROLLED_BACK, STATUS_FAILED, STATUS_SUPERSEDED)


class WebAppDeployment(models.Model, MojoModel):
    """One fleet convergence attempt for an immutable WebApp release."""

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    started = models.DateTimeField(null=True, blank=True, default=None)
    finished = models.DateTimeField(null=True, blank=True, default=None)

    webapp = models.ForeignKey(
        "edge.WebApp", related_name="deployments", on_delete=models.CASCADE)
    release = models.ForeignKey(
        "edge.WebAppRelease", related_name="deployments", on_delete=models.CASCADE)
    previous_release = models.ForeignKey(
        "edge.WebAppRelease", related_name="rollback_deployments",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)

    release_previous_status = models.CharField(
        max_length=16, blank=True, default="",
        help_text="Target release status restored when deployment rolls back.")
    status = models.CharField(
        max_length=20,
        choices=[
            (STATUS_QUEUED, "Queued"),
            (STATUS_DEPLOYING, "Deploying"),
            (STATUS_LIVE, "Live on the active fleet"),
            (STATUS_ROLLING_BACK, "Rolling back"),
            (STATUS_ROLLED_BACK, "Rolled back"),
            (STATUS_FAILED, "Failed"),
            (STATUS_SUPERSEDED, "Superseded by a newer deployment"),
        ],
        default=STATUS_QUEUED, db_index=True)
    targets = models.JSONField(
        default=list, blank=True,
        help_text="Active-runner snapshot and direct job ids for deployment.")
    rollback_targets = models.JSONField(
        default=list, blank=True,
        help_text="Direct job ids used to restore the prior desired release.")
    detail = models.TextField(blank=True, default="")

    class Meta:
        db_table = "edge_web_app_deployment"
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["webapp", "status"]),
            models.Index(fields=["release", "status"]),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_DELETE = False
        GROUP_FIELD = "webapp__group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_webapp", "security"]
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "uuid", "webapp", "release",
            "previous_release", "release_previous_status", "status",
            "targets", "rollback_targets", "detail", "started", "finished",
        ]
        GRAPHS = {
            "basic": {
                "fields": ["id", "created", "status", "detail"],
                "graphs": {"release": "basic", "previous_release": "basic"},
            },
            "list": {
                "fields": ["id", "created", "started", "finished", "status"],
                "graphs": {"release": "basic", "previous_release": "basic"},
            },
            # Detail fallback for a wired URL: the union of basic + list, no
            # operational internals (targets / rollback_targets stay withheld).
            "default": {
                "fields": [
                    "id", "created", "modified", "started", "finished",
                    "status", "release_previous_status", "detail",
                ],
                "graphs": {"release": "basic", "previous_release": "basic"},
            },
        }

    @property
    def is_terminal(self):
        return self.status in TERMINAL_STATUSES

    def __str__(self):
        return f"{self.webapp_id}:{self.release_id} ({self.status})"
