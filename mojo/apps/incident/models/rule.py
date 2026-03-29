import logging
import re
from django.db import models
from mojo.models import MojoModel
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


class BundleBy:
    """Named constants for bundle_by field values."""
    NONE = 0
    HOSTNAME = 1
    MODEL_NAME = 2
    MODEL_NAME_AND_ID = 3
    SOURCE_IP = 4
    HOSTNAME_AND_MODEL_NAME = 5
    HOSTNAME_AND_MODEL_NAME_AND_ID = 6
    SOURCE_IP_AND_MODEL_NAME = 7
    SOURCE_IP_AND_MODEL_NAME_AND_ID = 8
    SOURCE_IP_AND_HOSTNAME = 9

    CHOICES = [
        (NONE, "Don't bundle - each event creates new incident"),
        (HOSTNAME, "Bundle by hostname"),
        (MODEL_NAME, "Bundle by model type"),
        (MODEL_NAME_AND_ID, "Bundle by specific model instance"),
        (SOURCE_IP, "Bundle by source IP"),
        (HOSTNAME_AND_MODEL_NAME, "Bundle by hostname + model type"),
        (HOSTNAME_AND_MODEL_NAME_AND_ID, "Bundle by hostname + specific model"),
        (SOURCE_IP_AND_MODEL_NAME, "Bundle by IP + model type"),
        (SOURCE_IP_AND_MODEL_NAME_AND_ID, "Bundle by IP + specific model"),
        (SOURCE_IP_AND_HOSTNAME, "Bundle by IP + hostname"),
    ]


class MatchBy:
    """Named constants for match_by field values."""
    ALL = 0  # All rules must match
    ANY = 1  # Any rule can match

    CHOICES = [
        (ALL, "All rules must match"),
        (ANY, "Any rule can match"),
    ]


class BundleMinutes:
    """Preset time window options for bundle_minutes field."""
    DISABLED = 0
    FIVE_MINUTES = 5
    TEN_MINUTES = 10
    FIFTEEN_MINUTES = 15
    THIRTY_MINUTES = 30
    ONE_HOUR = 60
    TWO_HOURS = 120
    SIX_HOURS = 360
    TWELVE_HOURS = 720
    ONE_DAY = 1440

    CHOICES = [
        (DISABLED, "Disabled - don't bundle by time"),
        (FIVE_MINUTES, "5 minutes"),
        (TEN_MINUTES, "10 minutes"),
        (FIFTEEN_MINUTES, "15 minutes"),
        (THIRTY_MINUTES, "30 minutes"),
        (ONE_HOUR, "1 hour"),
        (TWO_HOURS, "2 hours"),
        (SIX_HOURS, "6 hours"),
        (TWELVE_HOURS, "12 hours"),
        (ONE_DAY, "1 day"),
        (None, "No limit - bundle forever"),
    ]


class RuleSet(models.Model, MojoModel):
    """
    A RuleSet represents a collection of rules that are applied to events.
    This model supports categorizing and prioritizing sets of rules to be checked against events.

    Attributes:
        created (datetime): The timestamp when the RuleSet was created.
        modified (datetime): The timestamp when the RuleSet was last modified.
        priority (int): The priority of the RuleSet. Lower numbers indicate higher priority.
        category (str): The category to which this RuleSet belongs.
        name (str): The name of the RuleSet.
        bundle (int): Indicator of whether events should be bundled. 0 means bundling is off.
        bundle_by (int): Defines how events are bundled.
                         0=none, 1=hostname, 2=model, 3=model and hostname.
        match_by (int): Defines the matching behavior for events.
                        0 for all rules must match, 1 for any rule can match.
        handler (str): A field specifying a chain of handlers to process the event,
                       formatted as URL-like strings, e.g.,
                       job://handler_name?param1=value1&param2=value2.
                       Handlers are separated by commas, and they can include
                       different schemes like email or notify.
        metadata (json): A JSON field to store additional metadata about the RuleSet.
    """

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)
    priority = models.IntegerField(default=0, db_index=True)
    category = models.CharField(max_length=124, db_index=True)
    name = models.TextField(default=None, null=True)
    bundle_minutes = models.IntegerField(default=0, null=True, blank=True,
        help_text="Time window in minutes for bundling events (0=disabled/no bundling, >0=time window in minutes)")
    bundle_by = models.IntegerField(default=BundleBy.MODEL_NAME_AND_ID, choices=BundleBy.CHOICES,
        help_text="How to group events into incidents")
    bundle_by_rule_set = models.BooleanField(default=True, help_text="Bundle by rule set")
    match_by = models.IntegerField(default=MatchBy.ALL, choices=MatchBy.CHOICES,
        help_text="Rule matching mode")
    # handler syntax is a url like string that can be chained by commas
    # job://handler_name?param1=value1&param2=value2 | email://user@example.com
    # notify://perm@permission,user@example.com | ticket://?status=open
    # Chains split on ',(job|email|notify|ticket)://'
    handler = models.TextField(default=None, null=True)
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        SEARCH_FIELDS = ["name"]
        VIEW_PERMS = ["view_security", "security"]
        CREATE_PERMS = ["manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security", "security"]
        CAN_DELETE = True

    def run_handler(self, event, incident=None):
        """
        Dispatch all handlers configured on this RuleSet as async jobs.

        Each handler spec in the chain is published as a separate job via
        `jobs.publish()`. The job function (`execute_handler`) loads the
        event/incident and runs the handler in the background.

        Args:
            event (Event): The event that triggered this handler.
            incident (Incident|None): The incident created for this event (if any).

        Returns:
            bool: True if at least one job was published, False otherwise.
        """
        if not self.handler:
            return False

        try:
            from mojo.apps import jobs

            specs = re.split(r',(?=(?:job|email|sms|notify|ticket|block|llm)://)', self.handler.strip())
            published = False

            for spec in filter(None, [s.strip() for s in specs]):
                handler_url = urlparse(spec)
                if handler_url.scheme not in ("job", "email", "sms", "notify", "block", "ticket", "llm"):
                    continue

                payload = {
                    "handler_spec": spec,
                    "event_id": event.pk,
                    "incident_id": incident.pk if incident else None,
                }
                try:
                    channel = "incident_handlers" if "incident_handlers" in jobs.JOB_CHANNELS else "default"
                    jobs.publish(
                        "mojo.apps.incident.handlers.event_handlers.execute_handler",
                        payload,
                        channel=channel,
                    )
                    published = True
                except Exception:
                    logger.exception("Failed to publish handler job for %s", spec)
                    # Record failure in history so admins can see it
                    if incident:
                        incident.add_history(f"handler:{handler_url.scheme}",
                            note=f"Handler {spec} failed to publish")

            return published
        except Exception:
            logger.exception("Error dispatching handlers for ruleset %s", self.pk)
            return False

    def _create_ticket_from_handler(self, event, incident, handler_url, params):
        """
        Create a Ticket from a 'ticket://' handler. Example:
        ticket://?status=open&priority=8&title=Investigate&category=security

        Supported params:
        - title, description, status, priority, category, assignee (user id)
        """
        try:
            from django.contrib.auth import get_user_model
            from mojo.apps.incident.models import Ticket
        except Exception:
            return False

        title = params.get("title") or (event.title or (f"Incident {incident.id}" if incident else "Auto Ticket"))
        description = params.get("description") or (event.details or "")
        status = params.get("status", "open")
        category = params.get("category", "incident")

        # Priority: explicit param -> event.level -> 1
        try:
            priority = int(params.get("priority", event.level if getattr(event, "level", None) is not None else 1))
        except Exception:
            priority = 1

        # Assignee (optional, by user id)
        assignee = None
        assignee_param = params.get("assignee")
        if assignee_param:
            try:
                User = get_user_model()
                assignee = User.objects.filter(id=int(assignee_param)).first()
            except Exception:
                assignee = None

        try:
            ticket = Ticket.objects.create(
                title=title,
                description=description,
                status=status,
                priority=priority,
                category=category,
                assignee=assignee,
                incident=incident,
                metadata={**getattr(event, "metadata", {})},
            )
            return True if ticket else False
        except Exception:
            return False

    @classmethod
    def _create_ruleset(cls, category, name, rules, **kwargs):
        """Helper to create a RuleSet with its Rules if it doesn't exist."""
        ruleset, created = cls.objects.get_or_create(
            category=category,
            name=name,
            defaults=kwargs,
        )
        if created:
            for i, rule_data in enumerate(rules):
                rule_data.setdefault("index", i)
                Rule.objects.create(parent=ruleset, **rule_data)
        return ruleset, created

    @classmethod
    def ensure_default_rules(cls):
        """
        Create all default RuleSets across categories.
        Safe to call multiple times — each method uses get_or_create.
        """
        cls.ensure_ossec_rules()
        cls.ensure_bouncer_rules()
        cls.ensure_auth_rules()
        cls.ensure_health_rules()
        cls.ensure_catchall_rules()

    @classmethod
    def ensure_catchall_rules(cls):
        """
        Create a catch-all RuleSet that matches any event without a specific ruleset.
        Uses category="*" as a sentinel — Event.publish() falls back to this
        after scope and category lookups both return None.
        Safe to call multiple times — uses get_or_create.
        """
        cls._create_ruleset(
            category="*",
            name="Catch-All - Default Bundling",
            priority=9999,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=BundleMinutes.THIRTY_MINUTES,
            rules=[
                {"name": "Level >= 1", "field_name": "level",
                 "comparator": ">=", "value": "1", "value_type": "int"},
            ],
        )

    @classmethod
    def ensure_ossec_rules(cls):
        """
        Create default OSSEC RuleSets and Rules.
        Safe to call multiple times — uses get_or_create.

        Philosophy:
        - Events = audit trail (everything gets recorded)
        - Incidents = bundled events worth reviewing (avoid noise)
        - Tickets = only for unusual things requiring human action

        Most OSSEC noise (brute force, bot scanning) just gets blocked and forgotten.
        No incidents or tickets for routine attacks.

        Priority order (lower = checked first):
          1  - Known bot/scanner patterns → block, no incident
          5  - SSH brute force → block, no incident
         10  - Web attack 31104 → block, no incident
         50  - Critical severity (level >= 12) → block + incident (unusual, worth reviewing)
        """

        # 1) Known bot/scanner URL patterns — block and ignore
        # Matches ANY rule (bots probing for php, asp, git, wordpress, etc.)
        cls._create_ruleset(
            category="ossec",
            name="OSSEC - Bot/Scanner URL Patterns",
            priority=1,
            match_by=MatchBy.ANY,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=0,
            handler="block://?ttl=600&fleet_wide=1",
            rules=[
                {"name": "Suspicious URL extensions",
                 "field_name": "http_url",
                 "comparator": "regex",
                 "value": r"\.php\d*\b|\.git[x]?\b|\.asp[x]?\b|\.env[x]?\b|\bcgi-bin\b|wp-content\b|wlwmanifest\b|locale\.json\b|\.jsp\b|\.cfm\b|\.pl\b|\.cgi[x]?\b|dns\-query\b",
                 "value_type": "str"},
                {"name": "Suspicious URL paths",
                 "field_name": "http_url",
                 "comparator": "regex",
                 "value": r"/git/\b|/wlwmanifest\.xml\b|/\.well-known/security\.txt|/autodiscover/|/remote/login|/owa/|/ecp/",
                 "value_type": "str"},
                {"name": "Suspicious in details",
                 "field_name": "details",
                 "comparator": "regex",
                 "value": r"\.php\d*\b|\.git[x]?\b|\.asp[x]?\b|\.env[x]?\b|\bcgi-bin\b|wp-content\b|wlwmanifest\b|locale\.json\b|\.jsp\b|\.cfm\b|\.pl\b|\.cgi[x]?\b|dns\-query\b",
                 "value_type": "str"},
            ],
        )

        # 2) SSH Brute Force — block and ignore, this is just noise
        cls._create_ruleset(
            category="ossec",
            name="OSSEC - SSH Brute Force",
            priority=5,
            match_by=MatchBy.ANY,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=0,
            handler="block://?ttl=3600&fleet_wide=1",
            rules=[
                {"name": "Max auth attempts (5758)",
                 "field_name": "rule_id",
                 "comparator": "==", "value": "5758", "value_type": "int"},
                {"name": "Brute force (5712)",
                 "field_name": "rule_id",
                 "comparator": "==", "value": "5712", "value_type": "int"},
                {"name": "Multiple failures (5720)",
                 "field_name": "rule_id",
                 "comparator": "==", "value": "5720", "value_type": "int"},
                {"name": "Repeated auth failure (5551)",
                 "field_name": "rule_id",
                 "comparator": "==", "value": "5551", "value_type": "int"},
            ],
        )

        # 3) Web Attack Detection (rule 31104) — block and ignore
        cls._create_ruleset(
            category="ossec",
            name="OSSEC 31104 - Web Attack Detection",
            priority=10,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=0,
            handler="block://?ttl=600&fleet_wide=1",
            rules=[
                {"name": "Rule ID is 31104", "field_name": "rule_id",
                 "comparator": "==", "value": "31104", "value_type": "int"},
            ],
        )

        # 4) Critical Severity — level 12+ is unusual, create incident for review
        # This is the only default that creates an incident — something unexpected happened
        cls._create_ruleset(
            category="ossec",
            name="OSSEC - Critical Severity",
            priority=50,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=60,
            handler="block://?ttl=3600&fleet_wide=1",
            rules=[
                {"name": "Severity >= 12", "field_name": "level",
                 "comparator": ">=", "value": "12", "value_type": "int"},
            ],
        )

    @classmethod
    def ensure_auth_rules(cls):
        """
        Create default RuleSets for authentication security events.
        Safe to call multiple times — uses get_or_create.

        Auth events that indicate automated probing get IP-blocked.
        Single wrong passwords and MFA typos are noise (below incident threshold).
        """

        # Credential stuffing — unknown usernames at level 8.
        # If the username doesn't exist, there's no legitimate reason to keep trying.
        cls._create_ruleset(
            category="login:unknown",
            name="Auth - Credential Stuffing",
            priority=5,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=15,
            handler="block://?ttl=1800&fleet_wide=1",
            rules=[
                {"name": "Level >= 8", "field_name": "level",
                 "comparator": ">=", "value": "8", "value_type": "int"},
            ],
        )

        # Bouncer token abuse — replay, IP mismatch, expired reuse.
        # These are deliberate probing attempts, not accidents.
        cls._create_ruleset(
            category="security:bouncer:token_invalid",
            name="Auth - Bouncer Token Abuse",
            priority=5,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=30,
            handler="block://?ttl=1800&fleet_wide=1",
            rules=[
                {"name": "Level >= 7", "field_name": "level",
                 "comparator": ">=", "value": "7", "value_type": "int"},
            ],
        )

    @classmethod
    def ensure_health_rules(cls):
        """
        Create default RuleSets for infrastructure health events.
        Safe to call multiple times — uses get_or_create.

        IMPORTANT: Health events are NOT attacks — never block IPs.
        The right response is notify humans and create tickets for critical issues.
        """

        # Runner down — critical, needs immediate human attention.
        # A dead job runner means async work (handlers, learning, broadcasts) is stalled.
        cls._create_ruleset(
            category="system:health:runner",
            name="Health - Runner Down",
            priority=1,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.HOSTNAME,
            bundle_minutes=30,
            handler="notify://perm@manage_security,ticket://?priority=9",
            rules=[
                {"name": "Level >= 10", "field_name": "level",
                 "comparator": ">=", "value": "10", "value_type": "int"},
            ],
        )

        # Scheduler missing — critical, no cron jobs running.
        # Health checks themselves will stop if the scheduler is gone.
        cls._create_ruleset(
            category="system:health:scheduler",
            name="Health - Scheduler Missing",
            priority=1,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.NONE,
            bundle_minutes=60,
            handler="notify://perm@manage_security,ticket://?priority=9",
            rules=[
                {"name": "Level >= 10", "field_name": "level",
                 "comparator": ">=", "value": "10", "value_type": "int"},
            ],
        )

        # TCP connection overload — could be connection leak, DDoS, or load spike.
        # Notify but don't create a ticket — often self-resolving.
        cls._create_ruleset(
            category="system:health:tcp",
            name="Health - TCP Connection Overload",
            priority=5,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.HOSTNAME,
            bundle_minutes=30,
            handler="notify://perm@manage_security",
            rules=[
                {"name": "Level >= 8", "field_name": "level",
                 "comparator": ">=", "value": "8", "value_type": "int"},
            ],
        )

    @classmethod
    def ensure_bouncer_rules(cls):
        """
        Create default RuleSets for bouncer events.
        Safe to call multiple times — uses get_or_create.

        Bouncer events that indicate confirmed malicious activity should
        escalate to firewall-level IP blocks. Medium-confidence blocks
        (score 60-79) are left for LLM/human triage.
        """

        # Honeypot credential stuffing — always block.
        # If someone POSTs credentials to a decoy page, they're malicious.
        cls._create_ruleset(
            category="security:bouncer:honeypot_post",
            name="Bouncer - Honeypot Credential Stuffing",
            priority=1,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=30,
            handler="block://?ttl=3600&fleet_wide=1",
            rules=[
                {"name": "Level >= 9", "field_name": "level",
                 "comparator": ">=", "value": "9", "value_type": "int"},
            ],
        )

        # Coordinated bot campaign — block longer + notify admin.
        # Campaign detection means 5+ distinct blocks with the same signal pattern.
        cls._create_ruleset(
            category="security:bouncer:campaign",
            name="Bouncer - Bot Campaign Detection",
            priority=1,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=60,
            handler="block://?ttl=86400&fleet_wide=1,notify://perm@manage_security",
            rules=[
                {"name": "Level >= 10", "field_name": "level",
                 "comparator": ">=", "value": "10", "value_type": "int"},
            ],
        )

        # High-confidence bouncer block (score 80+) — block IP.
        # Score 60-79 is handled by LLM/human triage (no rule created).
        cls._create_ruleset(
            category="security:bouncer:block",
            name="Bouncer - High Confidence Bot Block",
            priority=1,
            match_by=MatchBy.ALL,
            bundle_by=BundleBy.SOURCE_IP,
            bundle_minutes=30,
            handler="block://?ttl=3600&fleet_wide=1",
            rules=[
                {"name": "Risk score >= 80", "field_name": "risk_score",
                 "comparator": ">=", "value": "80", "value_type": "int"},
            ],
        )

    def check_rules(self, event):
        """
        Checks if an event satisfies the rules in this RuleSet based
        on the match_by configuration.

        Args:
            event (Event): The event to check against the RuleSet.

        Returns:
            bool: True if the event matches the RuleSet, False otherwise.
        """
        if self.match_by == MatchBy.ALL:
            return self.check_all_match(event)
        return self.check_any_match(event)

    def check_all_match(self, event):
        """
        Checks if an event satisfies all rules in this RuleSet.

        Args:
            event (Event): The event to check.

        Returns:
            bool: True if the event matches all rules, False otherwise.
        """
        if not self.rules.exists():
            return False
        return all(rule.check_rule(event) for rule in self.rules.order_by("index"))

    def check_any_match(self, event):
        """
        Checks if an event satisfies any rule in this RuleSet.

        Args:
            event (Event): The event to check.

        Returns:
            bool: True if the event matches any rule, False otherwise.
        """
        if not self.rules.exists():
            return False
        return any(rule.check_rule(event) for rule in self.rules.order_by("index"))

    @classmethod
    def check_by_category(cls, category, event):
        """
        Iterates over RuleSets in a category ordered by priority, checking
        if the event satisfies any of the RuleSets.

        Args:
            category (str): The category of the RuleSets to check.
            event (Event): The event to check.

        Returns:
            RuleSet: The first RuleSet that matches the event, or None if no matches are found.
        """
        for rule_set in cls.objects.filter(category=category).order_by("priority"):
            if rule_set.check_rules(event):
                return rule_set
        return None


class Rule(models.Model, MojoModel):
    """
    A Rule represents a single condition that can be checked against an event.
    Each rule belongs to a specific RuleSet and defines how to compare event data fields.

    Attributes:
        created (datetime): The timestamp when the Rule was created.
        modified (datetime): The timestamp when the Rule was last modified.
        parent (RuleSet): The RuleSet to which this Rule belongs.
        name (str): The name of the Rule.
        index (int): The order in which this Rule should be checked within its RuleSet.
        comparator (str): The operation used to compare the event field value with a target value.
        field_name (str): The name of the field in the event to check against.
        value (str): The target value to compare the event field value with.
        value_type (str): The type of the target value (e.g., int, float).
        is_required (int): Indicates if this Rule is mandatory for an event to match. 0=no, 1=yes.
    """

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)
    parent = models.ForeignKey(RuleSet, on_delete=models.CASCADE, related_name="rules")
    name = models.TextField(default=None, null=True)
    index = models.IntegerField(default=0, db_index=True)
    comparator = models.CharField(max_length=32, default="==")
    field_name = models.CharField(max_length=124, default=None, null=True)
    value = models.TextField(default="")
    value_type = models.CharField(max_length=10, default="int")
    is_required = models.IntegerField(default=0)  # 0=no 1=yes

    class RestMeta:
        SEARCH_FIELDS = ["details"]
        VIEW_PERMS = ["view_security", "security"]
        CREATE_PERMS = ["manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security", "security"]
        CAN_DELETE = True

    def check_rule(self, event):
        """
        Checks if a field in the event matches the criteria defined in this Rule.

        Args:
            event (Event): The event to check.

        Returns:
            bool: True if the event field matches the criteria, False otherwise.
        """
        field_value = event.metadata.get(self.field_name, None)
        if field_value is None:
            field_value = getattr(event, self.field_name, None)
        if field_value is None:
            return False

        comp_value = self.value
        field_value, comp_value = self._convert_values(field_value, comp_value)

        if field_value is None or comp_value is None:
            return False

        return self._compare(field_value, comp_value)

    def _convert_values(self, field_value, comp_value):
        """
        Converts the field and comparison values to the appropriate types.

        Args:
            field_value: The value from the event to be converted.
            comp_value: The value defined in the Rule for comparison.

        Returns:
            tuple: A tuple containing the converted field and comparison values.
        """
        if self.comparator != "contains":
            try:
                if self.value_type == "int":
                    return int(field_value), int(comp_value)
                elif self.value_type == "float":
                    return float(field_value), float(comp_value)
                elif self.value_type == "bool":
                    return bool(field_value), bool(comp_value)
                elif self.value_type == "str":
                    return str(field_value), str(comp_value)
            except ValueError:
                return None, None
        return field_value, comp_value

    def _compare(self, field_value, comp_value):
        """
        Compares the field value to the comparison value using the specified comparator.

        Args:
            field_value: The value from the event to compare.
            comp_value: The target value defined in the Rule for comparison.

        Returns:
            bool: True if the comparison is successful, False otherwise.
        """
        comparators = {
            "==": field_value == comp_value,
            "eq": field_value == comp_value,
            ">": field_value > comp_value,
            ">=": field_value >= comp_value,
            "<": field_value < comp_value,
            "<=": field_value <= comp_value,
            "contains": str(comp_value) in str(field_value),
            "regex": re.search(str(comp_value), str(field_value), re.IGNORECASE) is not None,
        }
        return comparators.get(self.comparator, False)
