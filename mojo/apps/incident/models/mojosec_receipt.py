from django.db import models

from mojo.models import MojoModel


class MojoSecReceipt(models.Model, MojoModel):
    """Durable idempotency and publication state for one sensor event."""

    PUBLISH_PENDING = "pending"
    PUBLISH_PUBLISHED = "published"
    PUBLISH_STATES = (
        (PUBLISH_PENDING, "Pending"),
        (PUBLISH_PUBLISHED, "Published"),
    )

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    api_key = models.ForeignKey(
        "account.ApiKey", null=True, blank=True, default=None,
        related_name="mojosec_receipts", on_delete=models.SET_NULL)
    event = models.ForeignKey(
        "incident.Event", null=True, blank=True, default=None,
        related_name="mojosec_receipts", on_delete=models.SET_NULL)

    sensor_id = models.CharField(max_length=128, db_index=True)
    wire_event_id = models.CharField(max_length=64)
    payload_digest = models.CharField(max_length=64)
    protocol_version = models.PositiveSmallIntegerField(default=1)
    sensor_policy_revision = models.CharField(max_length=128, blank=True, default="")
    publish_state = models.CharField(
        max_length=16, choices=PUBLISH_STATES, default=PUBLISH_PENDING, db_index=True)
    publish_attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=256, blank=True, default="")
    replay_features = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=("sensor_id", "wire_event_id"), name="incident_mojosec_sensor_event_uniq"),
        ]
        indexes = [
            models.Index(fields=("publish_state", "modified"), name="incident_mo_publish_0ddfd1_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["replay_features", "last_error", "payload_digest"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified", "published_at", "sensor_id",
                    "wire_event_id", "protocol_version", "sensor_policy_revision",
                    "publish_state", "publish_attempts",
                ],
                "graphs": {"event": "reference"},
            },
        }
