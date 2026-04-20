"""
Discovery tools — let the LLM (and users) explore what's available.

load_tools (core) is the primary gateway — lists available domains or loads
domain-specific tools into the conversation.

list_tools (non-core) provides a full listing of all tools; available when
the discovery domain is loaded.

Domain-specific discovery tools (list_job_channels, list_event_categories,
list_permissions) are registered under their parent domains so they load
alongside related tools. Metrics discovery tools live in ``tools/metrics.py``.
"""
from mojo.apps.assistant import tool

MAX_RESULTS = 100


@tool(
    name="load_tools",
    domain="discovery",
    permission="view_admin",
    core=True,
    description=(
        "Discover and load domain-specific tools. "
        "Call with no arguments to see available domains with descriptions and tool counts. "
        "Call with a domain name to load that domain's tools for this conversation. "
        "Loaded tools persist for the rest of the conversation. "
        "Available domains: security, jobs, users, groups, metrics, comms, discovery."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Load tools for a single domain.",
            },
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Load tools for multiple domains at once.",
            },
        },
    },
)
def _tool_load_tools(params, user):
    """List available domains or load domain tools."""
    from mojo.apps.assistant import get_available_domains, get_domain_tools_for_user

    domain = params.get("domain")
    domains = params.get("domains")

    # Merge single and list into one list
    requested = []
    if domain:
        requested.append(domain)
    if domains:
        requested.extend(domains)

    if not requested:
        # No domain specified — list available domains
        available = get_available_domains(user)
        return {
            "message": "Available domains. Call load_tools with a domain name to load its tools.",
            "domains": available,
        }

    # Load requested domains
    loaded = {}
    for d in set(requested):
        tools = get_domain_tools_for_user(user, [d])
        if tools:
            loaded[d] = [
                {"name": t["name"], "description": t["description"][:120]}
                for t in tools
            ]
        else:
            loaded[d] = {"note": f"No tools available for domain '{d}' (check permissions)"}

    return {
        "message": "Tools loaded. They are now available for use in this conversation.",
        "loaded": loaded,
    }


@tool(
    name="list_tools",
    domain="discovery",
    permission="view_admin",
    description="List all tools available to you, grouped by domain. Use this when the user asks 'what can you do?' or you need a complete tool listing.",
    input_schema={
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Filter by domain. Omit to see all."},
        },
    },
)
def _tool_list_tools(params, user):
    """Return all tools the current user has access to, grouped by domain."""
    from mojo.apps.assistant import get_registry

    registry = get_registry()
    domain_filter = params.get("domain")

    domains = {}
    for name, entry in registry.items():
        if domain_filter and entry["domain"] != domain_filter:
            continue
        if not user.has_permission(entry["permission"]):
            continue

        d = entry["domain"]
        if d not in domains:
            domains[d] = []
        domains[d].append({
            "name": name,
            "description": entry["definition"]["description"],
            "mutates": entry["mutates"],
            "core": entry["core"],
        })

    return {
        "total_tools": sum(len(tools) for tools in domains.values()),
        "domains": domains,
    }


# ---------------------------------------------------------------------------
# Domain-specific discovery tools — registered under their parent domains
# ---------------------------------------------------------------------------

@tool(
    name="list_job_channels",
    domain="jobs",
    permission="view_jobs",
    description="List all configured job channels and their current queue depth.",
    input_schema={
        "type": "object",
        "properties": {},
    },
)
def _tool_list_job_channels(params, user):
    """List configured job channels and their current queue depth."""
    from mojo.helpers.settings import settings
    from mojo.apps.jobs.models import Job
    from django.db.models import Count

    channels = settings.get("JOBS_CHANNELS", ["default"])

    # Get counts per channel for pending/running
    counts = dict(
        Job.objects.filter(status__in=["pending", "running"])
        .values("channel")
        .annotate(count=Count("id"))
        .values_list("channel", "count")
    )

    return {
        "channels": [
            {
                "name": ch,
                "queue_depth": counts.get(ch, 0),
            }
            for ch in channels
        ],
    }


@tool(
    name="list_event_categories",
    domain="security",
    permission="view_security",
    description="List distinct security event categories seen in the system over a time period.",
    input_schema={
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Look back N minutes (default 10080 = 7 days)", "default": 10080},
        },
    },
)
def _tool_list_event_categories(params, user):
    """List distinct event categories seen in the system."""
    from mojo.apps.incident.models import Event
    from mojo.helpers import dates

    minutes = min(params.get("minutes", 10080), 43200)  # default 7 days, max 30 days
    since = dates.subtract(minutes=minutes)

    categories = list(
        Event.objects.filter(created__gte=since)
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")[:MAX_RESULTS]
    )

    return {
        "period_minutes": minutes,
        "categories": categories,
        "count": len(categories),
    }


@tool(
    name="list_permissions",
    domain="users",
    permission="view_admin",
    description="List all known permission keys from the system's models. Useful for understanding what permissions exist before granting or checking user permissions.",
    input_schema={
        "type": "object",
        "properties": {},
    },
)
def _tool_list_permissions(params, user):
    """List all known permission keys from RestMeta and active users."""
    from django.apps import apps
    from mojo.models import MojoModel

    # Collect permissions from RestMeta across all models
    perm_set = set()
    for app_config in apps.get_app_configs():
        for model in app_config.get_models():
            if not issubclass(model, MojoModel):
                continue
            rest_meta = getattr(model, "RestMeta", None)
            if not rest_meta:
                continue
            for attr in ("VIEW_PERMS", "SAVE_PERMS", "CREATE_PERMS", "DELETE_PERMS"):
                perms = getattr(rest_meta, attr, None) or []
                for p in perms:
                    if p and p not in ("owner", "all", "authenticated", None):
                        perm_set.add(p)

    return {
        "permissions": sorted(perm_set),
        "count": len(perm_set),
    }
