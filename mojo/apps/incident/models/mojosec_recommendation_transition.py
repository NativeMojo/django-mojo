from django.db import models

from mojo.models import MojoModel
from .mojosec_immutable import (
    MojoSecImmutableManager, assert_canonical_digest, canonical_digest)


class MojoSecRecommendationTransition(models.Model, MojoModel):
    """Append-only audit snapshot of one recommendation state change."""

    objects = MojoSecImmutableManager()
    maintenance_objects = models.Manager()

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    recommendation = models.ForeignKey(
        "incident.MojoSecRecommendation", related_name="transitions",
        on_delete=models.PROTECT)
    transition = models.CharField(max_length=32, db_index=True)
    reason = models.CharField(max_length=96)
    from_state = models.CharField(max_length=16, blank=True, default="")
    to_state = models.CharField(max_length=16)
    # The approver/rejecter/reverser; null for system transitions.
    actor = models.ForeignKey(
        "account.User", null=True, blank=True, default=None,
        related_name="+", on_delete=models.SET_NULL)
    actor_id_snapshot = models.PositiveBigIntegerField(default=0)
    target_count = models.PositiveIntegerField()
    validated_count = models.PositiveIntegerField()
    protected_count = models.PositiveIntegerField()
    executed_count = models.PositiveIntegerField()
    failed_count = models.PositiveIntegerField()
    reversed_count = models.PositiveIntegerField()
    row_digest = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ["created", "id"]
        indexes = [
            models.Index(
                fields=("recommendation", "created"), name="incident_msrx_rec_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["row_digest", "actor_id_snapshot"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "transition", "reason", "from_state",
                    "to_state", "target_count", "validated_count",
                    "protected_count", "executed_count", "failed_count",
                    "reversed_count",
                ],
            },
        }

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("MojoSec recommendation transitions are append-only")
        self.row_digest = canonical_digest(self.integrity_payload())
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec recommendation transitions are append-only")

    def integrity_payload(self):
        return {
            "recommendation_id": self.recommendation_id,
            "transition": self.transition,
            "reason": self.reason,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "actor_id_snapshot": self.actor_id_snapshot,
            "target_count": self.target_count,
            "validated_count": self.validated_count,
            "protected_count": self.protected_count,
            "executed_count": self.executed_count,
            "failed_count": self.failed_count,
            "reversed_count": self.reversed_count,
        }

    def assert_integrity(self):
        assert_canonical_digest(
            self.integrity_payload(), self.row_digest,
            "MojoSec recommendation transition")
        return self
