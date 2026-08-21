from django.db import models

from mojo.models import MojoModel


class MojoSecRecommendation(models.Model, MojoModel):
    """One bounded, validated MojoSec enforcement proposal for one case.

    Targets live on MojoSecRecommendationTarget and only ever come from the
    case's server-derived observed sources — never from receipt metadata,
    samples, notes, or an LLM. State changes are audited by append-only
    MojoSecRecommendationTransition rows; per-target tries by append-only
    MojoSecExecutionAttempt rows.
    """

    ACTION_BLOCK_IP = "temporary_block_ip"
    ACTION_BLOCK_IP_SET = "temporary_block_ip_set"
    ACTIONS = (
        (ACTION_BLOCK_IP, "Temporary IP block"),
        (ACTION_BLOCK_IP_SET, "Temporary IP set block"),
    )
    STATE_PROPOSED = "proposed"
    STATE_APPROVED = "approved"
    STATE_AUTO_APPROVED = "auto_approved"
    STATE_EXECUTING = "executing"
    STATE_EXECUTED = "executed"
    STATE_FAILED = "failed"
    STATE_EXPIRED = "expired"
    STATE_REVERSED = "reversed"
    STATE_REJECTED = "rejected"
    STATES = (
        (STATE_PROPOSED, "Proposed"),
        (STATE_APPROVED, "Approved"),
        (STATE_AUTO_APPROVED, "Auto-approved"),
        (STATE_EXECUTING, "Executing"),
        (STATE_EXECUTED, "Executed"),
        (STATE_FAILED, "Failed"),
        (STATE_EXPIRED, "Expired"),
        (STATE_REVERSED, "Reversed"),
        (STATE_REJECTED, "Rejected"),
    )
    # States in which a second proposal for the same (case, action) must
    # update this row instead of inserting a duplicate.
    OPEN_STATES = (
        STATE_PROPOSED, STATE_APPROVED, STATE_AUTO_APPROVED, STATE_EXECUTING)
    CONFIDENCES = (("low", "Low"), ("medium", "Medium"), ("high", "High"))
    SCOPES = (("installation", "Installation"), ("group", "Group"))

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group", null=True, blank=True, default=None,
        related_name="mojosec_recommendations", on_delete=models.SET_NULL)
    installation_key = models.ForeignKey(
        "account.ApiKey", related_name="mojosec_recommendations",
        on_delete=models.PROTECT)
    case = models.ForeignKey(
        "incident.MojoSecCase", related_name="recommendations",
        on_delete=models.PROTECT)

    action = models.CharField(max_length=32, choices=ACTIONS, db_index=True)
    state = models.CharField(
        max_length=16, choices=STATES, default=STATE_PROPOSED, db_index=True)
    reason_code = models.CharField(max_length=96)
    explanation = models.CharField(max_length=512, blank=True, default="")
    confidence = models.CharField(max_length=8, choices=CONFIDENCES)
    urgency = models.CharField(max_length=16)
    requested_scope = models.CharField(
        max_length=16, choices=SCOPES, default="installation")
    requested_ttl_seconds = models.PositiveIntegerField()
    policy_version = models.PositiveIntegerField(default=1)
    evaluator_version = models.PositiveIntegerField(default=1)
    idempotency_key = models.CharField(max_length=64, unique=True)
    # Unapproved proposals expire; the sweep flips them to STATE_EXPIRED.
    expires_at = models.DateTimeField(db_index=True)

    approved_by = models.ForeignKey(
        "account.User", null=True, blank=True, default=None,
        related_name="+", on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(null=True, blank=True, default=None)
    approval_note = models.CharField(max_length=256, blank=True, default="")

    target_count = models.PositiveIntegerField(default=0)
    validated_count = models.PositiveIntegerField(default=0)
    protected_count = models.PositiveIntegerField(default=0)
    executed_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    reversed_count = models.PositiveIntegerField(default=0)
    # Bumped each time execution is (re)queued; part of the job idempotency
    # key so a retry round is distinct from the round that already ran.
    execution_rounds = models.PositiveIntegerField(default=0)
    collateral = models.CharField(max_length=256, blank=True, default="")

    class Meta:
        ordering = ["-created", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("case", "action"),
                condition=models.Q(state__in=(
                    "proposed", "approved", "auto_approved", "executing")),
                name="incident_msr_open_uniq"),
        ]
        indexes = [
            models.Index(fields=("state", "-created"), name="incident_msr_state_idx"),
            models.Index(
                fields=("installation_key", "-created"),
                name="incident_msr_install_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "created", "modified", "action", "state",
                    "reason_code", "confidence", "urgency", "requested_scope",
                    "requested_ttl_seconds", "expires_at", "approved_at",
                    "target_count", "validated_count", "protected_count",
                    "executed_count", "failed_count", "reversed_count",
                    "policy_version", "evaluator_version",
                ],
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "action", "state",
                    "reason_code", "explanation", "confidence", "urgency",
                    "requested_scope", "requested_ttl_seconds", "expires_at",
                    "approved_at", "approval_note", "target_count",
                    "validated_count", "protected_count", "executed_count",
                    "failed_count", "reversed_count", "collateral",
                    "policy_version", "evaluator_version",
                ],
                "graphs": {"case": "reference"},
            },
        }
