from django.db import models

from mojo.models import MojoModel


class MojoSecCase(models.Model, MojoModel):
    """Bounded shadow correlation for immutable MojoSec receipt evidence."""

    STATE_OBSERVING = "observing"
    STATE_ELEVATED = "elevated"
    STATES = (
        (STATE_OBSERVING, "Observing"),
        (STATE_ELEVATED, "Elevated"),
    )
    URGENCIES = (
        ("info", "Info"),
        ("warning", "Warning"),
        ("high", "High"),
        ("critical", "Critical"),
    )

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    first_seen = models.DateTimeField(db_index=True)
    last_seen = models.DateTimeField(db_index=True)
    window_start = models.DateTimeField(db_index=True)
    window_end = models.DateTimeField(db_index=True)

    group = models.ForeignKey(
        "account.Group", null=True, blank=True, default=None,
        related_name="mojosec_cases", on_delete=models.SET_NULL)
    installation_key = models.ForeignKey(
        "account.ApiKey", related_name="mojosec_cases", on_delete=models.PROTECT)

    sensor_id = models.CharField(max_length=128, db_index=True)
    sensor_kind = models.CharField(max_length=16, db_index=True)
    resource_id = models.CharField(max_length=96, blank=True, default="", db_index=True)
    family = models.CharField(max_length=64, db_index=True)
    network = models.CharField(max_length=64, blank=True, default="", db_index=True)
    correlation_key = models.CharField(max_length=64)
    window_key = models.CharField(max_length=64)
    policy_version = models.PositiveIntegerField(default=1)
    evaluator_version = models.PositiveIntegerField(default=1)

    state = models.CharField(
        max_length=16, choices=STATES, default=STATE_OBSERVING, db_index=True)
    state_reason = models.CharField(max_length=96, default="shadow_observation")
    urgency = models.CharField(
        max_length=16, choices=URGENCIES, default="info", db_index=True)
    urgency_reason = models.CharField(max_length=96, default="unknown_evidence")

    occurrence_count = models.PositiveBigIntegerField(default=0)
    receipt_count = models.PositiveBigIntegerField(default=0)
    projected_event_count = models.PositiveBigIntegerField(default=0)
    distinct_count = models.PositiveBigIntegerField(default=0)
    sample_count = models.PositiveSmallIntegerField(default=0)
    overflow_count = models.PositiveBigIntegerField(default=0)
    samples = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-last_seen", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("installation_key", "correlation_key", "window_key"),
                name="incident_msc_window_uniq"),
        ]
        indexes = [
            models.Index(
                fields=("state", "urgency", "-last_seen"),
                name="incident_msc_state_urg_idx"),
            models.Index(
                fields=("installation_key", "sensor_kind", "-last_seen"),
                name="incident_msc_install_kind_idx"),
            models.Index(
                fields=("resource_id", "-last_seen"),
                name="incident_msc_resource_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["samples"]
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "created", "first_seen", "last_seen", "sensor_kind",
                    "resource_id", "family", "state", "state_reason", "urgency",
                    "urgency_reason", "occurrence_count", "receipt_count",
                    "projected_event_count", "distinct_count", "sample_count",
                    "overflow_count", "policy_version", "evaluator_version",
                ],
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "first_seen", "last_seen",
                    "window_start", "window_end", "sensor_id", "sensor_kind",
                    "resource_id", "family", "network", "state", "state_reason",
                    "urgency", "urgency_reason", "occurrence_count", "receipt_count",
                    "projected_event_count", "distinct_count", "sample_count",
                    "overflow_count", "samples", "policy_version", "evaluator_version",
                ],
            },
        }
