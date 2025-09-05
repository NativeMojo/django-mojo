from django.db import models
from mojo.models import MojoModel


class Incident(models.Model, MojoModel):
    """
    Incident model.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    priority = models.IntegerField(default=0, db_index=True)
    state = models.CharField(max_length=24, default=0, db_index=True)
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
