from django.db import models

from mojo.models import MojoModel
from .mojosec_immutable import (
    MojoSecImmutableManager, assert_canonical_digest, canonical_digest)


class MojoSecCaseTransition(models.Model, MojoModel):
    """Append-only bounded snapshot of a shadow case transition."""

    objects = MojoSecImmutableManager()
    maintenance_objects = models.Manager()

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    case = models.ForeignKey(
        "incident.MojoSecCase", related_name="transitions", on_delete=models.PROTECT)
    receipt = models.ForeignKey(
        "incident.MojoSecReceipt", null=True, blank=True, default=None,
        related_name="case_transitions", on_delete=models.SET_NULL)
    receipt_id_snapshot = models.PositiveBigIntegerField()
    transition = models.CharField(max_length=32, db_index=True)
    reason = models.CharField(max_length=96)
    from_state = models.CharField(max_length=16, blank=True, default="")
    to_state = models.CharField(max_length=16)
    from_urgency = models.CharField(max_length=16, blank=True, default="")
    to_urgency = models.CharField(max_length=16)
    occurrence_count = models.PositiveBigIntegerField()
    receipt_count = models.PositiveBigIntegerField()
    projected_event_count = models.PositiveBigIntegerField()
    distinct_count = models.PositiveBigIntegerField()
    sample_count = models.PositiveSmallIntegerField()
    overflow_count = models.PositiveBigIntegerField()
    policy_version = models.PositiveIntegerField()
    evaluator_version = models.PositiveIntegerField()
    row_digest = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ["created", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("receipt_id_snapshot",), name="incident_msct_receipt_uniq"),
        ]
        indexes = [
            models.Index(fields=("case", "created"), name="incident_msct_case_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["row_digest", "receipt_id_snapshot"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "transition", "reason", "from_state",
                    "to_state", "from_urgency", "to_urgency",
                    "occurrence_count", "receipt_count", "projected_event_count",
                    "distinct_count", "sample_count", "overflow_count",
                    "policy_version", "evaluator_version",
                ],
                "graphs": {"case": "reference"},
            },
        }

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("MojoSec case transitions are append-only")
        self.row_digest = canonical_digest(self.integrity_payload())
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec case transitions are append-only")

    def integrity_payload(self):
        return {
            "case_id": self.case_id,
            "receipt_id_snapshot": self.receipt_id_snapshot,
            "transition": self.transition,
            "reason": self.reason,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "from_urgency": self.from_urgency,
            "to_urgency": self.to_urgency,
            "occurrence_count": self.occurrence_count,
            "receipt_count": self.receipt_count,
            "projected_event_count": self.projected_event_count,
            "distinct_count": self.distinct_count,
            "sample_count": self.sample_count,
            "overflow_count": self.overflow_count,
            "policy_version": self.policy_version,
            "evaluator_version": self.evaluator_version,
        }

    def assert_integrity(self):
        assert_canonical_digest(
            self.integrity_payload(), self.row_digest, "MojoSec case transition")
        return self
