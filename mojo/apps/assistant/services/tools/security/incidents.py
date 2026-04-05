"""Incident query, detail, update, bulk, and merge tools."""
from mojo.apps.assistant import tool
from mojo.helpers import dates

MAX_RESULTS = 50
MAX_MINUTES = 43200  # 30 days


@tool(
    name="query_incidents",
    domain="security",
    permission="view_security",
    description="Filter incidents by status, priority, date range, category, source IP, hostname, rule_set_id, or model_name. Returns up to 50 incidents.",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status (new, open, investigating, resolved, ignored)"},
            "priority": {"type": "integer", "description": "Minimum priority level"},
            "category": {"type": "string", "description": "Filter by category"},
            "source_ip": {"type": "string", "description": "Filter by source IP"},
            "hostname": {"type": "string", "description": "Filter by hostname"},
            "rule_set_id": {"type": "integer", "description": "Filter by rule set ID"},
            "model_name": {"type": "string", "description": "Filter by model name (e.g. 'account.User')"},
            "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
def _tool_query_incidents(params, user):
    from mojo.apps.incident.models import Incident

    criteria = {}
    if params.get("status"):
        criteria["status"] = params["status"]
    if params.get("priority"):
        criteria["priority__gte"] = params["priority"]
    if params.get("category"):
        criteria["category"] = params["category"]
    if params.get("source_ip"):
        criteria["source_ip"] = params["source_ip"]
    if params.get("hostname"):
        criteria["hostname"] = params["hostname"]
    if params.get("rule_set_id"):
        criteria["rule_set_id"] = params["rule_set_id"]
    if params.get("model_name"):
        criteria["model_name"] = params["model_name"]

    minutes = min(params.get("minutes", 1440), MAX_MINUTES)
    criteria["created__gte"] = dates.subtract(minutes=minutes)

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    incidents = Incident.objects.filter(**criteria).order_by("-created")[:limit]

    return [
        {
            "id": i.pk,
            "status": i.status,
            "priority": i.priority,
            "category": i.category,
            "source_ip": i.source_ip,
            "hostname": i.hostname,
            "title": i.title,
            "created": str(i.created),
        }
        for i in incidents
    ]


@tool(
    name="get_incident",
    domain="security",
    permission="view_security",
    description="Get full details for a specific incident by ID, including metadata, event count, and rule set.",
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "integer", "description": "The incident ID"},
        },
        "required": ["incident_id"],
    },
)
def _tool_get_incident(params, user):
    from mojo.apps.incident.models import Incident

    incident_id = params["incident_id"]
    try:
        i = Incident.objects.get(pk=incident_id)
    except Incident.DoesNotExist:
        return {"error": f"Incident {incident_id} not found"}

    return {
        "id": i.pk,
        "status": i.status,
        "priority": i.priority,
        "category": i.category,
        "source_ip": i.source_ip,
        "hostname": i.hostname,
        "title": i.title,
        "details": (i.details or "")[:1000],
        "scope": i.scope,
        "country_code": i.country_code,
        "rule_set_id": i.rule_set_id,
        "metadata": i.metadata or {},
        "created": str(i.created),
        "event_count": i.events.count(),
    }


@tool(
    name="get_incident_events",
    domain="security",
    permission="view_security",
    description="Get events bundled into a specific incident, including full metadata (OSSEC rule_id, etc.).",
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "integer", "description": "The incident ID"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
        "required": ["incident_id"],
    },
)
def _tool_get_incident_events(params, user):
    from mojo.apps.incident.models import Event

    incident_id = params["incident_id"]
    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    events = Event.objects.filter(incident_id=incident_id).order_by("-created")[:limit]

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
            "metadata": e.metadata or {},
        }
        for e in events
    ]


@tool(
    name="get_incident_timeline",
    domain="security",
    permission="view_security",
    description="Get the full history/audit trail for a specific incident.",
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "integer", "description": "The incident ID"},
        },
        "required": ["incident_id"],
    },
)
def _tool_get_incident_timeline(params, user):
    from mojo.apps.incident.models import IncidentHistory

    incident_id = params["incident_id"]
    entries = IncidentHistory.objects.filter(parent_id=incident_id).order_by("created")[:MAX_RESULTS]

    return [
        {
            "id": h.pk,
            "created": str(h.created),
            "kind": h.kind,
            "note": h.note,
            "user_id": h.user_id,
            "state": h.state,
            "priority": h.priority,
        }
        for h in entries
    ]


@tool(
    name="update_incident",
    domain="security",
    permission="manage_security",
    description="Change an incident's status and add a history note. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "integer", "description": "The incident ID"},
            "status": {"type": "string", "description": "New status", "enum": ["investigating", "resolved", "ignored"]},
            "note": {"type": "string", "description": "Reason for the status change"},
        },
        "required": ["incident_id", "status", "note"],
    },
    mutates=True,
)
def _tool_update_incident(params, user):
    from mojo.apps.incident.models import Incident

    incident = Incident.objects.get(pk=params["incident_id"])
    old_status = incident.status
    incident.status = params["status"]
    incident.save(update_fields=["status"])
    incident.add_history("status_changed",
        note=f"[Admin Assistant] {params['note']} (status: {old_status} -> {params['status']})")
    return {"ok": True, "incident_id": incident.pk, "status": params["status"]}


@tool(
    name="bulk_update_incidents",
    domain="security",
    permission="manage_security",
    description="Resolve or ignore multiple incidents at once (max 100 per call). IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "incident_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of incident IDs to update (max 100)"},
            "status": {"type": "string", "description": "New status for all", "enum": ["investigating", "resolved", "ignored"]},
            "note": {"type": "string", "description": "Reason for the bulk status change"},
        },
        "required": ["incident_ids", "status", "note"],
    },
    mutates=True,
)
def _tool_bulk_update_incidents(params, user):
    from mojo.apps.incident.models import Incident

    ids = params.get("incident_ids", [])
    if len(ids) > 100:
        return {"error": "Maximum 100 incidents per call"}

    status = params["status"]
    note = params["note"]
    updated = []
    failed = []

    for pk in ids:
        try:
            incident = Incident.objects.get(pk=pk)
            old_status = incident.status
            incident.status = status
            incident.save(update_fields=["status"])
            incident.add_history("status_changed",
                note=f"[Admin Assistant] {note} (status: {old_status} -> {status})")
            updated.append(pk)
        except Incident.DoesNotExist:
            failed.append(pk)

    return {"updated": updated, "failed": failed, "count": len(updated)}


@tool(
    name="merge_incidents",
    domain="security",
    permission="manage_security",
    description="Merge source incidents into a target incident. Moves all events from sources to target, then deletes sources. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "target_id": {"type": "integer", "description": "The incident to merge into"},
            "source_ids": {"type": "array", "items": {"type": "integer"}, "description": "Incident IDs to merge from (will be deleted)"},
        },
        "required": ["target_id", "source_ids"],
    },
    mutates=True,
)
def _tool_merge_incidents(params, user):
    from mojo.apps.incident.models import Incident

    try:
        target = Incident.objects.get(pk=params["target_id"])
    except Incident.DoesNotExist:
        return {"error": f"Target incident {params['target_id']} not found"}

    source_ids = params.get("source_ids", [])
    if not source_ids:
        return {"error": "No source incident IDs provided"}
    if len(source_ids) > 50:
        return {"error": "Maximum 50 source incidents per merge"}

    target.on_action_merge(source_ids)
    return {"ok": True, "target_id": target.pk, "merged_count": len(source_ids)}
