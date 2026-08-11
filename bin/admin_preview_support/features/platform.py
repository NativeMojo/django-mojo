NAME = "platform"


def describe(capabilities):
    values = {"setup": capabilities["setup"], "view": capabilities["view_platform"],
              "manage": capabilities["manage_platform"],
              "security": capabilities["view_platform_security"],
              "advanced": capabilities["view_advanced"]}
    return {"id": NAME, "enabled": any(values.values()), "capabilities": values}


def reset(handler, fixtures, *, setup_state="idle", **options):
    handler.setup_operation = fixtures["setup_choice"]() if setup_state == "choice" else None
    handler.platform_deployments = [{
        "id": "18180000-0000-4000-8000-000000000001", "sha": "8" * 40,
        "status": "partial", "framework_version": "1.9.0", "source": "github",
        "actor": "github:release-bot", "frozen_roster": ["edge-a-engine", "edge-b-engine"],
        "node_evidence": [{"runner": "edge-a-engine", "state": "proven"}],
        "transitions": [], "links": {}, "detail": {}, "created": "2026-08-10T17:00:00Z",
    }]


def get(handler, parsed):
    if parsed.path != "/api/account/admin/platform":
        return None
    now = "2026-08-10T18:00:00Z"
    section = lambda data, status="healthy", observed_at=now: {
        "status": status, "observed_at": observed_at,
        "stale_after": "2026-08-10T18:10:00Z", "data": data}
    return 200, {"schema_version": 1, "sections": {
        "api": section({"django_mojo_version": "1.9.0", "configured": True},
                       observed_at=1786384800),
        "fleet": section({"channel": "edge", "runners": [{"runner": "edge-a-engine", "alive": True}, {"runner": "edge-b-engine", "alive": True}]}),
        "database": section({"reachable": True, "vendor": "postgresql"}),
        "redis": section({"reachable": True}),
        "deployments": section({"items": handler.platform_deployments, "limit": 50}),
        "certificates": section({"counts": {"active": 1, "issuing": 1}, "expiring_within_30_days": 0}),
        "security": section({"SECURE_SSL_REDIRECT": {"configured": True}}),
        "webapps": section({"summary_contract": 1, "items": [], "registrar_vs_dns": []}),
    }}


def post(handler, path, payload):
    prefix = "/api/account/admin/platform/deploy/"
    if not path.startswith(prefix):
        return None
    action = path[len(prefix):]
    row = handler.platform_deployments[0]
    row["status"] = {"retry": "requested", "verify": "verified",
                     "converge": "converged"}.get(action, row["status"])
    return 200, {"schema_version": 1, "queued": action == "retry", "deployment": row}
