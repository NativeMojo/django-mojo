from django.db import models

from mojo.models import MojoModel


class MojoSecPolicyEvaluation(models.Model, MojoModel):
    """Bounded digest and aggregate metrics from an explicit offline run."""

    REPLAY = "replay"
    SHADOW = "shadow"
    MODES = ((REPLAY, "Replay"), (SHADOW, "Shadow"))

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

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "mode", "sample_count", "sample_digest",
                    "result_digest", "metrics",
                ],
                "graphs": {"proposal": "reference"},
            },
        }
