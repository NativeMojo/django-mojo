"""Security domain tools — incidents, events, tickets, rules, IPs."""
from mojo.helpers import dates


MAX_RESULTS = 50


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

    minutes = params.get("minutes", 1440)
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

    minutes = params.get("minutes", 60)
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


def _tool_query_event_counts(params, user):
    from mojo.apps.incident.models import Event
    from django.db.models import Count

    minutes = params.get("minutes", 60)
    criteria = {"created__gte": dates.subtract(minutes=minutes)}
    if params.get("source_ip"):
        criteria["source_ip"] = params["source_ip"]
    if params.get("hostname"):
        criteria["hostname"] = params["hostname"]

    counts = (
        Event.objects.filter(**criteria)
        .values("category")
        .annotate(count=Count("id"))
        .order_by("-count")[:MAX_RESULTS]
    )
    return list(counts)


def _tool_query_tickets(params, user):
    from mojo.apps.incident.models import Ticket

    criteria = {}
    if params.get("status"):
        criteria["status"] = params["status"]
    if params.get("priority_gte"):
        criteria["priority__gte"] = params["priority_gte"]
    if params.get("category"):
        criteria["category"] = params["category"]

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    tickets = Ticket.objects.filter(**criteria).order_by("-modified")[:limit]

    return [
        {
            "id": t.pk,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "category": t.category,
            "created": str(t.created),
            "modified": str(t.modified),
            "assignee_id": t.assignee_id,
            "incident_id": t.incident_id,
        }
        for t in tickets
    ]


def _tool_query_rulesets(params, user):
    from mojo.apps.incident.models import RuleSet

    criteria = {}
    if params.get("category"):
        criteria["category"] = params["category"]

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    rulesets = RuleSet.objects.filter(**criteria).order_by("-id")[:limit]

    return [
        {
            "id": rs.pk,
            "name": rs.name,
            "category": rs.category,
            "handler": rs.handler,
            "bundle_by": rs.bundle_by,
            "bundle_minutes": rs.bundle_minutes,
            "is_disabled": (rs.metadata or {}).get("disabled", False),
        }
        for rs in rulesets
    ]


def _tool_query_ip_history(params, user):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import Incident

    ip = params["ip"]
    try:
        geo = GeoLocatedIP.objects.get(ip_address=ip)
        ip_data = {
            "ip": geo.ip_address,
            "country_code": geo.country_code,
            "city": geo.city,
            "region": geo.region,
            "threat_level": geo.threat_level,
            "is_blocked": geo.is_blocked,
            "blocked_reason": geo.blocked_reason,
            "blocked_at": str(geo.blocked_at) if geo.blocked_at else None,
            "block_count": geo.block_count,
            "is_whitelisted": geo.is_whitelisted,
        }
    except GeoLocatedIP.DoesNotExist:
        ip_data = {"ip": ip, "found": False}

    incidents = Incident.objects.filter(source_ip=ip).order_by("-created")[:10]
    ip_data["past_incidents"] = [
        {
            "id": i.pk,
            "status": i.status,
            "priority": i.priority,
            "category": i.category,
            "created": str(i.created),
        }
        for i in incidents
    ]
    return ip_data


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


def _tool_update_incident(params, user):
    from mojo.apps.incident.models import Incident

    incident = Incident.objects.get(pk=params["incident_id"])
    old_status = incident.status
    incident.status = params["status"]
    incident.save(update_fields=["status"])
    incident.add_history("status_changed",
        note=f"[Admin Assistant] {params['note']} (status: {old_status} -> {params['status']})")
    return {"ok": True, "incident_id": incident.pk, "status": params["status"]}


def _tool_block_ip(params, user):
    from mojo.apps.account.models import GeoLocatedIP

    ip = params["ip"]
    reason = f"[Admin Assistant] {params['reason']}"
    ttl = params.get("ttl", 3600)

    geo, _ = GeoLocatedIP.objects.get_or_create(ip_address=ip)
    geo.block(reason=reason, ttl=ttl)

    if params.get("incident_id"):
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=params["incident_id"])
            incident.add_history("handler:assistant",
                note=f"[Admin Assistant] Blocked IP {ip}: {params['reason']} (ttl={ttl}s)")
        except Exception:
            pass

    return {"ok": True, "ip": ip, "blocked": True, "ttl": ttl}


def _tool_create_ticket(params, user):
    from mojo.apps.incident.models import Ticket

    incident = None
    if params.get("incident_id"):
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=params["incident_id"])
        except Exception:
            pass

    ticket = Ticket.objects.create(
        title=params["title"],
        description=params["description"],
        priority=params.get("priority", 5),
        category="assistant_review",
        incident=incident,
        user=user,
        metadata={"assistant_created": True},
    )
    return {"ok": True, "ticket_id": ticket.pk}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "query_incidents",
        "description": "Filter incidents by status, priority, date range, category, or source IP. Returns up to 50 incidents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status (new, open, investigating, resolved, ignored)"},
                "priority": {"type": "integer", "description": "Minimum priority level"},
                "category": {"type": "string", "description": "Filter by category"},
                "source_ip": {"type": "string", "description": "Filter by source IP"},
                "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_incidents,
        "permission": "view_security",
    },
    {
        "name": "query_events",
        "description": "Filter security events by category, IP, hostname, level, date range. Returns up to 50 events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by event category"},
                "source_ip": {"type": "string", "description": "Filter by source IP"},
                "hostname": {"type": "string", "description": "Filter by hostname"},
                "level_gte": {"type": "integer", "description": "Minimum event level"},
                "minutes": {"type": "integer", "description": "Look back N minutes (default 60)", "default": 60},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_events,
        "permission": "view_security",
    },
    {
        "name": "query_event_counts",
        "description": "Aggregate event counts grouped by category. Useful for detecting spikes and trends.",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "Look back N minutes (default 60)", "default": 60},
                "source_ip": {"type": "string", "description": "Filter by source IP"},
                "hostname": {"type": "string", "description": "Filter by hostname"},
            },
        },
        "handler": _tool_query_event_counts,
        "permission": "view_security",
    },
    {
        "name": "query_tickets",
        "description": "Filter tickets by status, priority, category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status (open, closed, etc.)"},
                "priority_gte": {"type": "integer", "description": "Minimum priority"},
                "category": {"type": "string", "description": "Filter by category"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_tickets,
        "permission": "view_security",
    },
    {
        "name": "query_rulesets",
        "description": "List rule sets and their configurations, optionally filtered by category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_rulesets,
        "permission": "view_security",
    },
    {
        "name": "query_ip_history",
        "description": "Look up IP reputation, block history, geo info, and past incidents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "The IP address to look up"},
            },
            "required": ["ip"],
        },
        "handler": _tool_query_ip_history,
        "permission": "view_security",
    },
    {
        "name": "get_incident_timeline",
        "description": "Get the full history/audit trail for a specific incident.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer", "description": "The incident ID"},
            },
            "required": ["incident_id"],
        },
        "handler": _tool_get_incident_timeline,
        "permission": "view_security",
    },
    {
        "name": "update_incident",
        "description": "Change an incident's status and add a history note. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer", "description": "The incident ID"},
                "status": {"type": "string", "description": "New status", "enum": ["investigating", "resolved", "ignored"]},
                "note": {"type": "string", "description": "Reason for the status change"},
            },
            "required": ["incident_id", "status", "note"],
        },
        "handler": _tool_update_incident,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "block_ip",
        "description": "Block an IP address fleet-wide. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP address to block"},
                "reason": {"type": "string", "description": "Reason for blocking"},
                "ttl": {"type": "integer", "description": "Block duration in seconds (0=permanent, default 3600)", "default": 3600},
                "incident_id": {"type": "integer", "description": "Associated incident ID (optional)"},
            },
            "required": ["ip", "reason"],
        },
        "handler": _tool_block_ip,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "create_ticket",
        "description": "Create a ticket for human review. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Ticket title"},
                "description": {"type": "string", "description": "Ticket description / analysis"},
                "priority": {"type": "integer", "description": "1-10 priority (default 5)", "default": 5},
                "incident_id": {"type": "integer", "description": "Associated incident ID (optional)"},
            },
            "required": ["title", "description"],
        },
        "handler": _tool_create_ticket,
        "permission": "manage_security",
        "mutates": True,
    },
]
