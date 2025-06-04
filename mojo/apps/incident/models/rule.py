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
    # task://handler_name?param1=value1&param2=value2
    # email://user@example.com
    # notify://perm@permission,user@example.com,task://handler_name?param1=value1&param2=value2
    handler = models.TextField(default=None, null=True)
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        SEARCH_FIELDS = ["name"]
        VIEW_PERMS = ["view_incidents"]
        CREATE_PERMS = None

    def run_handler(self, event, incident=None):
        """
        Runs the handler for this rule.

        Args:
            event (Event): The event to run the handler for.

        Returns:
            bool: True if the handler was successfully run, False otherwise.
        """
        if not self.handler:
            return False
        try:
            handler_url = urlparse(self.handler)
            handler_type = handler_url.scheme
            handler_params = parse_qs(handler_url.query)
            if handler_type == "task":
                handler_name = handler_url.netloc
                handler_params = {k: v[0] for k, v in handler_params.items()}
                handler = TaskHandler(handler_name, **handler_params)
            elif handler_type == "email":
                handler = EmailHandler(handler_url.netloc)
            elif handler_type == "notify":
                handler = NotifyHandler(handler_url.netloc)
            else:
                raise ValueError(f"Unsupported handler type: {handler_type}")
            return handler.run(event)
        except Exception as e:
            # logger.error(f"Error running handler for rule {self.id}: {e}")
            return False

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
