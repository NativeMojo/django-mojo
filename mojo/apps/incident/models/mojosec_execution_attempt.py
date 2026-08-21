from django.db import models

from mojo.models import MojoModel
from .mojosec_immutable import (
    MojoSecImmutableManager, assert_canonical_digest, canonical_digest)


class MojoSecExecutionAttempt(models.Model, MojoModel):
    """Append-only record of one enforcement try against one target."""

    OUTCOMES = (
        ("applied", "Applied"),
        ("pre_existing", "Pre-existing"),
        ("whitelisted", "Whitelisted"),
        ("failed", "Failed"),
        ("reverse_applied", "Reverse applied"),
        ("reverse_failed", "Reverse failed"),
        ("expire_confirmed", "Expire confirmed"),
    )

    objects = MojoSecImmutableManager()
    maintenance_objects = models.Manager()

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    recommendation = models.ForeignKey(
        "incident.MojoSecRecommendation", related_name="attempts",
        on_delete=models.PROTECT)
    target = models.ForeignKey(
        "incident.MojoSecRecommendationTarget", related_name="execution_attempts",
        on_delete=models.PROTECT)
    attempt_number = models.PositiveIntegerField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    outcome = models.CharField(max_length=20, choices=OUTCOMES, db_index=True)
    detail = models.CharField(max_length=256, blank=True, default="")
    row_digest = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ["created", "id"]
        indexes = [
            models.Index(
                fields=("recommendation", "created"), name="incident_msea_rec_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["row_digest"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "attempt_number", "started_at",
                    "finished_at", "outcome", "detail",
                ],
            },
        }

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("MojoSec execution attempts are append-only")
        self.row_digest = canonical_digest(self.integrity_payload())
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec execution attempts are append-only")

    def integrity_payload(self):
        return {
            "recommendation_id": self.recommendation_id,
            "target_id": self.target_id,
            "attempt_number": self.attempt_number,
            "outcome": self.outcome,
            "detail": self.detail,
        }

    def assert_integrity(self):
        assert_canonical_digest(
            self.integrity_payload(), self.row_digest,
            "MojoSec execution attempt")
        return self
