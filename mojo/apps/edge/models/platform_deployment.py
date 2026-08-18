import uuid

from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


class PlatformDeployment(models.Model, MojoModel):
    """Durable, UUID-addressed evidence for one platform deploy attempt."""

    STATUS_REQUESTED = "requested"
    STATUS_CANARY = "canary"
    STATUS_FLEET = "fleet"
    STATUS_VERIFIED = "verified"
    STATUS_CONVERGED = "converged"
    STATUS_PARTIAL = "partial"
    STATUS_UNKNOWN = "unknown"
    STATUS_FAILED = "failed"
    STATUS_SUPERSEDED = "superseded"
    ACTIVE_STATUSES = (STATUS_REQUESTED, STATUS_CANARY, STATUS_FLEET)
    TERMINAL_STATUSES = (
        STATUS_VERIFIED, STATUS_CONVERGED, STATUS_PARTIAL, STATUS_UNKNOWN,
        STATUS_FAILED, STATUS_SUPERSEDED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    started = models.DateTimeField(null=True, blank=True, default=None)
    finished = models.DateTimeField(null=True, blank=True, default=None)
    sha = models.CharField(max_length=40, db_index=True)
    framework_version = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, default=STATUS_REQUESTED, db_index=True)
    source = models.CharField(max_length=32, default="external", db_index=True)
    actor = models.CharField(max_length=160, blank=True, default="")
    created_by = models.ForeignKey(
        "account.User", null=True, blank=True, default=None,
        on_delete=models.SET_NULL, related_name="platform_deployments")
    request_key = models.CharField(max_length=128, unique=True)
    source_delivery = models.CharField(max_length=128, blank=True, default="", db_index=True)
    retry_of = models.ForeignKey(
        "self", null=True, blank=True, default=None, on_delete=models.SET_NULL,
        related_name="retry_attempts")
    frozen_roster = models.JSONField(default=list, blank=True)
    transitions = models.JSONField(default=list, blank=True)
    node_evidence = models.JSONField(default=list, blank=True)
    links = models.JSONField(default=dict, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "edge_platform_deployment"
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["sha", "-created"]),
            models.Index(fields=["status", "-created"]),
            models.Index(fields=["source", "source_delivery"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_delivery"],
                condition=Q(source="github") & ~Q(source_delivery=""),
                name="edge_platform_unique_github_delivery"),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["view_platform", "manage_platform", "admin"]
        SAVE_PERMS = ["manage_platform", "admin"]
        NO_SAVE_FIELDS = [
            "id", "created", "modified", "started", "finished", "sha",
            "framework_version", "status", "source", "actor", "created_by",
            "request_key", "source_delivery", "retry_of", "frozen_roster",
            "transitions", "node_evidence", "links", "detail",
        ]
        SEARCH_FIELDS = ["sha", "actor", "request_key", "source_delivery"]
        # node_evidence (which carries the privileged deploy stderr tail) is
        # served ONLY by the `admin` graph, which GRAPH_PERMISSIONS gates on
        # manage_platform/admin (item 2102). `default`/`basic` are evidence
        # -free, and an unmapped graph name resolves to `default`, so no
        # ungated serialization path can hand out the tail. The privileged
        # platform service (which has a request to check) is the other reader.
        GRAPH_PERMISSIONS = {"admin": ["manage_platform", "admin"]}
        GRAPHS = {
            "basic": {
                "fields": [
                    "id", "created", "modified", "sha", "framework_version",
                    "status", "source", "actor", "started", "finished",
                ],
            },
            # Detail fallback for a wired URL. Evidence-free, like `basic` —
            # node_evidence is served only by the permission-gated `admin`
            # graph.
            "default": {
                "fields": [
                    "id", "created", "modified", "sha", "framework_version",
                    "status", "source", "actor", "started", "finished",
                ],
            },
            "admin": {
                # Gated by GRAPH_PERMISSIONS (manage_platform/admin), so this
                # graph carries the RAW node_evidence, stderr tail included.
                # The framework refuses the admin graph at the REST boundary and
                # the assistant tools to a caller without the perm (item 2102),
                # which is why the field no longer needs the stripped alias that
                # denied it to superusers too.
                "fields": [
                    "id", "created", "modified", "sha", "framework_version",
                    "status", "source", "actor", "request_key",
                    "source_delivery", "frozen_roster", "transitions",
                    "node_evidence", "links", "detail", "started", "finished",
                ],
                "graphs": {"retry_of": "basic"},
            },
        }

    def __str__(self):
        return f"{self.id}:{self.sha} ({self.status})"
