"""Security domain tools — incidents, events, tickets, rules, IPs."""
from mojo.helpers import dates


MAX_RESULTS = 50
MAX_MINUTES = 43200  # 30 days


# ---------------------------------------------------------------------------
# Existing tools (with improvements)
# ---------------------------------------------------------------------------

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
# New tools: Rule management
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# New tools: IP management
# ---------------------------------------------------------------------------

def _tool_unblock_ip(params, user):
    from mojo.apps.account.models import GeoLocatedIP

    ip = params["ip"]
    reason = f"[Admin Assistant] {params['reason']}"
    try:
        geo = GeoLocatedIP.objects.get(ip_address=ip)
    except GeoLocatedIP.DoesNotExist:
        return {"error": f"IP {ip} not found"}

    geo.unblock(reason=reason)
    return {"ok": True, "ip": ip, "is_blocked": False}


def _tool_whitelist_ip(params, user):
    from mojo.apps.account.models import GeoLocatedIP

    ip = params["ip"]
    reason = f"[Admin Assistant] {params['reason']}"
    geo, _ = GeoLocatedIP.objects.get_or_create(ip_address=ip)
    geo.whitelist(reason=reason)
    return {"ok": True, "ip": ip, "is_whitelisted": True, "is_blocked": geo.is_blocked}


def _tool_unwhitelist_ip(params, user):
    from mojo.apps.account.models import GeoLocatedIP

    ip = params["ip"]
    try:
        geo = GeoLocatedIP.objects.get(ip_address=ip)
    except GeoLocatedIP.DoesNotExist:
        return {"error": f"IP {ip} not found"}

    geo.unwhitelist()
    return {"ok": True, "ip": ip, "is_whitelisted": False}


def _tool_query_blocked_ips(params, user):
    from mojo.apps.account.models import GeoLocatedIP

    criteria = {"is_blocked": True}
    if params.get("minutes"):
        criteria["blocked_at__gte"] = dates.subtract(minutes=params["minutes"])

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    ips = GeoLocatedIP.objects.filter(**criteria).order_by("-blocked_at")[:limit]

    return [
        {
            "ip": g.ip_address,
            "blocked_at": str(g.blocked_at) if g.blocked_at else None,
            "blocked_until": str(g.blocked_until) if g.blocked_until else "permanent",
            "blocked_reason": g.blocked_reason,
            "block_count": g.block_count,
            "is_whitelisted": g.is_whitelisted,
            "country_code": g.country_code,
        }
        for g in ips
    ]


def _tool_query_ipsets(params, user):
    from mojo.apps.incident.models import IPSet

    criteria = {}
    if params.get("kind"):
        criteria["kind"] = params["kind"]
    if params.get("is_enabled") is not None:
        criteria["is_enabled"] = params["is_enabled"]

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    ipsets = IPSet.objects.filter(**criteria).order_by("name")[:limit]

    return [
        {
            "id": s.pk,
            "name": s.name,
            "kind": s.kind,
            "is_enabled": s.is_enabled,
            "cidr_count": s.cidr_count,
            "source": s.source,
            "last_synced": str(s.last_synced) if s.last_synced else None,
        }
        for s in ipsets
    ]


# ---------------------------------------------------------------------------
# New tools: Incident bulk operations
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    # --- Query tools (view_security) ---
    {
        "name": "query_incidents",
        "description": "Filter incidents by status, priority, date range, category, source IP, hostname, rule_set_id, or model_name. Returns up to 50 incidents.",
        "input_schema": {
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
        "handler": _tool_query_incidents,
        "permission": "view_security",
    },
    {
        "name": "query_events",
        "description": "Filter security events by category, IP, hostname, level, rule_id (OSSEC), incident_id, date range. Returns up to 50 events.",
        "input_schema": {
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
        "handler": _tool_query_events,
        "permission": "view_security",
    },
    {
        "name": "query_event_counts",
        "description": "Aggregate event counts grouped by category or rule_id. Useful for detecting spikes, trends, and measuring noise volume per OSSEC rule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "Look back N minutes (default 60)", "default": 60},
                "source_ip": {"type": "string", "description": "Filter by source IP"},
                "hostname": {"type": "string", "description": "Filter by hostname"},
                "category": {"type": "string", "description": "Filter by event category"},
                "group_by": {"type": "string", "enum": ["category", "rule_id"], "description": "Group counts by 'category' (default) or 'rule_id' (OSSEC metadata)", "default": "category"},
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
        "description": "List rule sets and their configurations, optionally filtered by category or active status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category"},
                "is_active": {"type": "boolean", "description": "Filter by active status"},
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
        "name": "get_incident",
        "description": "Get full details for a specific incident by ID, including metadata, event count, and rule set.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer", "description": "The incident ID"},
            },
            "required": ["incident_id"],
        },
        "handler": _tool_get_incident,
        "permission": "view_security",
    },
    {
        "name": "get_incident_events",
        "description": "Get events bundled into a specific incident, including full metadata (OSSEC rule_id, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer", "description": "The incident ID"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
            "required": ["incident_id"],
        },
        "handler": _tool_get_incident_events,
        "permission": "view_security",
    },
    {
        "name": "get_event",
        "description": "Get full details of a single event by ID, including complete metadata (no truncation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "The event ID"},
            },
            "required": ["event_id"],
        },
        "handler": _tool_get_event,
        "permission": "view_security",
    },
    {
        "name": "get_ruleset",
        "description": "Get full details of a rule set including all child rules (field conditions).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ruleset_id": {"type": "integer", "description": "The rule set ID"},
            },
            "required": ["ruleset_id"],
        },
        "handler": _tool_get_ruleset,
        "permission": "view_security",
    },
    {
        "name": "query_blocked_ips",
        "description": "List currently blocked IPs with block reason, TTL, and block count.",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "Only show IPs blocked within the last N minutes"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_blocked_ips,
        "permission": "view_security",
    },
    {
        "name": "query_ipsets",
        "description": "List bulk IP sets (country blocks, datacenter blocks, abuse lists). Returns metadata only, not CIDR data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["country", "datacenter", "abuse", "custom"], "description": "Filter by IPSet kind"},
                "is_enabled": {"type": "boolean", "description": "Filter by enabled status"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_ipsets,
        "permission": "view_security",
    },
    # --- Mutation tools (manage_security) ---
    {
        "name": "create_rule",
        "description": "Create a new event rule set (created DISABLED for human review). Use to auto-handle recurring event patterns. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
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
                "bundle_by": {"type": "integer", "description": "How to group events: 0=none, 4=source_ip, 2=model_name. Default 4.", "default": 4},
                "bundle_minutes": {"type": "integer", "description": "Time window for bundling (default 30 min)", "default": 30},
                "min_count": {"type": "integer", "description": "Minimum events before triggering (optional)"},
                "window_minutes": {"type": "integer", "description": "Time window for threshold counting (optional)"},
                "reasoning": {"type": "string", "description": "Why this rule is being created"},
            },
            "required": ["name", "category"],
        },
        "handler": _tool_create_rule,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "add_rule_condition",
        "description": "Add a field-level rule condition to an existing rule set. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
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
        "handler": _tool_add_rule_condition,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "update_ruleset",
        "description": "Update fields on an existing rule set. Only provided fields are changed. Use to enable assistant-proposed rules after review. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ruleset_id": {"type": "integer", "description": "The rule set ID to update"},
                "name": {"type": "string", "description": "New name"},
                "handler": {"type": "string", "description": "New handler chain"},
                "bundle_by": {"type": "integer", "description": "New bundle_by value (0=none, 4=source_ip, etc.)"},
                "bundle_minutes": {"type": "integer", "description": "New bundle time window in minutes"},
                "match_by": {"type": "integer", "description": "Rule matching mode (0=ALL must match, 1=ANY can match)"},
                "trigger_count": {"type": "integer", "description": "Event count threshold for handler execution"},
                "trigger_window": {"type": "integer", "description": "Time window in minutes for counting events"},
                "is_active": {"type": "boolean", "description": "Enable or disable the rule set"},
                "priority": {"type": "integer", "description": "Rule set priority (lower = checked first)"},
            },
            "required": ["ruleset_id"],
        },
        "handler": _tool_update_ruleset,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "delete_ruleset",
        "description": "Delete a rule set and all its child rules. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ruleset_id": {"type": "integer", "description": "The rule set ID to delete"},
            },
            "required": ["ruleset_id"],
        },
        "handler": _tool_delete_ruleset,
        "permission": "manage_security",
        "mutates": True,
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
        "name": "bulk_update_incidents",
        "description": "Resolve or ignore multiple incidents at once (max 100 per call). IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of incident IDs to update (max 100)"},
                "status": {"type": "string", "description": "New status for all", "enum": ["investigating", "resolved", "ignored"]},
                "note": {"type": "string", "description": "Reason for the bulk status change"},
            },
            "required": ["incident_ids", "status", "note"],
        },
        "handler": _tool_bulk_update_incidents,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "merge_incidents",
        "description": "Merge source incidents into a target incident. Moves all events from sources to target, then deletes sources. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "integer", "description": "The incident to merge into"},
                "source_ids": {"type": "array", "items": {"type": "integer"}, "description": "Incident IDs to merge from (will be deleted)"},
            },
            "required": ["target_id", "source_ids"],
        },
        "handler": _tool_merge_incidents,
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
        "name": "unblock_ip",
        "description": "Unblock a blocked IP address fleet-wide. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP address to unblock"},
                "reason": {"type": "string", "description": "Reason for unblocking"},
            },
            "required": ["ip", "reason"],
        },
        "handler": _tool_unblock_ip,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "whitelist_ip",
        "description": "Add an IP to the whitelist. Whitelisted IPs are never auto-blocked. Also unblocks the IP if currently blocked. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP address to whitelist"},
                "reason": {"type": "string", "description": "Reason for whitelisting"},
            },
            "required": ["ip", "reason"],
        },
        "handler": _tool_whitelist_ip,
        "permission": "manage_security",
        "mutates": True,
    },
    {
        "name": "unwhitelist_ip",
        "description": "Remove an IP from the whitelist. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP address to remove from whitelist"},
            },
            "required": ["ip"],
        },
        "handler": _tool_unwhitelist_ip,
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
