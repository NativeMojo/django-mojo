import uuid

from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


STATUS_ACTIVE = "active"
STATUS_WAITING = "waiting"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)


class WebAppOnboardingOperation(models.Model, MojoModel):
    """Durable, secret-free intent and evidence for WebApp onboarding.

    This is deliberately not a second deployment authority. ``WebApp``,
    ``Domain``, ``Certificate`` and ``Vhost`` remain authoritative for the
    resources they own; this row records how to reconcile those resources
    after a provider timeout or worker crash.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    finished = models.DateTimeField(null=True, blank=True, default=None)

    operation_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    group = models.ForeignKey(
        "account.Group", related_name="webapp_onboarding_operations",
        on_delete=models.PROTECT)
    actor = models.ForeignKey(
        "account.User", related_name="webapp_onboarding_operations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)

    web_app = models.ForeignKey(
        "edge.WebApp", related_name="onboarding_operations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)
    domain = models.ForeignKey(
        "dnsman.Domain", related_name="webapp_onboarding_operations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)
    certificate = models.ForeignKey(
        "dnsman.Certificate", related_name="webapp_onboarding_operations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)
    vhost = models.ForeignKey(
        "edge.Vhost", related_name="webapp_onboarding_operations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)

    status = models.CharField(
        max_length=16,
        choices=[
            (STATUS_ACTIVE, "Active"),
            (STATUS_WAITING, "Waiting for a choice or provider"),
            (STATUS_SUCCEEDED, "Succeeded"),
            (STATUS_FAILED, "Failed"),
            (STATUS_CANCELLED, "Cancelled"),
        ], default=STATUS_ACTIVE, db_index=True)
    cursor = models.CharField(max_length=24, default="app", db_index=True)
    revision = models.PositiveIntegerField(default=0)
    origin = models.CharField(max_length=255)
    replay_fingerprint = models.CharField(max_length=64)

    registrar_provider = models.CharField(max_length=32, blank=True, default="")
    dns_provider = models.CharField(max_length=32, blank=True, default="")
    state = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    activity = models.JSONField(default=list, blank=True)

    lease_owner = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, default=None)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, default=None)
    last_error = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "edge_web_app_onboarding_operation"
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "replay_fingerprint"],
                condition=Q(status__in=(STATUS_ACTIVE, STATUS_WAITING)),
                name="edge_webapp_onboarding_live_replay_uniq"),
        ]
        indexes = [
            models.Index(fields=["group", "status", "modified"]),
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["web_app", "status"]),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        GROUP_FIELD = "group"
        VIEW_PERMS = ["manage_webapp", "security"]
        SAVE_PERMS = ["manage_webapp", "security"]
        NO_SHOW_FIELDS = ["state"]
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "modified", "finished", "operation_id",
            "group", "actor", "web_app", "domain", "certificate", "vhost",
            "status", "cursor", "revision", "origin", "replay_fingerprint",
            "registrar_provider", "dns_provider", "state", "evidence",
            "activity", "lease_owner", "lease_expires_at", "attempts",
            "next_attempt_at", "last_error",
        ]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id", "operation_id", "status", "cursor", "revision",
                    "created", "modified", "finished", "evidence", "activity",
                    "registrar_provider", "dns_provider", "attempts",
                    "next_attempt_at", "last_error",
                ],
                "graphs": {
                    "web_app": "basic", "domain": "basic",
                    "certificate": "basic", "vhost": "basic",
                },
            },
        }

    @property
    def is_terminal(self):
        return self.status in TERMINAL_STATUSES

    def __str__(self):
        return f"{self.operation_id} ({self.status}:{self.cursor})"
