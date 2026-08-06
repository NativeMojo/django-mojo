from django.db import models

from mojo.models import MojoModel


class GuardDutyFinding(models.Model, MojoModel):
    """Durable lifecycle state for one exact GuardDuty finding.

    Identity is the SHA-256 of ``account:region:detector_id:finding_id``, NOT
    the finding id alone. GuardDuty ids are unique per detector, and fanning
    several accounts and regions into one SNS topic is the normal deployment
    shape — keying on the bare id would silently merge two unrelated findings.
    """

    DISPATCH_PENDING = "pending"
    DISPATCH_COMPLETE = "complete"

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    finding_key = models.CharField(max_length=64, unique=True)
    finding_id = models.CharField(max_length=128, blank=True, default="")
    detector_id = models.CharField(max_length=64, blank=True, default="")
    account = models.CharField(max_length=12, blank=True, default="", db_index=True)
    region = models.CharField(max_length=64, blank=True, default="")
    finding_type = models.CharField(max_length=100, blank=True, default="", db_index=True)
    severity = models.FloatField(default=0.0)
    level = models.IntegerField(default=0)

    # Monotonic dedupe watermark. GuardDuty re-publishes a finding as its
    # occurrence count grows, so an updatedAt at or before this value is a
    # replay or an out-of-order delivery and must change nothing.
    last_updated_at = models.DateTimeField(
        blank=True, null=True, default=None, db_index=True,
    )
    occurrence_count = models.IntegerField(default=0)

    active_incident = models.ForeignKey(
        "incident.Incident", related_name="+", blank=True, null=True,
        default=None, on_delete=models.SET_NULL,
    )
    # The Event that opened the current occurrence. Stamped onto every Event
    # of that occurrence as ``model_id`` and CLEARED when the incident goes
    # terminal, so a finding that recurs after resolution bundles into a new
    # incident instead of reopening the resolved one.
    opening_event = models.ForeignKey(
        "incident.Event", related_name="+", blank=True, null=True,
        default=None, on_delete=models.SET_NULL,
    )
    pending_event = models.ForeignKey(
        "incident.Event", related_name="+", blank=True, null=True,
        default=None, on_delete=models.SET_NULL,
    )
    dispatch_status = models.CharField(
        max_length=16, default=DISPATCH_COMPLETE, db_index=True,
    )

    class RestMeta:
        VIEW_PERMS = ["manage_aws", "security"]
        SAVE_PERMS = ["manage_aws", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "finding_id", "detector_id", "account", "region",
                    "finding_type", "severity", "level", "last_updated_at",
                    "occurrence_count", "dispatch_status", "created", "modified",
                ],
                "extra": [
                    "active_incident_id", "opening_event_id", "pending_event_id",
                ],
            },
        }
