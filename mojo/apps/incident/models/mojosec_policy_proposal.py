import uuid

from django.db import models

from mojo.models import MojoModel


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
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec policy proposal revisions are immutable")
