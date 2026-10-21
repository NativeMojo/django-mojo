"""Independent ACME mutation evidence; no caller-owned foreign keys."""
import uuid

from django.db import models
from django.db.models import Q

from mojo import errors as me
from mojo.models import MojoModel


class DnsChallengeAttempt(models.Model, MojoModel):
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, editable=False, db_index=True)
    attempt_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    producer_epoch = models.PositiveIntegerField(default=1, editable=False)
    kind = models.CharField(max_length=32, editable=False)
    scope_digest = models.CharField(max_length=64, editable=False)
    owner_digest = models.CharField(max_length=64, editable=False)
    provider = models.CharField(max_length=32, editable=False)
    owner = models.JSONField(default=dict, editable=False)
    backend = models.JSONField(default=dict, editable=False)
    source_sha = models.CharField(max_length=40, default="", editable=False)
    manifest_sha256 = models.CharField(max_length=64, editable=False)
    request_sha256 = models.CharField(max_length=64, editable=False)
    request = models.JSONField(default=dict, editable=False)
    state = models.CharField(max_length=32, default="prepared", editable=False)
    version = models.PositiveIntegerField(default=1, editable=False)
    provider_change_id = models.CharField(max_length=256, default="", editable=False)
    provider_request_id = models.CharField(max_length=256, default="", editable=False)
    reason = models.CharField(max_length=64, default="", editable=False)
    receipt_sha256 = models.CharField(max_length=64, default="", editable=False)

    class Meta:
        db_table = "dnsman_challenge_attempt"
        ordering = ["pk"]
        indexes = [
            models.Index(fields=["scope_digest", "state"]),
            models.Index(fields=["owner_digest", "kind"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["kind", "owner_digest"],
                condition=Q(kind__in=["origin", "retirement_barrier"]),
                name="dnsman_challenge_terminal_owner_uniq"),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["security"]
        SAVE_PERMS = ["security"]
        DENY_AI = True
        SENSITIVE_FIELDS = ["owner", "backend", "request", "provider_change_id",
                            "provider_request_id", "scope_digest", "owner_digest"]
        GRAPHS = {}

    def save(self, *args, **kwargs):
        raise me.PermissionDeniedException("Challenge evidence requires its independent journal writer")

    def delete(self, *args, **kwargs):
        raise me.PermissionDeniedException("Challenge evidence is retained permanently")
