"""
Discovery tools — let the LLM (and users) explore what's available.

These tools answer questions like "what can you do?", "what metrics exist?",
"what job channels are configured?", and "what event categories are there?".
"""

MAX_RESULTS = 100


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

        domain = entry["domain"]
        if domain not in domains:
            domains[domain] = []
        domains[domain].append({
            "name": name,
            "description": entry["definition"]["description"],
            "mutates": entry["mutates"],
        })

    return {
        "total_tools": sum(len(tools) for tools in domains.values()),
        "domains": domains,
    }


def _tool_list_metric_categories(params, user):
    """List all metric categories in a given account scope."""
    from mojo.apps import metrics
    import re

    account = params.get("account", "public")
    if not re.match(r"^(public|global|group-\d+|user-\d+)$", account):
        return {"error": f"Invalid account scope: {account}"}

    try:
        categories = sorted(metrics.get_categories(account=account))
    except Exception:
        return {"categories": [], "note": "Redis not available or no categories found"}

    return {
        "account": account,
        "categories": categories,
        "count": len(categories),
    }


def _tool_list_metric_slugs(params, user):
    """List all metric slugs within a category."""
    from mojo.apps import metrics
    import re

    category = params["category"]
    account = params.get("account", "public")
    if not re.match(r"^(public|global|group-\d+|user-\d+)$", account):
        return {"error": f"Invalid account scope: {account}"}

    try:
        slugs = sorted(metrics.get_category_slugs(category, account=account))
    except Exception:
        return {"slugs": [], "note": "Redis not available or category not found"}

    return {
        "category": category,
        "account": account,
        "slugs": slugs,
        "count": len(slugs),
    }


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


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_tools",
        "description": "List all tools available to you, grouped by domain. Use this when the user asks 'what can you do?' or you need to find the right tool for a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Filter by domain (security, jobs, users, groups, metrics, discovery). Omit to see all."},
            },
        },
        "handler": _tool_list_tools,
        "permission": "view_admin",
    },
    {
        "name": "list_metric_categories",
        "description": "List all metric categories being tracked. Use this to discover what metrics exist before fetching data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account scope (public, global, group-<id>)", "default": "public"},
            },
        },
        "handler": _tool_list_metric_categories,
        "permission": "view_admin",
    },
    {
        "name": "list_metric_slugs",
        "description": "List all metric slugs within a category. Use this to find specific metrics to fetch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "The metric category to list slugs for"},
                "account": {"type": "string", "description": "Account scope (public, global, group-<id>)", "default": "public"},
            },
            "required": ["category"],
        },
        "handler": _tool_list_metric_slugs,
        "permission": "view_admin",
    },
    {
        "name": "list_job_channels",
        "description": "List all configured job channels and their current queue depth.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": _tool_list_job_channels,
        "permission": "view_jobs",
    },
    {
        "name": "list_event_categories",
        "description": "List distinct security event categories seen in the system over a time period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "Look back N minutes (default 10080 = 7 days)", "default": 10080},
            },
        },
        "handler": _tool_list_event_categories,
        "permission": "view_security",
    },
    {
        "name": "list_permissions",
        "description": "List all known permission keys from the system's models. Useful for understanding what permissions exist.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": _tool_list_permissions,
        "permission": "view_admin",
    },
]
