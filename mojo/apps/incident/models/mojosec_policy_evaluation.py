from django.db import models, router

from mojo.models import MojoModel
from .mojosec_immutable import (
    MojoSecImmutableManager, assert_canonical_digest, canonical_digest)


class MojoSecPolicyEvaluation(models.Model, MojoModel):
    """Bounded digest and aggregate metrics from an explicit offline run."""

    REPLAY = "replay"
    SHADOW = "shadow"
    MODES = ((REPLAY, "Replay"), (SHADOW, "Shadow"))

    objects = MojoSecImmutableManager()

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    proposal = models.ForeignKey(
        "incident.MojoSecPolicyProposal",
        related_name="evaluations", on_delete=models.PROTECT)
    created_by = models.ForeignKey(
        "account.User", related_name="+", on_delete=models.PROTECT)
    mode = models.CharField(max_length=16, choices=MODES, db_index=True)
    sample_count = models.PositiveSmallIntegerField(default=0)
    sample_digest = models.CharField(max_length=64)
    result_digest = models.CharField(max_length=64)
    metrics = models.JSONField(default=dict)
    row_digest = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "mode", "sample_count", "sample_digest",
                    "result_digest", "metrics",
                ],
                "graphs": {"proposal": "reference"},
            },
        }

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("MojoSec policy evaluations are immutable")
        self.row_digest = canonical_digest(self.integrity_payload())
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec policy evaluations are immutable")

    def integrity_payload(self):
        return {
            "proposal_id": self.proposal_id,
            "created_by_id": self.created_by_id,
            "mode": self.mode,
            "sample_count": self.sample_count,
            "sample_digest": self.sample_digest,
            "result_digest": self.result_digest,
            "metrics": self.metrics,
        }

    def assert_integrity(self):
        assert_canonical_digest(
            self.integrity_payload(), self.row_digest, "MojoSec policy evaluation")
        return self


def _prune_policy_evaluations_before(cutoff):
    """Retention-only deletion path; normal model/queryset deletion is sealed."""
    using = router.db_for_write(MojoSecPolicyEvaluation)
    queryset = models.QuerySet(model=MojoSecPolicyEvaluation, using=using)
    return queryset.filter(created__lt=cutoff).delete()
