"""Metrics domain tools — fetch metrics, system health, incident trends."""
from mojo.helpers import dates


def _tool_fetch_metrics(params, user):
    from mojo.apps import metrics

    slugs = params.get("slugs", [])
    if not slugs:
        return {"error": "At least one slug is required"}

    granularity = params.get("granularity", "hours")
    account = params.get("account", "public")
    dt_start = None
    dt_end = None

    if params.get("dt_start"):
        dt_start = dates.parse(params["dt_start"])
    if params.get("dt_end"):
        dt_end = dates.parse(params["dt_end"])

    if len(slugs) == 1:
        slugs = slugs[0]

    records = metrics.fetch(
        slugs, dt_start=dt_start, dt_end=dt_end,
        granularity=granularity, account=account,
        with_labels=True, allow_empty=True,
    )
    return records


def _tool_get_system_health(params, user):
    """Aggregate cross-domain health stats."""
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Incident, Event
    from mojo.apps.jobs.models import Job
    from django.db.models import Count

    now_minus_1h = dates.subtract(minutes=60)
    now_minus_24h = dates.subtract(minutes=1440)

    # Active users (last_activity in last hour)
    active_users = User.objects.filter(
        last_activity__gte=now_minus_1h, is_active=True
    ).count()

    # Open incidents
    open_incidents = Incident.objects.filter(
        status__in=["new", "open", "investigating"]
    ).count()

    # Events in last hour
    events_1h = Event.objects.filter(created__gte=now_minus_1h).count()

    # Job queue
    pending_jobs = Job.objects.filter(status="pending").count()
    running_jobs = Job.objects.filter(status="running").count()
    failed_24h = Job.objects.filter(
        status="failed", created__gte=now_minus_24h
    ).count()

    return {
        "active_users_1h": active_users,
        "open_incidents": open_incidents,
        "events_last_hour": events_1h,
        "pending_jobs": pending_jobs,
        "running_jobs": running_jobs,
        "failed_jobs_24h": failed_24h,
    }


def _tool_get_incident_trends(params, user):
    """Incident and event counts over recent time periods for comparison."""
    from mojo.apps.incident.models import Incident, Event

    periods = [
        ("last_1h", 60),
        ("last_6h", 360),
        ("last_24h", 1440),
        ("last_7d", 10080),
    ]

    result = {}
    for label, minutes in periods:
        since = dates.subtract(minutes=minutes)
        result[label] = {
            "incidents": Incident.objects.filter(created__gte=since).count(),
            "events": Event.objects.filter(created__gte=since).count(),
        }

    # Category breakdown for last 24h
    from django.db.models import Count
    since_24h = dates.subtract(minutes=1440)
    categories = list(
        Event.objects.filter(created__gte=since_24h)
        .values("category")
        .annotate(count=Count("id"))
        .order_by("-count")[:20]
    )
    result["categories_24h"] = categories

    return result


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "fetch_metrics",
        "description": "Fetch time-series metrics data for given slugs and date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slugs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of metric slugs to fetch",
                },
                "dt_start": {"type": "string", "description": "Start date (ISO format, optional)"},
                "dt_end": {"type": "string", "description": "End date (ISO format, optional)"},
                "granularity": {"type": "string", "description": "Data granularity (hours, days, months)", "default": "hours"},
                "account": {"type": "string", "description": "Account scope (public, global, group-<id>)", "default": "public"},
            },
            "required": ["slugs"],
        },
        "handler": _tool_fetch_metrics,
        "permission": "view_admin",
    },
    {
        "name": "get_system_health",
        "description": "Overview of system health: active users, job queue depth, error rates, open incident counts.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": _tool_get_system_health,
        "permission": "view_admin",
    },
    {
        "name": "get_incident_trends",
        "description": "Incident and event trends over time (1h, 6h, 24h, 7d) with category breakdown.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": _tool_get_incident_trends,
        "permission": "view_security",
    },
]
