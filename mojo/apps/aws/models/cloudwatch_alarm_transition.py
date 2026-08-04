from django.db import models

from mojo.models import MojoModel


class CloudWatchAlarmTransition(models.Model, MojoModel):
    """An exact, idempotent CloudWatch state transition delivered by SNS."""

    DISPATCH_PENDING = "pending"
    DISPATCH_COMPLETE = "complete"

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    alarm = models.ForeignKey(
        "aws.CloudWatchAlarm", related_name="transitions", on_delete=models.PROTECT,
    )
    topic_arn = models.TextField()
    sns_message_id = models.CharField(max_length=100)
    old_state = models.CharField(max_length=32)
    new_state = models.CharField(max_length=32)
    state_changed_at = models.DateTimeField(db_index=True)
    event = models.ForeignKey(
        "incident.Event", related_name="+", blank=True, null=True,
        default=None, on_delete=models.SET_NULL,
    )
    incident = models.ForeignKey(
        "incident.Incident", related_name="+", blank=True, null=True,
        default=None, on_delete=models.SET_NULL,
    )
    dispatch_status = models.CharField(
        max_length=16, default=DISPATCH_COMPLETE, db_index=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["topic_arn", "sns_message_id"],
                name="aws_cw_topic_message_unique",
            ),
            models.UniqueConstraint(
                fields=["alarm", "state_changed_at", "old_state", "new_state"],
                name="aws_cw_logical_transition_unique",
            ),
        ]

    class RestMeta:
        VIEW_PERMS = ["manage_aws", "security"]
        SAVE_PERMS = ["manage_aws", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "topic_arn", "sns_message_id", "old_state",
                    "new_state", "state_changed_at", "dispatch_status",
                    "created", "modified",
                ],
                "extra": ["alarm_id", "event_id", "incident_id"],
            },
        }

