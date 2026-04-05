"""Event query, detail, and count tools."""
from mojo.apps.assistant import tool
from mojo.helpers import dates

MAX_RESULTS = 50
MAX_MINUTES = 43200  # 30 days


@tool(
    name="query_events",
    domain="security",
    permission="view_security",
    description="Filter security events by category, IP, hostname, level, rule_id (OSSEC), incident_id, date range. Returns up to 50 events.",
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Filter by event category"},
            "source_ip": {"type": "string", "description": "Filter by source IP"},
            "hostname": {"type": "string", "description": "Filter by hostname"},
            "level_gte": {"type": "integer", "description": "Minimum event level"},
            "incident_id": {"type": "integer", "description": "Filter by incident ID"},
            "rule_id": {"type": "integer", "description": "Filter by OSSEC rule_id (in event metadata)"},
            "minutes": {"type": "integer", "description": "Look back N minutes (default 60)", "default": 60},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
def _tool_query_events(params, user):
    from mojo.apps.incident.models import Event

    criteria = {}
    if params.get("category"):
        criteria["category"] = params["category"]
    if params.get("source_ip"):
        criteria["source_ip"] = params["source_ip"]
    if params.get("hostname"):
        criteria["hostname"] = params["hostname"]
    if params.get("level_gte"):
        criteria["level__gte"] = params["level_gte"]
    if params.get("incident_id"):
        criteria["incident_id"] = params["incident_id"]
    if params.get("rule_id"):
        criteria["metadata__rule_id"] = params["rule_id"]

    minutes = min(params.get("minutes", 60), MAX_MINUTES)
    criteria["created__gte"] = dates.subtract(minutes=minutes)

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    events = Event.objects.filter(**criteria).order_by("-created")[:limit]

    return [
        {
            "id": e.pk,
            "created": str(e.created),
            "category": e.category,
            "level": e.level,
            "source_ip": e.source_ip,
            "hostname": e.hostname,
            "title": e.title,
            "details": (e.details or "")[:500],
            "incident_id": e.incident_id,
        }
        for e in events
    ]


@tool(
    name="query_event_counts",
    domain="security",
    permission="view_security",
    description="Aggregate event counts grouped by category or rule_id. Useful for detecting spikes, trends, and measuring noise volume per OSSEC rule.",
    input_schema={
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Look back N minutes (default 60)", "default": 60},
            "source_ip": {"type": "string", "description": "Filter by source IP"},
            "hostname": {"type": "string", "description": "Filter by hostname"},
            "category": {"type": "string", "description": "Filter by event category"},
            "group_by": {"type": "string", "enum": ["category", "rule_id"], "description": "Group counts by 'category' (default) or 'rule_id' (OSSEC metadata)", "default": "category"},
        },
    },
)
def _tool_query_event_counts(params, user):
    from mojo.apps.incident.models import Event
    from django.db.models import Count

    minutes = params.get("minutes", 60)
    criteria = {"created__gte": dates.subtract(minutes=minutes)}
    if params.get("source_ip"):
        criteria["source_ip"] = params["source_ip"]
    if params.get("hostname"):
        criteria["hostname"] = params["hostname"]
    if params.get("category"):
        criteria["category"] = params["category"]

    group_by = params.get("group_by", "category")
    if group_by == "rule_id":
        group_field = "metadata__rule_id"
    else:
        group_field = "category"

    counts = (
        Event.objects.filter(**criteria)
        .values(group_field)
        .annotate(count=Count("id"))
        .order_by("-count")[:MAX_RESULTS]
    )
    return list(counts)


@tool(
    name="get_event",
    domain="security",
    permission="view_security",
    description="Get full details of a single event by ID, including complete metadata (no truncation).",
    input_schema={
        "type": "object",
        "properties": {
            "event_id": {"type": "integer", "description": "The event ID"},
        },
        "required": ["event_id"],
    },
)
def _tool_get_event(params, user):
    from mojo.apps.incident.models import Event

    try:
        e = Event.objects.get(pk=params["event_id"])
    except Event.DoesNotExist:
        return {"error": f"Event {params['event_id']} not found"}

    return {
        "id": e.pk,
        "created": str(e.created),
        "category": e.category,
        "level": e.level,
        "scope": e.scope,
        "source_ip": e.source_ip,
        "hostname": e.hostname,
        "uid": e.uid,
        "country_code": e.country_code,
        "title": e.title,
        "details": e.details,
        "model_name": e.model_name,
        "model_id": e.model_id,
        "incident_id": e.incident_id,
        "metadata": e.metadata or {},
    }
