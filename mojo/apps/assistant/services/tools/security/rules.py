"""Rule set query, detail, create, update, delete, and condition tools."""
from mojo.apps.assistant import tool

MAX_RESULTS = 50


@tool(
    name="query_rulesets",
    domain="security",
    permission="view_security",
    description="List rule sets and their configurations, optionally filtered by category or active status.",
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Filter by category"},
            "is_active": {"type": "boolean", "description": "Filter by active status"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
def _tool_query_rulesets(params, user):
    from mojo.apps.incident.models import RuleSet

    criteria = {}
    if params.get("category"):
        criteria["category"] = params["category"]
    if params.get("is_active") is not None:
        criteria["is_active"] = params["is_active"]

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    rulesets = RuleSet.objects.filter(**criteria).order_by("-id")[:limit]

    return [
        {
            "id": rs.pk,
            "name": rs.name,
            "category": rs.category,
            "priority": rs.priority,
            "handler": rs.handler,
            "bundle_by": rs.bundle_by,
            "bundle_minutes": rs.bundle_minutes,
            "match_by": rs.match_by,
            "trigger_count": rs.trigger_count,
            "trigger_window": rs.trigger_window,
            "is_active": rs.is_active,
        }
        for rs in rulesets
    ]


@tool(
    name="get_ruleset",
    domain="security",
    permission="view_security",
    description="Get full details of a rule set including all child rules (field conditions).",
    input_schema={
        "type": "object",
        "properties": {
            "ruleset_id": {"type": "integer", "description": "The rule set ID"},
        },
        "required": ["ruleset_id"],
    },
)
def _tool_get_ruleset(params, user):
    from mojo.apps.incident.models import RuleSet

    try:
        rs = RuleSet.objects.get(pk=params["ruleset_id"])
    except RuleSet.DoesNotExist:
        return {"error": f"RuleSet {params['ruleset_id']} not found"}

    rules = rs.rules.order_by("index")
    return {
        "id": rs.pk,
        "name": rs.name,
        "category": rs.category,
        "priority": rs.priority,
        "handler": rs.handler,
        "bundle_by": rs.bundle_by,
        "bundle_minutes": rs.bundle_minutes,
        "match_by": rs.match_by,
        "trigger_count": rs.trigger_count,
        "trigger_window": rs.trigger_window,
        "is_active": rs.is_active,
        "metadata": rs.metadata or {},
        "rules": [
            {
                "id": r.pk,
                "name": r.name,
                "index": r.index,
                "field_name": r.field_name,
                "comparator": r.comparator,
                "value": r.value,
                "value_type": r.value_type,
            }
            for r in rules
        ],
    }


@tool(
    name="create_rule",
    domain="security",
    permission="manage_security",
    description="Create a new event rule set (created DISABLED for human review). Use to auto-handle recurring event patterns. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Rule name describing the pattern"},
            "category": {"type": "string", "description": "Event category to match"},
            "handler": {"type": "string", "description": "Handler chain (e.g. 'ignore://' or 'block://?ttl=3600,notify://perm@manage_security'). Use 'ignore://' to auto-ignore matching events."},
            "rules": {
                "type": "array",
                "description": "Field match rules (conditions that events must satisfy)",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short description of this rule condition"},
                        "field": {"type": "string", "description": "Event field or metadata key to match (e.g. 'level', 'rule_id', 'source_ip', 'details', 'http_url')"},
                        "comparator": {"type": "string", "enum": ["==", ">", ">=", "<", "<=", "contains", "regex"], "description": "Comparison operator"},
                        "value": {"type": "string", "description": "Value to compare against (always a string, converted per value_type)"},
                        "value_type": {"type": "string", "enum": ["int", "float", "str", "bool"], "description": "Type to convert value to before comparison. Default: str", "default": "str"},
                    },
                    "required": ["field", "comparator", "value"],
                },
            },
            "bundle_by": {
                "type": "integer",
                "description": (
                    "How to group events into one incident. "
                    "0=none (each event = own incident), "
                    "1=hostname, "
                    "2=model_name, "
                    "3=model_name+id, "
                    "4=source_ip, "
                    "5=hostname+model_name, "
                    "6=hostname+model_name+id, "
                    "7=source_ip+model_name, "
                    "8=source_ip+model_name+id, "
                    "9=source_ip+hostname, "
                    "10=group (per-tenant), "
                    "11=group+model_name, "
                    "12=group+model_name+id, "
                    "13=group+source_ip (multi-tenant attack patterns). "
                    "For multi-tenant deployments use 13 so one tenant's flood "
                    "does not drown out signal from another. Otherwise 4 is the "
                    "default for security rules. Default 4."
                ),
                "default": 4,
            },
            "bundle_minutes": {"type": "integer", "description": "Time window for bundling (default 30 min)", "default": 30},
            "min_count": {"type": "integer", "description": "Minimum events before triggering (optional)"},
            "window_minutes": {"type": "integer", "description": "Time window for threshold counting (optional)"},
            "reasoning": {"type": "string", "description": "Why this rule is being created"},
        },
        "required": ["name", "category"],
    },
    mutates=True,
)
def _tool_create_rule(params, user):
    from mojo.apps.incident.models import RuleSet, Rule

    metadata = {
        "assistant_proposed": True,
        "reasoning": params.get("reasoning", ""),
    }

    ruleset = RuleSet.objects.create(
        name=params["name"],
        category=params["category"],
        handler=params.get("handler", ""),
        bundle_by=params.get("bundle_by", 4),
        bundle_minutes=params.get("bundle_minutes", 30),
        trigger_count=params.get("min_count"),
        trigger_window=params.get("window_minutes"),
        is_active=False,
        metadata=metadata,
    )

    for i, rule_data in enumerate(params.get("rules") or []):
        Rule.objects.create(
            parent=ruleset,
            name=rule_data.get("name", ""),
            index=i,
            field_name=rule_data.get("field", ""),
            comparator=rule_data.get("comparator", "=="),
            value=rule_data.get("value", ""),
            value_type=rule_data.get("value_type", "str"),
        )

    return {"ok": True, "ruleset_id": ruleset.pk, "name": params["name"], "disabled": True}


@tool(
    name="add_rule_condition",
    domain="security",
    permission="manage_security",
    description="Add a field-level rule condition to an existing rule set. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ruleset_id": {"type": "integer", "description": "The rule set ID to add the condition to"},
            "name": {"type": "string", "description": "Short description of this condition"},
            "field": {"type": "string", "description": "Event field or metadata key (e.g. 'level', 'rule_id', 'source_ip', 'details')"},
            "comparator": {"type": "string", "enum": ["==", ">", ">=", "<", "<=", "contains", "regex"], "description": "Comparison operator"},
            "value": {"type": "string", "description": "Value to compare against"},
            "value_type": {"type": "string", "enum": ["int", "float", "str", "bool"], "description": "Type to convert value to. Default: str", "default": "str"},
        },
        "required": ["ruleset_id", "field", "comparator", "value"],
    },
    mutates=True,
)
def _tool_add_rule_condition(params, user):
    from mojo.apps.incident.models import RuleSet, Rule

    try:
        ruleset = RuleSet.objects.get(pk=params["ruleset_id"])
    except RuleSet.DoesNotExist:
        return {"error": f"RuleSet {params['ruleset_id']} not found"}

    next_index = ruleset.rules.count()
    rule = Rule.objects.create(
        parent=ruleset,
        name=params.get("name", ""),
        index=next_index,
        field_name=params["field"],
        comparator=params.get("comparator", "=="),
        value=params.get("value", ""),
        value_type=params.get("value_type", "str"),
    )
    return {
        "ok": True,
        "rule_id": rule.pk,
        "ruleset_id": ruleset.pk,
        "index": next_index,
    }


@tool(
    name="update_ruleset",
    domain="security",
    permission="manage_security",
    description="Update fields on an existing rule set. Only provided fields are changed. Use to enable assistant-proposed rules after review. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ruleset_id": {"type": "integer", "description": "The rule set ID to update"},
            "name": {"type": "string", "description": "New name"},
            "handler": {"type": "string", "description": "New handler chain"},
            "bundle_by": {
                "type": "integer",
                "description": (
                    "New bundle_by value. "
                    "0=none, 1=hostname, 2=model_name, 3=model_name+id, "
                    "4=source_ip, 5=hostname+model_name, 6=hostname+model_name+id, "
                    "7=source_ip+model_name, 8=source_ip+model_name+id, "
                    "9=source_ip+hostname, "
                    "10=group, 11=group+model_name, 12=group+model_name+id, "
                    "13=group+source_ip. Use a GROUP_* mode (10-13) for "
                    "multi-tenant deployments so per-tenant signal stays separated."
                ),
            },
            "bundle_minutes": {"type": "integer", "description": "New bundle time window in minutes"},
            "match_by": {"type": "integer", "description": "Rule matching mode (0=ALL must match, 1=ANY can match)"},
            "trigger_count": {"type": "integer", "description": "Event count threshold for handler execution"},
            "trigger_window": {"type": "integer", "description": "Time window in minutes for counting events"},
            "is_active": {"type": "boolean", "description": "Enable or disable the rule set"},
            "priority": {"type": "integer", "description": "Rule set priority (lower = checked first)"},
        },
        "required": ["ruleset_id"],
    },
    mutates=True,
)
def _tool_update_ruleset(params, user):
    from mojo.apps.incident.models import RuleSet

    try:
        rs = RuleSet.objects.get(pk=params["ruleset_id"])
    except RuleSet.DoesNotExist:
        return {"error": f"RuleSet {params['ruleset_id']} not found"}

    updatable = [
        "name", "handler", "bundle_by", "bundle_minutes", "match_by",
        "trigger_count", "trigger_window", "is_active", "priority",
    ]
    changed = []
    for field in updatable:
        if field in params:
            setattr(rs, field, params[field])
            changed.append(field)

    if not changed:
        return {"error": "No fields to update"}

    changed.append("modified")
    rs.save(update_fields=changed)
    return {"ok": True, "ruleset_id": rs.pk, "updated_fields": changed}


@tool(
    name="delete_ruleset",
    domain="security",
    permission="manage_security",
    description="Delete a rule set and all its child rules. IMPORTANT: Always confirm with the user before executing — this is irreversible.",
    input_schema={
        "type": "object",
        "properties": {
            "ruleset_id": {"type": "integer", "description": "The rule set ID to delete"},
        },
        "required": ["ruleset_id"],
    },
    mutates=True,
)
def _tool_delete_ruleset(params, user):
    from mojo.apps.incident.models import RuleSet

    try:
        rs = RuleSet.objects.get(pk=params["ruleset_id"])
    except RuleSet.DoesNotExist:
        return {"error": f"RuleSet {params['ruleset_id']} not found"}

    rule_count = rs.rules.count()
    rs_id = rs.pk
    rs.delete()
    return {"ok": True, "ruleset_id": rs_id, "rules_deleted": rule_count}


@tool(
    name="delete_rule",
    domain="security",
    permission="manage_security",
    description=(
        "Delete a single rule condition from a rule set by rule ID. "
        "Use get_ruleset first to see the rules and their IDs. "
        "IMPORTANT: Always confirm with the user before executing — this is irreversible."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "rule_id": {"type": "integer", "description": "The rule (condition) ID to delete"},
        },
        "required": ["rule_id"],
    },
    mutates=True,
)
def _tool_delete_rule(params, user):
    from mojo.apps.incident.models import Rule

    try:
        rule = Rule.objects.select_related("parent").get(pk=params["rule_id"])
    except Rule.DoesNotExist:
        return {"error": f"Rule {params['rule_id']} not found"}

    ruleset_id = rule.parent_id
    rule_id = rule.pk
    rule.delete()

    remaining = Rule.objects.filter(parent_id=ruleset_id).count()
    return {
        "ok": True,
        "rule_id": rule_id,
        "ruleset_id": ruleset_id,
        "remaining_rules": remaining,
    }
