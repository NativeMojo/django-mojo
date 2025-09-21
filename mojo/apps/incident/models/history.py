from django.db import models
from mojo.models import MojoModel

class IncidentHistory(models.Model, MojoModel):
    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ["manage_incidents", "view_incidents"]
        SAVE_PERMS = ["manage_incidents"]
        DELETE_PERMS = ["manage_incidents"]
        CAN_DELETE = False  # History should not be deletable

        GRAPHS = {
            "default": {
                "extra": [
                    ("get_state_display", "state_display"),
                    ("get_priority_display", "priority_display"),
                ],
                "graphs": {
                    "by": "basic",
                    "to": "basic",
                    "media": "basic"
                }
            },
        }
    parent = models.ForeignKey("incident.Incident", related_name="history", on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True, editable=False)

    group = models.ForeignKey("account.Group", blank=True, null=True, default=None, related_name="+", on_delete=models.CASCADE)

    kind = models.CharField(max_length=80, blank=True, null=True, default=None, db_index=True)

    to = models.ForeignKey("account.User", blank=True, null=True, default=None, related_name="+", on_delete=models.CASCADE)
    by = models.ForeignKey("account.User", blank=True, null=True, default=None, related_name="+", on_delete=models.CASCADE)

    state = models.IntegerField(default=0)
    priority = models.IntegerField(default=0)

    note = models.TextField(blank=True, null=True, default=None)
    media = models.ForeignKey("fileman.File", related_name="+", null=True, default=None, on_delete=models.CASCADE)
