from django.db import models

from mojo.models import MojoModel


class MojoSecRecommendationTarget(models.Model, MojoModel):
    """One canonical IP inside a recommendation, with its own outcome.

    Validation happens at proposal time and again at execution; a target
    that fails validation is recorded and skipped, never silently dropped.
    ``pre_existing`` means an active block already covered the IP — the
    requested TTL/reason were NOT applied, and the prior state is
    snapshotted so the distinction survives.
    """

    VALIDATION_VALIDATED = "validated"
    VALIDATION_PROTECTED = "protected"
    VALIDATION_INVALID = "invalid"
    VALIDATION_STATES = (
        (VALIDATION_VALIDATED, "Validated"),
        (VALIDATION_PROTECTED, "Protected"),
        (VALIDATION_INVALID, "Invalid"),
    )
    OUTCOME_PENDING = "pending"
    OUTCOME_APPLIED = "applied"
    OUTCOME_PRE_EXISTING = "pre_existing"
    OUTCOME_WHITELISTED = "whitelisted"
    OUTCOME_FAILED = "failed"
    OUTCOME_EXPIRED = "expired"
    OUTCOME_REVERSED = "reversed"
    OUTCOMES = (
        (OUTCOME_PENDING, "Pending"),
        (OUTCOME_APPLIED, "Applied"),
        (OUTCOME_PRE_EXISTING, "Pre-existing"),
        (OUTCOME_WHITELISTED, "Whitelisted"),
        (OUTCOME_FAILED, "Failed"),
        (OUTCOME_EXPIRED, "Expired"),
        (OUTCOME_REVERSED, "Reversed"),
    )

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    recommendation = models.ForeignKey(
        "incident.MojoSecRecommendation", related_name="targets",
        on_delete=models.PROTECT)
    # Full IPv6 length including ::ffff: IPv4-mapped notation.
    ip = models.CharField(max_length=45, db_index=True)
    kind = models.CharField(max_length=8, default="ip")
    validation_state = models.CharField(
        max_length=16, choices=VALIDATION_STATES, db_index=True)
    validation_reason = models.CharField(max_length=96, blank=True, default="")
    outcome = models.CharField(
        max_length=16, choices=OUTCOMES, default=OUTCOME_PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=256, blank=True, default="")
    applied_at = models.DateTimeField(null=True, blank=True, default=None)
    expires_at = models.DateTimeField(null=True, blank=True, default=None)
    reversed_at = models.DateTimeField(null=True, blank=True, default=None)
    prior_blocked_until = models.DateTimeField(null=True, blank=True, default=None)
    prior_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=("recommendation", "ip"), name="incident_msrt_rec_ip_uniq"),
        ]
        indexes = [
            models.Index(
                fields=("outcome", "expires_at"), name="incident_msrt_expiry_idx"),
        ]

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
                    "id", "created", "modified", "ip", "kind",
                    "validation_state", "validation_reason", "outcome",
                    "attempts", "last_error", "applied_at", "expires_at",
                    "reversed_at", "prior_blocked_until", "prior_reason",
                ],
            },
        }
