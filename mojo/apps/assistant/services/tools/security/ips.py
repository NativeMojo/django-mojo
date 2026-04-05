"""IP history, block/unblock, whitelist, and IPSet query tools."""
from mojo.apps.assistant import tool
from mojo.helpers import dates

MAX_RESULTS = 50


@tool(
    name="query_ip_history",
    domain="security",
    permission="view_security",
    description="Look up IP reputation, block history, geo info, and past incidents.",
    input_schema={
        "type": "object",
        "properties": {
            "ip": {"type": "string", "description": "The IP address to look up"},
        },
        "required": ["ip"],
    },
)
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


@tool(
    name="block_ip",
    domain="security",
    permission="manage_security",
    description="Block an IP address fleet-wide. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ip": {"type": "string", "description": "IP address to block"},
            "reason": {"type": "string", "description": "Reason for blocking"},
            "ttl": {"type": "integer", "description": "Block duration in seconds (0=permanent, default 3600)", "default": 3600},
            "incident_id": {"type": "integer", "description": "Associated incident ID (optional)"},
        },
        "required": ["ip", "reason"],
    },
    mutates=True,
)
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


@tool(
    name="unblock_ip",
    domain="security",
    permission="manage_security",
    description="Unblock a blocked IP address fleet-wide. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ip": {"type": "string", "description": "IP address to unblock"},
            "reason": {"type": "string", "description": "Reason for unblocking"},
        },
        "required": ["ip", "reason"],
    },
    mutates=True,
)
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


@tool(
    name="whitelist_ip",
    domain="security",
    permission="manage_security",
    description="Add an IP to the whitelist. Whitelisted IPs are never auto-blocked. Also unblocks the IP if currently blocked. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ip": {"type": "string", "description": "IP address to whitelist"},
            "reason": {"type": "string", "description": "Reason for whitelisting"},
        },
        "required": ["ip", "reason"],
    },
    mutates=True,
)
def _tool_whitelist_ip(params, user):
    from mojo.apps.account.models import GeoLocatedIP

    ip = params["ip"]
    reason = f"[Admin Assistant] {params['reason']}"
    geo, _ = GeoLocatedIP.objects.get_or_create(ip_address=ip)
    geo.whitelist(reason=reason)
    return {"ok": True, "ip": ip, "is_whitelisted": True, "is_blocked": geo.is_blocked}


@tool(
    name="unwhitelist_ip",
    domain="security",
    permission="manage_security",
    description="Remove an IP from the whitelist. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ip": {"type": "string", "description": "IP address to remove from whitelist"},
        },
        "required": ["ip"],
    },
    mutates=True,
)
def _tool_unwhitelist_ip(params, user):
    from mojo.apps.account.models import GeoLocatedIP

    ip = params["ip"]
    try:
        geo = GeoLocatedIP.objects.get(ip_address=ip)
    except GeoLocatedIP.DoesNotExist:
        return {"error": f"IP {ip} not found"}

    geo.unwhitelist()
    return {"ok": True, "ip": ip, "is_whitelisted": False}


@tool(
    name="query_blocked_ips",
    domain="security",
    permission="view_security",
    description="List currently blocked IPs with block reason, TTL, and block count.",
    input_schema={
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Only show IPs blocked within the last N minutes"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
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


@tool(
    name="query_ipsets",
    domain="security",
    permission="view_security",
    description="List bulk IP sets (country blocks, datacenter blocks, abuse lists). Returns metadata only, not CIDR data.",
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["country", "datacenter", "abuse", "custom"], "description": "Filter by IPSet kind"},
            "is_enabled": {"type": "boolean", "description": "Filter by enabled status"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
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
