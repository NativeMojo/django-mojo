from mojo.apps.metrics import record
from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates
from mojo.helpers.settings import settings
from mojo.apps import metrics
from mojo.apps.account.models import GeoLocatedIP


INCIDENT_LEVEL_THRESHOLD = settings.get('INCIDENT_LEVEL_THRESHOLD', 7)

class Event(models.Model, MojoModel):
    """
    Event model.

    Level 0–3: Informational or low importance
	Level 4–7: Warning or potential issue
	Level 8–15: Increasing severity, with Level 15 being critical
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    level = models.IntegerField(default=0, db_index=True)
    category = models.CharField(max_length=124, db_index=True)
    source_ip = models.CharField(max_length=16, null=True, default=None, db_index=True)
    hostname = models.CharField(max_length=16, null=True, default=None, db_index=True)
    uid = models.IntegerField(default=None, null=True, db_index=True)
    country_code = models.CharField(max_length=2, default=None, null=True, db_index=True)

    title = models.TextField(default=None, null=True)
    details = models.TextField(default=None, null=True)

    model_name = models.TextField(default=None, null=True, db_index=True)
    model_id = models.IntegerField(default=None, null=True, db_index=True)

    incident = models.ForeignKey("incident.Incident", null=True, related_name="events",
        default=None, on_delete=models.CASCADE)

    # JSON-based metadata field
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        SEARCH_FIELDS = ["details"]
        VIEW_PERMS = ["view_incidents"]
        CREATE_PERMS = ["all"]
        SAVE_PERMS = ["edit_incidents"]
        GRAPHS = {
            "default": {
                "graphs": {
                    "incident": "basic",
                }
            },
        }

        FORMATS = {
            "csv": [
                "created",
                "level",
                "category",
                "source_ip",
                "hostname",
                "uid",
                "country_code",
                "title",
                "details",
                "model_name",
                "model_id",
                "metadata.text",
                "metadata.request_ip",
                "metadata.source_ip",
                "metadata.ext_ip",
                "metadata.ip",
                "metadata.rule_id",
                "incident.id",
            ]
        }

    _geo_ip = None
    @property
    def geo_ip(self):
        if self._geo_ip is None and self.source_ip:
            try:
                self._geo_ip = GeoLocatedIP.geolocate(self.source_ip, subdomain_only=True)
            except Exception:
                pass
        return self._geo_ip

    def sync_metadata(self):
        # Gather all field values into the metadata
        field_values = {
            'level': self.level,
            'category': self.category,
            'source_ip': self.source_ip,
            'title': self.title,
            'details': self.details,
            'model_name': self.model_name,
            'model_id': self.model_id        }

        if not self.country_code and self.geo_ip:
            self.country_code = self.geo_ip.country_code
            field_values["country_code"] = self.geo_ip.country_code
            field_values["country_name"] = self.geo_ip.country_name
            field_values["city"] = self.geo_ip.city
            field_values["region"] = self.geo_ip.region
            field_values["latitude"] = self.geo_ip.latitude
            field_values["longitude"] = self.geo_ip.longitude
            field_values["timezone"] = self.geo_ip.timezone

        # Update the metadata with these values
        self.metadata.update(field_values)

    def publish(self):
        from mojo.apps.incident.models import RuleSet
        # Record metrics and find the RuleSet by category
        self.record_event_metrics()
        rule_set = RuleSet.check_by_category(self.category, self)

        # Honor action=ignore from RuleSet metadata
        if rule_set and isinstance(rule_set.metadata, dict) and rule_set.metadata.get("action") == "ignore":
            return

        # Threshold-based pending/open logic
        min_count = None
        window_minutes = None
        pending_status = "pending"
        if rule_set and isinstance(rule_set.metadata, dict):
            try:
                min_count = int(rule_set.metadata.get("min_count")) if rule_set.metadata.get("min_count") is not None else None
            except Exception:
                min_count = None
            try:
                window_minutes = int(rule_set.metadata.get("window_minutes")) if rule_set.metadata.get("window_minutes") is not None else None
            except Exception:
                window_minutes = None
            pending_status = rule_set.metadata.get("pending_status", "pending")

        # Fallback to ruleset bundling window when not provided
        if window_minutes is None and rule_set and rule_set.bundle_minutes:
            window_minutes = rule_set.bundle_minutes

        # Count recent matching events to evaluate threshold
        meets_threshold = True
        event_count = 1
        if rule_set and (min_count or window_minutes) and rule_set.bundle_by > 0:
            criteria = {"category": self.category}
            if window_minutes:
                criteria["created__gte"] = dates.subtract(minutes=window_minutes)
            b = rule_set.bundle_by
            if b in [1, 5, 6, 9]:
                criteria["hostname"] = self.hostname
            if b in [2, 3, 5, 6, 7, 8]:
                criteria["model_name"] = self.model_name
                if b in [3, 6, 8]:
                    criteria["model_id"] = self.model_id
            if b in [4, 7, 8, 9]:
                criteria["source_ip"] = self.source_ip

            try:
                event_count = self.__class__.objects.filter(**criteria).count()
            except Exception:
                event_count = 1

            if min_count:
                meets_threshold = event_count >= min_count

        if rule_set or self.level >= INCIDENT_LEVEL_THRESHOLD:
            incident, created = self.get_or_create_incident(rule_set)

            # Determine status transitions for pending/open
            prev_status = getattr(incident, "status", None)
            if rule_set and (min_count or window_minutes):
                try:
                    desired_status = "open" if meets_threshold else pending_status
                    if incident.status != desired_status:
                        incident.status = desired_status
                        incident.save(update_fields=["status"])
                except Exception:
                    pass

            self.link_to_incident(incident)

            # Run handlers on creation or when transitioning from pending -> open
            if rule_set:
                transitioned_to_open = (prev_status == pending_status and getattr(incident, "status", None) == "open")
                if (created and (min_count is None or meets_threshold)) or transitioned_to_open:
                    rule_set.run_handler(self, incident)

    def record_event_metrics(self):
        if settings.INCIDENT_EVENT_METRICS:
            metrics.record('incident_events', account="incident",
                min_granularity=settings.get("INCIDENT_METRICS_MIN_GRANULARITY", "hours"))
            if self.country_code:
                metrics.record(f'incident_events:country:{self.country_code}',
                    account="incident",
                    category="incident_events_by_country",
                    min_granularity=settings.get("INCIDENT_METRICS_MIN_GRANULARITY", "hours"))

    def record_incident_metrics(self):
        if settings.INCIDENT_EVENT_METRICS:
            metrics.record('incidents', account="incident",
                min_granularity=settings.get("INCIDENT_METRICS_MIN_GRANULARITY", "hours"))
            if self.country_code:
                metrics.record(f'incident:country:{self.country_code}',
                    account="incident",
                    category="incidents_by_country",
                    min_granularity=settings.get("INCIDENT_METRICS_MIN_GRANULARITY", "hours"))

    def get_or_create_incident(self, rule_set=None):
        """
        Gets or creates an incident based on the event's level and rule set bundle criteria.
        """
        from mojo.apps.incident.models import Incident

        incident = None
        created = False
        if rule_set is not None and rule_set.bundle_by > 0:
            bundle_criteria = self.determine_bundle_criteria(rule_set)
            incident = Incident.objects.filter(**bundle_criteria).first()
            # Escalate priority when reusing an existing incident
            if incident and self.level > incident.priority:
                incident.priority = self.level
                try:
                    incident.save(update_fields=['priority'])
                except Exception:
                    incident.save()

        if not incident:
            # Create a new incident if none found
            created = True
            incident = Incident(
                priority=self.level,
                state=0,
                category=self.category,
                country_code=self.country_code,
                title=self.title,
                details=self.details,
                hostname=self.hostname,
                model_name=self.model_name,
                model_id=self.model_id,
                source_ip=self.source_ip
            )
            incident.metadata.update(self.metadata)
            incident.save()
            self.record_incident_metrics()

        return incident, created

    def determine_bundle_criteria(self, rule_set):
        """
        Determines the bundle criteria based on the rule set configuration.
        """
        bundle_criteria = {
            "category": self.category
        }
        if rule_set.bundle_minutes:
            bundle_criteria['created__gte'] = dates.subtract(minutes=rule_set.bundle_minutes)
        if rule_set.bundle_by in [1, 5, 6, 9]:  # hostname or hostname and others
            bundle_criteria['hostname'] = self.hostname
        if rule_set.bundle_by in [2, 3, 5, 6, 7, 8]:  # model and combinations
            bundle_criteria['model_name'] = self.model_name
            if rule_set.bundle_by in [3, 6, 8]:  # model_id where applicable
                bundle_criteria['model_id'] = self.model_id
        if rule_set.bundle_by in [4, 7, 8, 9]:  # source_ip and combinations
            bundle_criteria['source_ip'] = self.source_ip

        return bundle_criteria

    def link_to_incident(self, incident):
        """
        Links the event to an incident and saves the event.
        """
        self.incident = incident
        self.save()
