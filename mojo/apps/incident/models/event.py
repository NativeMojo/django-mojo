from mojo.apps.metrics import record
from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates
from mojo.helpers.settings import settings
from mojo.apps import metrics
from mojo.apps.account.models import GeoLocatedIP


INCIDENT_LEVEL_THRESHOLD = settings.get_static('INCIDENT_LEVEL_THRESHOLD', 7)
INCIDENT_METRICS_MIN_GRANULARITY = settings.get_static("INCIDENT_METRICS_MIN_GRANULARITY", "hours")
LLM_API_KEY = settings.get_static("LLM_HANDLER_API_KEY", None)

class Event(models.Model, MojoModel):
    id = models.BigAutoField(primary_key=True)
    """
    Event model.

    Level 0–3: Informational or low importance
	Level 4–7: Warning or potential issue
	Level 8–15: Increasing severity, with Level 15 being critical
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    level = models.IntegerField(default=0, db_index=True)
    scope = models.CharField(max_length=64, db_index=True, default="global")
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
        VIEW_PERMS = ["view_security", "security"]
        CREATE_PERMS = ["all"]
        SAVE_PERMS = ["manage_security", "security"]
        GRAPHS = {
            "default": {
                "graphs": {
                    "incident": "basic",
                    "geo_ip": "basic",
                }
            },
            "security": {
                "fields": ["created"],
                "extra": [
                    ("category", "kind"),
                    ("security_summary", "summary"),
                    ("source_ip", "ip"),
                ],
            },
        }

        FORMATS = {
            "csv": [
                "created",
                "level",
                "scope",
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

    # kind → human-readable summary for the security events graph
    _SECURITY_SUMMARIES = {
        "login": "Successful login",
        "login:unknown": "Login attempt with unknown account",
        "invalid_password": "Failed login — incorrect password",
        "password_reset": "Password reset requested",
        "totp:confirm_failed": "TOTP setup — invalid confirmation code",
        "totp:login_failed": "Failed login — incorrect TOTP code",
        "totp:login_unknown": "TOTP login attempt with unknown account",
        "totp:recovery_used": "TOTP recovery code used",
        "email_change:requested": "Email change requested",
        "email_change:requested_code": "Email change requested (code flow)",
        "email_change:cancelled": "Email change cancelled",
        "email_change:invalid": "Email change — invalid token",
        "email_change:expired": "Email change — expired token",
        "email_verify:confirmed": "Email address verified",
        "email_verify:confirmed_code": "Email address verified via code",
        "phone_change:requested": "Phone number change requested",
        "phone_change:confirmed": "Phone number changed",
        "phone_change:cancelled": "Phone number change cancelled",
        "phone_verify:confirmed": "Phone number verified",
        "username:changed": "Username changed",
        "oauth": "Signed in with social account",
        "passkey:login_failed": "Failed passkey login",
        "account:deactivated": "Account deactivated",
        "account:deactivate_requested": "Account deactivation requested",
        "sessions:revoked": "All sessions revoked",
        "sessions:revoke_failed": "Session revoke — incorrect password",
    }

    @property
    def security_summary(self):
        return self._SECURITY_SUMMARIES.get(self.category, self.category)

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
            'scope': self.scope,
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
        # check by scope first
        rule_set = RuleSet.check_by_category(self.scope, self)
        if rule_set is None:
            rule_set = RuleSet.check_by_category(self.category, self)
        if rule_set is None:
            rule_set = RuleSet.check_by_category("*", self)

        # Honor action=ignore from RuleSet metadata
        if rule_set and rule_set.handler == "ignore":
            return

        # Read trigger config from model fields
        trigger_count = None
        trigger_window = None
        retrigger_every = None
        if rule_set:
            trigger_count = rule_set.trigger_count
            trigger_window = rule_set.trigger_window
            retrigger_every = rule_set.retrigger_every
            # Fall back to bundle_minutes for the count window if trigger_window not set
            if trigger_window is None and rule_set.bundle_minutes and rule_set.bundle_minutes > 0:
                trigger_window = rule_set.bundle_minutes

        if rule_set or self.level >= INCIDENT_LEVEL_THRESHOLD:
            incident, created = self.get_or_create_incident(rule_set)

            # Capture status BEFORE any modifications for transition detection
            prev_status = incident.status if incident.pk else None

            # Count events already on this incident (before linking current event)
            # +1 to include the event currently being published
            meets_threshold = True
            incident_event_count = 1
            if trigger_count is not None:
                try:
                    qs = incident.events
                    if trigger_window:
                        qs = qs.filter(created__gte=dates.subtract(minutes=trigger_window))
                    incident_event_count = qs.count() + 1
                except Exception:
                    incident_event_count = 1
                meets_threshold = incident_event_count >= trigger_count

            # Status transition based on trigger_count threshold
            # - Below threshold: hold at (or set to) pending
            # - Threshold met and was pending: transition to new
            # - Threshold met and already past pending: leave status alone
            if rule_set and trigger_count is not None:
                try:
                    if not meets_threshold:
                        # Not yet at threshold — force/keep pending
                        if incident.status != "pending":
                            incident.status = "pending"
                            incident.save(update_fields=["status"])
                    elif incident.status == "pending":
                        # Was pending, threshold just reached — transition to new
                        incident.status = "new"
                        incident.save(update_fields=["status"])
                        incident.add_history("threshold_reached",
                            note=f"Threshold met: {incident_event_count} events (trigger_count: {trigger_count})")
                        if settings.INCIDENT_EVENT_METRICS:
                            metrics.record('incidents:threshold_reached', account="incident",
                                min_granularity=INCIDENT_METRICS_MIN_GRANULARITY)
                except Exception:
                    pass

            self.link_to_incident(incident)

            # Run handlers on creation or when transitioning from pending -> new
            if rule_set:
                transitioned_to_new = (prev_status == "pending" and incident.status == "new")
                if (created and (trigger_count is None or meets_threshold)) or transitioned_to_new:
                    rule_set.run_handler(self, incident)
                    # Track event count at trigger so re-trigger knows the baseline
                    if retrigger_every is not None:
                        try:
                            meta = dict(incident.metadata or {})
                            meta["last_trigger_count"] = incident.events.count()
                            incident.metadata = meta
                            incident.save(update_fields=["metadata"])
                        except Exception:
                            pass
                elif retrigger_every is not None and incident.status in ("new", "open", "investigating"):
                    try:
                        total = incident.events.count()
                        last = (incident.metadata or {}).get("last_trigger_count")
                        if last is None:
                            last = trigger_count or 1
                        if total >= last + retrigger_every:
                            meta = dict(incident.metadata or {})
                            meta["last_trigger_count"] = total
                            incident.metadata = meta
                            incident.save(update_fields=["metadata"])
                            incident.add_history("handler_retriggered",
                                note=f"Re-triggered: {total} events (retrigger_every: {retrigger_every})")
                            rule_set.run_handler(self, incident)
                    except Exception:
                        pass
            elif created and LLM_API_KEY:
                # No rule matched but level exceeded threshold — default to LLM triage
                try:
                    from mojo.apps import jobs
                    jobs.publish(
                        "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                        {
                            "event_id": self.pk,
                            "incident_id": incident.pk,
                            "ruleset_id": None,
                        },
                        channel="incident_handlers",
                    )
                except Exception:
                    pass

    def record_event_metrics(self):
        if settings.INCIDENT_EVENT_METRICS:
            metrics.record('incident_events', account="incident",
                min_granularity=INCIDENT_METRICS_MIN_GRANULARITY)
            if self.country_code:
                metrics.record(f'incident_events:country:{self.country_code}',
                    account="incident",
                    category="incident_events_by_country",
                    min_granularity=INCIDENT_METRICS_MIN_GRANULARITY)

    def record_incident_metrics(self):
        if settings.INCIDENT_EVENT_METRICS:
            metrics.record('incidents', account="incident",
                min_granularity=INCIDENT_METRICS_MIN_GRANULARITY)
            if self.country_code:
                metrics.record(f'incident:country:{self.country_code}',
                    account="incident",
                    category="incidents_by_country",
                    min_granularity=INCIDENT_METRICS_MIN_GRANULARITY)

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
                old_priority = incident.priority
                incident.priority = self.level
                try:
                    incident.save(update_fields=['priority'])
                except Exception:
                    incident.save()
                incident.add_history("priority_escalated",
                    note=f"Priority escalated from {old_priority} to {self.level} by event (category: {self.category})")
                if settings.INCIDENT_EVENT_METRICS:
                    metrics.record('incidents:escalated', account="incident",
                        min_granularity=INCIDENT_METRICS_MIN_GRANULARITY)

        if not incident:
            # Create a new incident if none found
            created = True
            self.sync_metadata()
            incident = Incident(
                priority=self.level,
                state=0,
                rule_set=rule_set,
                scope=self.scope,
                category=self.category,
                country_code=self.country_code,
                title=self.title,
                details=self.details,
                hostname=self.hostname,
                model_name=self.model_name,
                model_id=self.model_id,
                source_ip=self.source_ip
            )
            self.save()
            incident.metadata.update(self.metadata)
            incident.save()
            self.record_incident_metrics()
            rule_name = rule_set.name if rule_set else "level threshold"
            incident.add_history("created",
                note=f"Incident created from event (category: {self.category}, level: {self.level}, rule: {rule_name})")

            # Update IP threat level when a new incident is created
            if self.source_ip and self.geo_ip:
                try:
                    self.geo_ip.update_threat_from_incident(self.level)
                except Exception:
                    pass

        return incident, created

    def determine_bundle_criteria(self, rule_set):
        """
        Determines the bundle criteria based on the rule set configuration.
        """
        from mojo.apps.incident.models.rule import BundleBy

        bundle_criteria = {
            "category": self.category
        }

        if rule_set.bundle_by_rule_set:
            bundle_criteria['rule_set'] = rule_set

        # Add time window if specified
        # bundle_minutes=0 or None means disabled (don't add time filter, will not find existing incidents)
        # bundle_minutes>0 means only bundle within that time window
        if rule_set.bundle_minutes and rule_set.bundle_minutes > 0:
            bundle_criteria['created__gte'] = dates.subtract(minutes=rule_set.bundle_minutes)
        elif rule_set.bundle_minutes == 0:
            # bundle_minutes=0 means disabled - make criteria impossible to match
            # by requiring a specific timestamp that won't exist
            from django.utils import timezone
            bundle_criteria['created__exact'] = timezone.now()

        # Add field-based criteria using named constants
        if rule_set.bundle_by in [BundleBy.HOSTNAME, BundleBy.HOSTNAME_AND_MODEL_NAME,
                                   BundleBy.HOSTNAME_AND_MODEL_NAME_AND_ID, BundleBy.SOURCE_IP_AND_HOSTNAME]:
            bundle_criteria['hostname'] = self.hostname

        if rule_set.bundle_by in [BundleBy.MODEL_NAME, BundleBy.MODEL_NAME_AND_ID,
                                   BundleBy.HOSTNAME_AND_MODEL_NAME, BundleBy.HOSTNAME_AND_MODEL_NAME_AND_ID,
                                   BundleBy.SOURCE_IP_AND_MODEL_NAME, BundleBy.SOURCE_IP_AND_MODEL_NAME_AND_ID]:
            bundle_criteria['model_name'] = self.model_name
            if rule_set.bundle_by in [BundleBy.MODEL_NAME_AND_ID, BundleBy.HOSTNAME_AND_MODEL_NAME_AND_ID,
                                       BundleBy.SOURCE_IP_AND_MODEL_NAME_AND_ID]:
                bundle_criteria['model_id'] = self.model_id

        if rule_set.bundle_by in [BundleBy.SOURCE_IP, BundleBy.SOURCE_IP_AND_MODEL_NAME,
                                   BundleBy.SOURCE_IP_AND_MODEL_NAME_AND_ID, BundleBy.SOURCE_IP_AND_HOSTNAME]:
            bundle_criteria['source_ip'] = self.source_ip

        return bundle_criteria

    def link_to_incident(self, incident):
        """
        Links the event to an incident and saves the event.
        """
        self.incident = incident
        self.save()
