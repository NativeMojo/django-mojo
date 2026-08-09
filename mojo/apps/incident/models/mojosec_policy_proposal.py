import uuid

from django.db import models

from mojo.models import MojoModel
from .mojosec_immutable import (
    MojoSecImmutableManager, assert_canonical_digest, canonical_digest)


class MojoSecPolicyProposal(models.Model, MojoModel):
    """Immutable, non-executable revision of an offline detector proposal."""

    DRAFT = "draft"
    SHADOW = "shadow"
    REJECTED = "rejected"
    STATUSES = (
        (DRAFT, "Draft"),
        (SHADOW, "Shadow"),
        (REJECTED, "Rejected"),
    )

    objects = MojoSecImmutableManager()
    # Explicit exception for migrations/database administration only.
    maintenance_objects = models.Manager()

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    created_by = models.ForeignKey(
        "account.User", related_name="+", on_delete=models.PROTECT)
    lineage_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    revision = models.PositiveIntegerField(default=1)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, default=None,
        related_name="superseded_by", on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=STATUSES, default=DRAFT, db_index=True)
    summary = models.CharField(max_length=500, blank=True, default="")
    content = models.JSONField(default=dict)
    content_digest = models.CharField(max_length=64)
    row_digest = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=("lineage_id", "revision"),
                name="incident_msp_lineage_revision_uniq"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "lineage_id", "revision", "status",
                    "summary", "content", "content_digest",
                ],
            },
        }

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("MojoSec policy proposal revisions are immutable")
        self.row_digest = canonical_digest(self.integrity_payload())
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec policy proposal revisions are immutable")

    def integrity_payload(self):
        return {
            "created_by_id": self.created_by_id,
            "lineage_id": str(self.lineage_id),
            "revision": self.revision,
            "supersedes_id": self.supersedes_id,
            "status": self.status,
            "summary": self.summary,
            "content": self.content,
            "content_digest": self.content_digest,
        }

    def assert_integrity(self):
        assert_canonical_digest(
            self.integrity_payload(), self.row_digest, "MojoSec policy proposal")
        return self
