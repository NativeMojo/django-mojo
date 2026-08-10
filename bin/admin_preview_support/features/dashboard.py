NAME = "dashboard"


def describe(capabilities):
    return {"id": NAME, "enabled": True, "capabilities": {"view": True}}


def reset(handler, fixtures, **options):
    handler.events = []
    handler.dashboard_state = options.get("dashboard_state", "healthy")


def get(handler, parsed):
    if parsed.path != "/api/account/admin/dashboard":
        return None
    now = "2026-08-10T18:00:00Z"
    def source(status="healthy", data=None, reason=None):
        value = {"status": status, "observed_at": now,
                 "stale_after": "2026-08-10T18:10:00Z", "data": data or {}}
        if reason:
            value["reason"] = reason
        return value
    sources = {
        "public_api": source(data={"configured": True, "public_origin": "https://api.nativemojo.com", "probe": {"version": "1.9.0", "http_status": 200}}),
        "fleet": source(data={"runners": [{"runner": "edge-a-engine"}, {"runner": "edge-b-engine"}]}),
        "webapps": source(data={"count": 2, "active_deployment_keys": 2,
                                "onboarding": {"succeeded": 2}}),
        "security": source(data={"open_incidents": {"count": 0}}),
        "last_deployment": source(data={"items": [{"id": "18180000-0000-4000-8000-000000000001", "sha": "8" * 40, "status": "succeeded"}]}),
        "incidents": source(data={"open": 0}),
        "tickets": source(data={"open": 0}),
    }
    overall = "healthy"
    if handler.dashboard_state == "degraded":
        sources["fleet"] = source("degraded", {"runners": [{"runner": "edge-a-engine"}]}, "runner_missing")
        sources["incidents"] = source("degraded", {"open": 1}, "open_attention")
        overall = "degraded"
    elif handler.dashboard_state == "denied":
        for name in ("security", "incidents", "tickets"):
            sources[name] = source("permission_denied", reason="permission_required")
        overall = "healthy"
    elif handler.dashboard_state == "unknown":
        for name in sources:
            sources[name] = source("unknown", reason="collector_unavailable")
        overall = "unknown"
    return 200, {"schema_version": 1, "overall": overall,
                 "observable_sources": sum(row["status"] not in
                     ("permission_denied", "unknown") for row in sources.values()),
                 "sources": sources}
