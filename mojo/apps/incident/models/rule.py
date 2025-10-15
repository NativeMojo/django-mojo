from django.db import models
from mojo.models import MojoModel
from urllib.parse import urlparse, parse_qs
from mojo.apps.incident.handlers import TaskHandler, EmailHandler, NotifyHandler
import re


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
                       task://handler_name?param1=value1&param2=value2.
                       Handlers are separated by commas, and they can include
                       different schemes like email or notify.
        metadata (json): A JSON field to store additional metadata about the RuleSet.
    """

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)
    priority = models.IntegerField(default=0, db_index=True)
    category = models.CharField(max_length=124, db_index=True)
    name = models.TextField(default=None, null=True)
    bundle_minutes = models.IntegerField(default=0)  # 0=off
    # 0=none, 1=hostname, 2=model_name, 3=model_name and model_id, 4=source_ip
    # 5=hostname and model_name, 6=hostname and model_name and model_id,
    # 7=source_ip and model_name, 8=source_ip and model_name and model_id
    # 9=source_ip and hostname
    bundle_by = models.IntegerField(default=3)
    match_by = models.IntegerField(default=0)  # 0=all, 1=any
    # handler syntax is a url like string that can be chained by commas
    # task://handler_name?param1=value1&param2=value2 | email://user@example.com
    # notify://perm@permission,user@example.com | ticket://?status=open
    # Chains split on ',(task|email|notify|ticket)://'
    handler = models.TextField(default=None, null=True)
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        SEARCH_FIELDS = ["name"]
        VIEW_PERMS = ["view_incidents"]
        CREATE_PERMS = None

    def run_handler(self, event, incident=None):
        """
        Runs one or more handlers configured on this RuleSet.

        Handlers can be chained in the `handler` field. The chain is split on
        ',{scheme}://' where scheme in [task, email, notify, ticket].

        Args:
            event (Event): The event to run the handler for.
            incident (Incident|None): The incident created for this event (if any).

        Returns:
            bool: True if at least one handler was successfully run, False otherwise.
        """
        if not self.handler:
            return False

        success = False
        try:
            # Split on commas that are followed by a known scheme, so commas inside
            # notify recipient lists don't break the chain.
            specs = re.split(r',(?=(?:task|email|notify|ticket)://)', self.handler.strip())

            for spec in filter(None, [s.strip() for s in specs]):
                handler_url = urlparse(spec)
                handler_type = handler_url.scheme
                params = {k: v[0] for k, v in parse_qs(handler_url.query).items()}

                if handler_type == "task":
                    handler = TaskHandler(handler_url.netloc, **params)
                    success = handler.run(event) or success

                elif handler_type == "email":
                    handler = EmailHandler(handler_url.netloc)
                    success = handler.run(event) or success

                elif handler_type == "notify":
                    handler = NotifyHandler(handler_url.netloc)
                    success = handler.run(event) or success

                elif handler_type == "ticket":
                    created = self._create_ticket_from_handler(event, incident, handler_url, params)
                    success = created or success

                else:
                    # Unsupported handler type; skip to next
                    continue

            return success
        except Exception:
            # logger.error(f"Error running handlers for ruleset {self.id}: {e}")
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
    def ensure_default_rules(cls):
        """
        Create or update a set of core default RuleSets and Rules.

        Defaults provided:
        1) OSSEC bundling by source_ip within 10 minutes (level >= 7)
        2) OSSEC high severity auto-ticket (level >= 10)
        """
        # 1) OSSEC: Bundle by source_ip within 10 minutes for level >= 7
        ossec_bundle, created = cls.objects.get_or_create(
            category="ossec",
            name="OSSEC - Bundle by IP + Type",
            defaults={
                "priority": 10,
                "match_by": 0,
                "bundle_by": 8,  # source_ip + model_name + model_id
                "bundle_minutes": 10,
                "handler": None,
            },
        )
        if created:
            try:
                Rule.objects.create(
                    parent=ossec_bundle,
                    name="Category is ossec",
                    field_name="category",
                    comparator="==",
                    value="ossec",
                    value_type="str",
                )
                Rule.objects.create(
                    parent=ossec_bundle,
                    name="Severity >= 7",
                    field_name="level",
                    comparator=">=",
                    value="7",
                    value_type="int",
                )
                Rule.objects.create(
                    parent=ossec_bundle,
                    name="Event type is OSSEC rule",
                    field_name="model_name",
                    comparator="==",
                    value="ossec_rule",
                    value_type="str",
                )
            except Exception:
                # Safe-guard: do not raise on partial rule creation
                pass

        # 2) OSSEC: High severity auto-ticket (creates a ticket on new incident)
        ossec_ticket, created = cls.objects.get_or_create(
            category="ossec",
            name="OSSEC - High severity auto-ticket",
            defaults={
                "priority": 5,
                "match_by": 0,
                "bundle_by": 8,  # source_ip + model_name + model_id
                "bundle_minutes": 15,
                "handler": "ticket://?status=open",
            },
        )
        if created:
            try:
                Rule.objects.create(
                    parent=ossec_ticket,
                    name="Category is ossec",
                    field_name="category",
                    comparator="==",
                    value="ossec",
                    value_type="str",
                )
                Rule.objects.create(
                    parent=ossec_ticket,
                    name="Severity >= 10",
                    field_name="level",
                    comparator=">=",
                    value="10",
                    value_type="int",
                )
                Rule.objects.create(
                    parent=ossec_ticket,
                    name="Event type is OSSEC rule",
                    field_name="model_name",
                    comparator="==",
                    value="ossec_rule",
                    value_type="str",
                )
            except Exception:
                # Safe-guard: do not raise on partial rule creation
                pass

    def check_rules(self, event):
        """
        Checks if an event satisfies the rules in this RuleSet based
        on the match_by configuration.

        Args:
            event (Event): The event to check against the RuleSet.

        Returns:
            bool: True if the event matches the RuleSet, False otherwise.
        """
        if self.match_by == 0:
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
        return all(rule.check_rule(event) for rule in self.rules.all())

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
        return any(rule.check_rule(event) for rule in self.rules.all())

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
    value = models.CharField(max_length=124, default="")
    value_type = models.CharField(max_length=10, default="int")
    is_required = models.IntegerField(default=0)  # 0=no 1=yes

    class RestMeta:
        SEARCH_FIELDS = ["details"]
        VIEW_PERMS = ["view_incidents"]
        CREATE_PERMS = ["manage_incidents"]

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
