NAME = "dashboard"

NOW = "2026-08-10T18:00:00Z"
STALE = "2026-08-10T18:10:00Z"

PLATFORM_SOURCES = (
    "load_balancer", "compute", "database", "cache", "certificates",
    "public_api", "framework", "last_deployment", "jobs", "sanity")

SANITY_CHECKS = ("django apps", "database", "migrations", "redis", "local request")


def describe(capabilities):
    return {"id": NAME, "enabled": True, "capabilities": {"view": True}}


def reset(handler, fixtures, **options):
    handler.events = []
    handler.dashboard_state = options.get("dashboard_state", "healthy")


def _source(status="healthy", data=None, reason=None, reason_detail=None):
    value = {"status": status, "observed_at": NOW, "stale_after": STALE,
             "data": data or {}}
    if reason:
        value["reason"] = reason
    if reason_detail:
        value["reason_detail"] = reason_detail
    return value


def _healthy_sources():
    return {
        "load_balancer": _source(data={
            "configured": True, "registered": 2, "healthy": 2,
            "elastic_ip_missing": False, "denied": [],
            "balancer": {"name": "mojo-api-nlb", "type": "network",
                         "scheme": "internet-facing", "state": "active",
                         "elastic_ips": ["52.14.9.201"],
                         "addresses": [{"zone": "us-east-2a", "ip": "52.14.9.201",
                                        "allocation_id": "eipalloc-0a1b2c3d"}]},
            "groups": [{"name": "mojo-api", "registered": 2, "healthy": 2,
                        "target_type": "instance", "observed": True}]}),
        "compute": _source(data={
            "source": "target_group", "total": 2, "up": 2,
            "instances": [{"id": "i-0a1b2c3d", "name": "mojo-api-a", "healthy": True},
                          {"id": "i-0e4f5a6b", "name": "mojo-api-b", "healthy": True}]}),
        "database": _source(data={
            "reachable": True, "vendor": "postgresql", "engine": "Postgres",
            "version": "16.3", "drift": None}),
        "cache": _source(data={
            "reachable": True, "engine": "Redis", "version": "7.1",
            "memory_used": "412.5M"}),
        "certificates": _source(data={
            "counts": {"active": 3}, "expiring_within_30_days": 0, "total": 3,
            "active": 3, "failing": 0, "soonest_renew": "2026-09-02T09:00:00Z",
            "soonest_renew_name": "api.nativemojo.com",
            "soonest_expiry": "2026-10-02T09:00:00Z"}),
        "public_api": _source(data={
            "configured": True, "public_origin": "https://api.nativemojo.com",
            "django_mojo_version": "1.12.3", "node_version": "1.9.0",
            "probe": {"ok": True, "version": "1.9.0", "http_status": 200}}),
        "framework": _source(data={
            "installed": "1.12.3", "latest": "1.12.3", "update_available": False,
            "checked_at": NOW, "source": "cache",
            "pin": {"mode": "latest", "value": None}}),
        "last_deployment": _source(data={"items": [{
            "id": "18180000-0000-4000-8000-000000000001", "sha": "8" * 40,
            "status": "converged", "created": NOW, "finished": NOW,
            "actor": "admin:1"}]}),
        # A long-lived queue carries a large all-time failed count; only the
        # one-hour window is allowed to colour the row.
        "jobs": _source(data={
            "scheduler_active": True, "failed_recent": 0,
            "jobs": {"pending": 1, "running": 0, "failed": 11701}}),
        "sanity": _source(data={
            "checks": [{"name": name, "ok": True} for name in SANITY_CHECKS],
            "local_target_source": "configured_static", "migration_check": True}),
        "incidents": _source(data={"open": 0, "oldest_created": None,
                                   "oldest_age_days": None}),
        "tickets": _source(data={"open": 0, "oldest_created": None,
                                 "oldest_age_days": None}),
    }


def _availability(state, message, down=()):
    return {"state": state, "message": message, "down": list(down)}


def get(handler, parsed):
    if parsed.path != "/api/account/admin/dashboard":
        return None
    sources = _healthy_sources()
    availability = _availability("ok", "Everything is running")
    attention = {"message": None}

    if handler.dashboard_state == "degraded":
        # Amber that is explicitly NOT an outage: a major engine upgrade is
        # published and the fleet is one framework release behind.
        sources["database"] = _source(data={
            "reachable": True, "vendor": "postgresql", "engine": "Postgres",
            "version": "16.3",
            "drift": {"available_major": "16.5", "deadline": "2027-02-28T00:00:00Z",
                      "note": "standard support ends"}})
        sources["framework"] = _source("degraded", {
            "installed": "1.12.3", "latest": "1.13.0", "update_available": True,
            "checked_at": NOW, "source": "pypi",
            "pin": {"mode": "latest", "value": None}}, "update_available")
    elif handler.dashboard_state == "down":
        sources["cache"] = _source(
            "unhealthy", {"reachable": False}, "cache_unreachable")
        sources["incidents"] = _source("degraded", {
            "open": 121, "oldest_created": "2026-08-04T18:00:00Z",
            "oldest_age_days": 6}, "open_attention")
        availability = _availability("down", "Elasticache is down", ["Elasticache"])
        attention = {"message": "121 incidents need review."}
    elif handler.dashboard_state == "jobs_stalled":
        # Amber that is explicitly NOT an outage: the scheduler stopped and
        # jobs failed inside the last hour. Availability stays green.
        sources["jobs"] = _source("degraded", {
            "scheduler_active": False, "failed_recent": 4,
            "jobs": {"pending": 37, "running": 0, "failed": 11705}},
            "scheduler_inactive")
    elif handler.dashboard_state == "sanity_failed":
        # One core node check is failing. The Public API row says so in plain
        # words; the headline still reports what customers can reach.
        sources["sanity"] = _source("degraded", {
            "checks": [{"name": name, "ok": name != "migrations"}
                       for name in SANITY_CHECKS],
            "local_target_source": "configured_static",
            "migration_check": False}, "sanity_check_failed")
    elif handler.dashboard_state == "denied":
        for name in PLATFORM_SOURCES:
            sources[name] = _source("permission_denied", reason="permission_required")
        # One source is readable but the platform identity lacks the grant —
        # the collapsed Restricted row must name the action to add.
        sources["load_balancer"] = _source(
            "unknown", reason="provider_denied",
            reason_detail={"iam_action": "elasticloadbalancing:DescribeTargetHealth"})
        availability = _availability(
            "unknown", "Status unknown — no source is reporting.")
    elif handler.dashboard_state == "unknown":
        for name in sources:
            sources[name] = _source("unknown", reason="collector_unavailable")
        availability = _availability(
            "unknown", "Status unknown — no source is reporting.")

    return 200, {"schema_version": 2, "observed_at": NOW,
                 "availability": availability, "attention": attention,
                 "sources": sources}
