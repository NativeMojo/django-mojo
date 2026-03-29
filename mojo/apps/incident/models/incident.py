from django.db import models
from mojo.models import MojoModel
import logging

logger = logging.getLogger(__name__)


class Incident(models.Model, MojoModel):
    id = models.BigAutoField(primary_key=True)
    """
    Incident model.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    priority = models.IntegerField(default=0, db_index=True)
    state = models.CharField(max_length=24, default=0, db_index=True)
    # new, open, paused, closed
    status = models.CharField(max_length=50, default='new', db_index=True)
    scope = models.CharField(max_length=64, db_index=True, default="global")
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
        VIEW_PERMS = ["view_security"]
        CREATE_PERMS = None
        SAVE_PERMS = ["manage_security"]
        DELETE_PERMS = ["manage_security"]
        POST_SAVE_ACTIONS = ["merge"]
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "graphs": {
                    "geo_ip": "basic",
                },
            },
            "basic": {
                "fields": ["id", "created", "priority", "status", "scope", "category",
                           "country_code", "title", "source_ip", "hostname"],
            },
        }


    _geo_ip = None
    @property
    def geo_ip(self):
        if self._geo_ip is None and self.source_ip:
            from mojo.apps.account.models import GeoLocatedIP
            try:
                self._geo_ip = GeoLocatedIP.objects.filter(ip_address=self.source_ip).first()
            except Exception:
                pass
        return self._geo_ip

    def add_history(self, kind, note=None, by=None, to=None, group=None, media=None):
        """
        Record a history entry for this incident.

        Args:
            kind: Type of change (e.g., "created", "priority_changed", "handler:block")
            note: Human-readable description of what happened
            by: User who made the change (None for system actions)
            to: Target user (for assignments)
            group: Group context
            media: Attached file evidence
        """
        try:
            from mojo.apps.incident.models import IncidentHistory
            IncidentHistory.objects.create(
                parent=self,
                kind=kind,
                note=note,
                by=by,
                to=to,
                group=group,
                media=media,
                state=self.state,
                priority=self.priority,
            )
        except Exception:
            logger.exception("Failed to create IncidentHistory for incident %s", self.pk)

    def on_rest_saved(self, changed_fields, created):
        if created:
            return

        by = getattr(self.active_request, 'user', None) if self.active_request else None

        if 'status' in changed_fields:
            self.add_history("status_changed",
                note=f"Status changed from {changed_fields['status']} to {self.status}",
                by=by)
            if self.status == "resolved":
                try:
                    from mojo.apps import metrics
                    from mojo.helpers.settings import settings
                    if settings.INCIDENT_EVENT_METRICS:
                        metrics.record('incidents:resolved', account="incident",
                            min_granularity=settings.get_static("INCIDENT_METRICS_MIN_GRANULARITY", "hours"))
                except Exception:
                    pass

        if 'priority' in changed_fields:
            self.add_history("priority_changed",
                note=f"Priority changed from {changed_fields['priority']} to {self.priority}",
                by=by)

        if 'state' in changed_fields:
            self.add_history("state_changed",
                note=f"State changed from {changed_fields['state']} to {self.state}",
                by=by)

        # Track other field changes as a single "updated" entry
        other_fields = set(changed_fields.keys()) - {'status', 'priority', 'state'}
        if other_fields:
            self.add_history("updated",
                note=f"Fields updated: {', '.join(sorted(other_fields))}",
                by=by)

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

        by = getattr(getattr(self, 'active_request', None), 'user', None)

        for incident in other_incidents:
            event_count = incident.events.count()
            # Move all events from the other incident to this incident
            incident.events.update(incident=self)

            self.add_history("merged",
                note=f"Merged incident #{incident.id} ({event_count} events)",
                by=by)

            # Delete the now-empty incident
            incident.delete()
        return {"status": True}
