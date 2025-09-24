from django.db import models
from mojo.models import MojoModel


class Ticket(models.Model, MojoModel):
    class Meta:
        ordering = ['-modified']

    class RestMeta:
        VIEW_PERMS = ['view_incidents']
        SAVE_PERMS = ['manage_incidents']
        GRAPHS = {
            "default": {
                "graphs": {
                    "assignee": "basic",
                    "incident": "basic",
                    "user": "basic",
                    "group": "basic"
                }
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True)

    user = models.ForeignKey("account.User", blank=True, null=True, default=None, related_name="+", on_delete=models.SET_NULL)
    group = models.ForeignKey("account.Group", blank=True, null=True, default=None, related_name="+", on_delete=models.SET_NULL)

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True, default=None)

    status = models.CharField(max_length=50, default='open', db_index=True)
    priority = models.IntegerField(default=1, db_index=True)
    category = models.CharField(max_length=80, default='ticket', db_index=True)

    assignee = models.ForeignKey("account.User", blank=True, null=True, default=None, related_name="assigned_tickets", on_delete=models.SET_NULL)
    incident = models.ForeignKey("incident.Incident", blank=True, null=True, default=None, related_name="tickets", on_delete=models.SET_NULL)

    metadata = models.JSONField(default=dict, blank=True)


class TicketNote(models.Model, MojoModel):
    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ['view_incidents']
        SAVE_PERMS = ['manage_incidents']
        GRAPHS = {
            "default": {
                "graphs": {
                    "user": "basic",
                    "media": "basic"
                }
            },
        }

    parent = models.ForeignKey(Ticket, related_name="notes", on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True, editable=False)

    user = models.ForeignKey("account.User", related_name="+", on_delete=models.CASCADE)
    note = models.TextField(blank=True, null=True)
    media = models.ForeignKey("fileman.File", related_name="+", null=True, blank=True, default=None, on_delete=models.SET_NULL)
