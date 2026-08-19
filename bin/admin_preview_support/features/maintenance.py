"""Deterministic Maintenance fixtures for the Admin visual preview.

Answers both halves of the page: the AWS pending-upgrade report and the
framework overview. States are chosen so every branch the page can render is
reachable without an AWS account — a near-deadline finding and a finding with
no published deadline, a denied IAM warning, an upgrade already in flight, and
the three framework outcomes.
"""

from urllib.parse import parse_qs

NAME = "maintenance"

FRAMEWORK_INSTALLED = "1.12.3"
FRAMEWORK_LATEST = "1.13.0"

# A postgres instance whose standard support ends inside the danger window, an
# Aurora cluster with a comfortable deadline, and a cache group with none at
# all — ElastiCache publishes no end-of-life dates.
FINDINGS = [
    {"kind": "rds-instance", "resource_id": "mojo-prod-postgres", "engine": "postgres",
     "current_version": "14.11", "available_major": "16.4",
     "deadline": "2026-09-10T00:00:00+00:00", "extended_deadline": None,
     "days_remaining": 23,
     "note": "postgres 14.11 has a major upgrade to 16.4 (current major 14)",
     "release_notes_url": "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Upgrading.html",
     "status": "available", "pending_version": None, "settled": True},
    {"kind": "rds-cluster", "resource_id": "mojo-prod-aurora", "engine": "aurora-postgresql",
     "current_version": "13.12", "available_major": "16.2",
     "deadline": "2027-02-28T00:00:00+00:00", "extended_deadline": None,
     "days_remaining": 194,
     "note": "aurora-postgresql 13.12 has a major upgrade to 16.2",
     "release_notes_url": "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_UpgradeDBCluster.html",
     "status": "available", "pending_version": None, "settled": True},
    {"kind": "elasticache", "resource_id": "mojo-prod-redis", "engine": "redis",
     "current_version": "6.2", "available_major": "7.1",
     "deadline": None, "extended_deadline": None, "days_remaining": None,
     "note": "redis 6.2 has a major upgrade to 7.1; the ElastiCache API publishes no end-of-life date",
     "release_notes_url": "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/VersionManagement.html",
     "status": "available", "pending_version": None,
     "settled": True, "members": 2, "engine_versions": ["6.2"]},
]

DENIED_WARNING = {
    "iam_action": "elasticache:DescribeCacheClusters",
    "aws_code": "AccessDenied",
    "message": ("elasticache:DescribeCacheClusters was denied; version drift "
                "for this API was not inventoried"),
}


# No describe(): the Maintenance lane is published by the platform feature's
# capability mirror, so this provider serves pages and owns no bootstrap entry.
def reset(handler, fixtures, *, maintenance_state="findings", **options):
    handler.maintenance_state = maintenance_state
    handler.maintenance_applied = {}


def _findings(state):
    if state == "denied":
        # The cache API was refused, so its resources were never inventoried.
        return [dict(row) for row in FINDINGS if row["kind"] != "elasticache"]
    if state in ("framework_pinned", "framework_none", "clear"):
        return []
    return [dict(row) for row in FINDINGS]


def _report(handler):
    state = getattr(handler, "maintenance_state", "findings")
    if state == "unavailable":
        return 200, {"schema_version": 1, "status": "unavailable",
                     "reason": "NoCredentialsError: Unable to locate credentials",
                     "region": "us-east-1", "generated_at": "2026-08-18T18:00:00Z",
                     "level": 1, "findings": [], "warnings": [], "scheduled": True}
    findings = _findings(state)
    for row in findings:
        applied = getattr(handler, "maintenance_applied", {}).get(row["resource_id"])
        if applied:
            row.update({"status": "upgrading", "settled": False,
                        "pending_version": row["available_major"]})
    if state == "in_flight" and findings:
        findings[0].update({"status": "upgrading", "settled": False,
                            "pending_version": findings[0]["available_major"]})
    return 200, {
        "schema_version": 1, "status": "ok", "region": "us-east-1",
        "generated_at": "2026-08-18T18:00:00Z",
        "level": 8 if findings else 1,
        "findings": findings,
        "warnings": [dict(DENIED_WARNING)] if state == "denied" else [],
        # "clear" is the calm page: nothing pending AND nothing scanning it.
        "scheduled": state != "clear",
    }


def _status(handler, parsed):
    query = parse_qs(parsed.query)
    resource = (query.get("resource") or [""])[0]
    target = (query.get("target_version") or [""])[0]
    applied = getattr(handler, "maintenance_applied", {}).get(resource)
    if not applied:
        return 200, {"schema_version": 1, "resource": resource, "found": True,
                     "status": "available", "engine_version": target,
                     "engine_versions": [target], "pending_version": None,
                     "settled": True, "upgraded": True, "target_version": target}
    # Two polls of visible work, then the settled answer the fixture was asked
    # for — enough to exercise the waiting copy without a real wait.
    applied["polls"] = applied.get("polls", 0) + 1
    if applied["polls"] < 3:
        return 200, {"schema_version": 1, "resource": resource, "found": True,
                     "status": "upgrading", "engine_version": applied["from"],
                     "engine_versions": [applied["from"]], "pending_version": target,
                     "settled": False, "upgraded": False, "target_version": target}
    upgraded = getattr(handler, "maintenance_state", "findings") != "stalled"
    version = target if upgraded else applied["from"]
    return 200, {"schema_version": 1, "resource": resource, "found": True,
                 "status": "available", "engine_version": version,
                 "engine_versions": [version], "pending_version": None,
                 "settled": True, "upgraded": upgraded, "target_version": target}


def _framework(handler):
    state = getattr(handler, "maintenance_state", "findings")
    pin = {"mode": "latest", "value": None}
    blocked = None
    if state == "framework_pinned":
        pin = {"mode": "pinned", "value": "1.12.0"}
    elif state == "framework_none":
        blocked = "no_converged_deployment"
    elif state == "clear":
        blocked = "update_unavailable"
    if getattr(handler, "infrastructure_mode", "managed") == "external":
        # Mirrors framework_overview: the mode overrides every other block, and
        # the version facts stay truthful.
        blocked = "infrastructure_external"
    return 200, {
        "schema_version": 1,
        "installed": FRAMEWORK_INSTALLED,
        "latest": FRAMEWORK_INSTALLED if state == "clear" else FRAMEWORK_LATEST,
        "checked_at": "2026-08-18T17:55:00Z", "source": "pypi",
        # framework_version.status() is pin-aware: a pinned fleet is never
        # offered an update even when PyPI is ahead, so the fixture must not
        # model update_available=true alongside a pin.
        "update_available": state not in ("clear", "framework_pinned"),
        "pin": pin, "can_update": blocked is None, "blocked_reason": blocked,
    }


ROUTES = {
    "/api/aws/maintenance/versions": lambda handler, parsed: _report(handler),
    "/api/aws/maintenance/status": _status,
    "/api/account/admin/platform/framework": lambda handler, parsed: _framework(handler),
}


def get(handler, parsed):
    route = ROUTES.get(parsed.path)
    return route(handler, parsed) if route else None


def post(handler, path, payload):
    if path == "/api/aws/maintenance/apply":
        resource = payload.get("resource")
        target = payload.get("target_version")
        current = next((row["current_version"] for row in FINDINGS
                        if row["resource_id"] == resource), "")
        handler.maintenance_applied[resource] = {"from": current, "polls": 0}
        return 200, {"schema_version": 1, "requested": True, "resource": resource,
                     "kind": payload.get("kind"), "from_version": current,
                     "target_version": target,
                     "apply_immediately": bool(payload.get("apply_immediately"))}
    if path == "/api/account/admin/platform/framework/update":
        return 200, {"schema_version": 1, "requested": True,
                     "version": payload.get("version"),
                     "cleared_pin": None,
                     "deployment": {"id": "18180000-0000-4000-8000-000000000002",
                                    "sha": "9" * 40, "status": "requested"}}
    return None
