from django.db import models

from mojo.models import MojoModel


class CloudWatchAlarm(models.Model, MojoModel):
    """Durable lifecycle state for one exact CloudWatch alarm ARN."""

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    alarm_key = models.CharField(max_length=64, unique=True)
    alarm_arn = models.TextField()
    current_state = models.CharField(max_length=32, blank=True, default="")
    last_state_change = models.DateTimeField(blank=True, null=True, default=None)
    active_incident = models.ForeignKey(
        "incident.Incident", related_name="+", blank=True, null=True,
        default=None, on_delete=models.SET_NULL,
    )
    opening_transition = models.ForeignKey(
        "aws.CloudWatchAlarmTransition", related_name="+", blank=True,
        null=True, default=None, on_delete=models.SET_NULL,
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
                    "id", "alarm_arn", "current_state", "last_state_change",
                    "created", "modified",
                ],
                "extra": ["active_incident_id", "opening_transition_id"],
            },
        }

