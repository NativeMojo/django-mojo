from urllib.parse import parse_qs

NAME = "platform"

# Deterministic CloudWatch fixture. Two EC2 instances, one RDS, one
# ElastiCache — enough to exercise multi-series colour cycling, per-row
# selection, and the "one service is down" partial state.
METRICS_RESOURCES = {
    "ec2": [
        {"id": "i-0a1b2c3d", "name": "mojo-api-a", "state": "running",
         "instance_type": "m6i.large", "private_ip": "10.0.1.11",
         "public_ip": "", "slug": "mojo-api-a"},
        {"id": "i-0e4f5a6b", "name": "mojo-api-b", "state": "running",
         "instance_type": "m6i.large", "private_ip": "10.0.1.12",
         "public_ip": "", "slug": "mojo-api-b"},
    ],
    "rds": [
        {"id": "mojo-prod-postgres", "engine": "postgres 16.3",
         "status": "available", "instance_class": "db.m6g.large",
         "endpoint": "mojo-prod-postgres.internal:5432",
         "slug": "mojo-prod-postgres"},
    ],
    "redis": [
        {"id": "mojo-prod-redis-001", "engine": "redis 7.1",
         "status": "available", "node_type": "cache.t4g.medium",
         "num_nodes": 1, "slug": "mojo-prod-redis-001"},
    ],
}

METRICS_BUCKETS = {"minutes": 60, "hours": 24, "days": 30}


def describe(capabilities):
    values = {"setup": capabilities["setup"], "view": capabilities["view_platform"],
              "manage": capabilities["manage_platform"],
              "security": capabilities["view_platform_security"],
              "advanced": capabilities["view_advanced"],
              "metrics": capabilities["manage_aws"],
              "maintenance": capabilities["manage_aws"]}
    return {"id": NAME, "enabled": any(values.values()), "capabilities": values}


def reset(handler, fixtures, *, setup_state="idle", metrics_state="live",
          deployments_state="mixed", **options):
    handler.setup_operation = fixtures["setup_choice"]() if setup_state == "choice" else None
    handler.metrics_state = metrics_state
    handler.deployments_state = deployments_state
    base = {
        "id": "18180000-0000-4000-8000-000000000001", "sha": "8" * 40,
        "status": "partial", "framework_version": "1.9.0", "source": "github",
        "actor": "github:release-bot", "frozen_roster": ["edge-a-engine", "edge-b-engine"],
        "node_evidence": [{"runner": "edge-a-engine", "state": "proven"}],
        "transitions": [], "links": {}, "detail": {}, "created": "2026-08-10T17:00:00Z",
    }
    if deployments_state == "empty":
        handler.platform_deployments = []
    elif deployments_state == "converged":
        handler.platform_deployments = [{
            **base, "status": "converged", "finished": "2026-08-10T17:05:00Z",
            "node_evidence": [{"runner": "edge-a-engine", "state": "proven"},
                              {"runner": "edge-b-engine", "state": "proven"}]}]
    elif deployments_state == "failed":
        handler.platform_deployments = [{
            **base, "status": "failed", "finished": "2026-08-10T17:04:00Z"}]
    else:
        # mixed: one in-flight partial attempt — the previous default fixture.
        handler.platform_deployments = [dict(base)]


def _metrics_labels(granularity, count):
    if granularity == "minutes":
        return ["{:02d}:{:02d}".format(9 + index // 60, index % 60) for index in range(count)]
    if granularity == "days":
        return ["2026-07-{:02d}".format(12 + index) if index < 20
                else "2026-08-{:02d}".format(index - 19) for index in range(count)]
    return ["{:02d}:00".format(index % 24) for index in range(count)]


def _metrics_series(slug, count, empty=False):
    if empty:
        return [0.0 for _ in range(count)]
    seed = sum(ord(character) for character in slug) % 17
    # A sawtooth rather than noise: the shape is obvious at a glance and it is
    # identical on every run, so a visual diff means the chart actually changed.
    return [round(5.0 + ((index * 3 + seed) % 19) * 2.75, 2) for index in range(count)]


# Providers return the body the browser receives after `api()` unwraps the
# envelope — `_send` adds the {"status": ..., "data": ...} wrapper itself.
def _cloudwatch_resources(handler):
    state = getattr(handler, "metrics_state", "live")
    if state in ("unconfigured", "denied"):
        reason = "credentials_unavailable" if state == "unconfigured" else "denied"
        return 200, {"ec2": [], "rds": [], "redis": [],
                     "degraded": {"ec2": reason, "rds": reason, "redis": reason},
                     "available": False, "reason": reason}
    payload = {key: [dict(row) for row in rows]
               for key, rows in METRICS_RESOURCES.items()}
    degraded = {}
    if state == "partial":
        payload["rds"] = []
        degraded["rds"] = "service_error"
    payload.update({"degraded": degraded, "available": True, "reason": None})
    return 200, payload


def _cloudwatch_fetch(handler, parsed):
    state = getattr(handler, "metrics_state", "live")
    query = parse_qs(parsed.query)
    account = (query.get("account") or ["ec2"])[0]
    granularity = (query.get("granularity") or ["hours"])[0]
    count = METRICS_BUCKETS.get(granularity, 24)
    if state in ("unconfigured", "denied"):
        reason = "credentials_unavailable" if state == "unconfigured" else "denied"
        return 200, {"data": {}, "labels": [], "available": False, "reason": reason}
    if state == "partial" and account == "rds":
        return 200, {"data": {}, "labels": [], "available": False,
                     "reason": "service_error"}
    rows = METRICS_RESOURCES.get(account, [])
    wanted = (query.get("slugs") or [""])[0]
    slugs = [slug for slug in wanted.split(",") if slug] or [row["slug"] for row in rows]
    return 200, {
        "data": {slug: _metrics_series(slug, count, empty=state == "empty")
                 for slug in slugs},
        "labels": _metrics_labels(granularity, count),
        "available": True, "reason": None}


def _platform(handler):
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
        "deployments": section({
            "items": handler.platform_deployments, "limit": 50,
            "framework_pin": {"configured": True, "value": "hold",
                              "mode": "hold", "resolved": "1.9.0"}}),
        "certificates": section({"counts": {"active": 1, "issuing": 1}, "expiring_within_30_days": 0}),
        "security": section({
            "open_incidents": {"count": 0, "items": []},
            "monitoring_delivery": {"present": True},
            "secure_posture": {"controls": {
                "https_redirect": False, "session_cookie_secure": True,
                "csrf_cookie_secure": False, "hsts": False,
            }, "disabled": ["https_redirect", "csrf_cookie_secure", "hsts"]},
        }, "unhealthy"),
        "webapps": section({
            "summary_contract": 1,
            "rollup": {"count": 2, "configured_origins": 2,
                       "current_health": {"healthy": 2},
                       "onboarding": {"not_started": 1, "succeeded": 1},
                       "deployment_keys": {"active": 1, "inactive": 1}},
            "items": [
                {"webapp": {"id": 42, "slug": "mojo-portal"},
                 "address": {"https_origin": "https://portal.nativemojo.com"},
                 "onboarding": {"status": "not_started"},
                 "deployment_key": {"linked": True, "active": True},
                 "current_health": {"status": "healthy", "http_status": 200,
                                    "observed_at": now, "stale_after": "2026-08-10T18:10:00Z"}},
                {"webapp": {"id": 54, "slug": "docs"},
                 "address": {"https_origin": "https://docs.nativemojo.com"},
                 "onboarding": {"status": "succeeded"},
                 "deployment_key": {"linked": False, "active": False},
                 "current_health": {"status": "healthy", "http_status": 200,
                                    "observed_at": now, "stale_after": "2026-08-10T18:10:00Z"}},
            ], "registrar_vs_dns": [], "truncated": False}),
    }}


# One switch per served path. Sibling Platform routes register here rather than
# growing a chain of early returns at the top of the function.
# The overview accepts and ignores the merged lane's ?sections= narrowing —
# fixtures are cheap; the parameter exists for the real server.
ROUTES = {
    "/api/account/admin/platform": lambda handler, parsed: _platform(handler),
    "/api/aws/cloudwatch/resources": lambda handler, parsed: _cloudwatch_resources(handler),
    "/api/aws/cloudwatch/fetch": _cloudwatch_fetch,
}


def get(handler, parsed):
    route = ROUTES.get(parsed.path)
    return route(handler, parsed) if route else None


def post(handler, path, payload):
    prefix = "/api/account/admin/platform/deploy/"
    if not path.startswith(prefix):
        return None
    action = path[len(prefix):]
    row = handler.platform_deployments[0]
    row["status"] = {"retry": "requested", "verify": "verified",
                     "converge": "converged"}.get(action, row["status"])
    return 200, {"schema_version": 1, "queued": action == "retry", "deployment": row}
