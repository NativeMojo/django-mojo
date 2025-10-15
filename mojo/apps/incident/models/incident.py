from django.db import models
from mojo.models import MojoModel


class Incident(models.Model, MojoModel):
    """
    Incident model.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    priority = models.IntegerField(default=0, db_index=True)
    state = models.CharField(max_length=24, default=0, db_index=True)
    # new, open, paused, closed
    status = models.CharField(max_length=50, default='new', db_index=True)
    category = models.CharField(max_length=124, db_index=True)
    country_code = models.CharField(max_length=2, default=None, null=True, db_index=True)
    title = models.TextField(default=None, null=True)
    details = models.TextField(default=None, null=True)

    model_name = models.TextField(default=None, null=True, db_index=True)
    model_id = models.IntegerField(default=None, null=True, db_index=True)

    # the
    source_ip = models.CharField(max_length=16, null=True, default=None, db_index=True)
    hostname = models.CharField(max_length=16, null=True, default=None, db_index=True)

    # JSON-based metadata field
    metadata = models.JSONField(default=dict, blank=True)

    rule_set = models.ForeignKey("incident.Ruleset", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="incidents")

    class RestMeta:
        SEARCH_FIELDS = ["details"]
        VIEW_PERMS = ["view_incidents"]
        CREATE_PERMS = None
        POST_SAVE_ACTIONS = ["merge"]
        CAN_DELETE = True


    def on_action_merge(self, value):
        """
        Merge events from other incidents into this incident and delete the other incidents.

        Args:
            value: List of Incident ids to merge into this incident
        """
        if not value or not isinstance(value, list):
            raise ValueError("Invalid value")

        # Get the other incidents to merge
        other_incidents = Incident.objects.filter(id__in=value).exclude(id=self.id)

        for incident in other_incidents:
            # Move all events from the other incident to this incident
            incident.events.update(incident=self)

            # Delete the now-empty incident
            incident.delete()
        return {"status": True }
