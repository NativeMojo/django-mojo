"""Cloud domain — the read tools.

Every tool here mirrors ONE Admin read and calls the same shared service the
endpoint calls, with the same permission tuple. Nothing here mutates, so none
declares an approval gate (passing one without ``mutates=True`` raises at
import). Two tools additionally declare ``authorize=`` because their Admin
twins carry a superuser AND-check the ``permission`` argument cannot express.

Bounding is not optional decoration: ``_dumps_tool_result`` serializes whatever
a handler returns, so every bound here is this module's own. Raw provider
exceptions, account credentials, deploy stderr tails, Elastic IP allocation ids
and unbounded inventory never reach model context.
"""

from mojo.apps.assistant import tool
from mojo.apps.aws.services import capacity as capacity_service
from mojo.apps.aws.services import maintenance as maintenance_service
from mojo.helpers import logit
from mojo.helpers.aws.cloudwatch import ACCOUNT_NAMESPACE, CATEGORY_METRIC

from .common import (
    actor_request, bounded, can_system_admin, interactive_refusal,
    provider_reason, refuse, superuser_refusal,
)


logger = logit.get_logger("assistant", "assistant.log")

# Permission tuples, copied from the mirrored endpoint's
# @md.requires_global_perms(...) so a reviewer can diff them by eye.
DASHBOARD_PERMS = ["view_admin", "manage_users", "manage_settings", "admin"]
PLATFORM_PERMS = ["view_platform", "view_platform_security",
                  "manage_platform", "admin"]
FRAMEWORK_PERMS = ["view_platform", "manage_platform", "admin"]
ADVANCED_PERMS = ["view_advanced", "view_advanced_inventory",
                  "view_advanced_security", "manage_advanced", "admin"]

# The ten sections platform_overview can collect. Naming them is what keeps a
# chat turn from triggering the full roster, whose `webapps` collector fans out
# per-app summary_for + HTTPS probes.
PLATFORM_SECTIONS = ("api", "fleet", "jobs", "sanity", "database", "redis",
                     "deployments", "certificates", "security", "webapps")
MAX_SECTIONS = 4

# The deployment projection. Named rather than generic because these are the
# fields the three deploy actions and their explanations need — and because
# `node_evidence`, `transitions` and `diagnosis` carry `detail.stderr_tail`,
# which exists precisely because the redactor has gaps a credential survives.
DEPLOYMENT_FIELDS = ("id", "sha", "framework_version", "status", "source",
                     "actor", "retry_of", "created", "started", "finished",
                     "duration_seconds", "node_summary", "current_commits",
                     "desired_commit")

MAX_DRIFT_FINDINGS = 20
MAX_RESOURCE_ROWS = 100
MAX_METRIC_SLUGS = 10
MAX_METRIC_BUCKETS = 60
MAX_SETUP_LOG = 20
MAX_HOURS = 168

# Node ceilings for the two tools whose own documented caps are WIDER than
# bounded()'s default 40-item / 400-node envelope defaults. Without these the
# projector would silently re-truncate a result this module already capped —
# 100 rows per service would come back as 40, and a 60-bucket series as 40.
# Three services x 100 rows x (1 dict + 5 scalars) plus the containers.
RESOURCE_NODE_BUDGET = 2200
# Ten series x (1 dict + 1 list + 60 values + 4 summary leaves) plus labels.
METRIC_NODE_BUDGET = 1200

# The deployments section is the one platform section whose useful fields sit
# two levels deeper than the rest: sections -> envelope -> data -> items[] ->
# item -> node_summary -> counts. It is ALREADY a named allowlist projection
# (project_deployment), so the extra depth adds no new surface.
DEPLOYMENTS_DEPTH = 6
SECTION_DEPTH = 4


# --- projections ----------------------------------------------------------

def project_deployment(row):
    """One serialized PlatformDeployment, cut down to the operator's fields."""
    if not isinstance(row, dict):
        return {}
    return {key: row.get(key) for key in DEPLOYMENT_FIELDS}


def _project_deployments_section(envelope):
    """Replace the deployments section's `items` with the named projection."""
    data = envelope.get("data")
    if not isinstance(data, dict):
        return envelope
    items = data.get("items")
    if isinstance(items, list):
        data = dict(data)
        data["items"] = [project_deployment(row) for row in items[:20]]
        envelope = dict(envelope)
        envelope["data"] = data
    return envelope


def project_capacity(envelope):
    """The fleet picture, minus everything a mutation would take as input.

    Elastic IP allocation ids and the raw ``assign`` map are deliberately
    absent: they are inputs to a mutation this domain does not offer, and the
    egress picture an operator needs is counts and booleans.
    """
    egress = envelope.get("egress") or {}
    return {
        "mode": envelope.get("mode"),
        "region": envelope.get("region"),
        "generated_at": envelope.get("generated_at"),
        "node_id_pinned": envelope.get("node_id_pinned"),
        "nodes": [{
            "id": row.get("id"), "name": row.get("name"),
            "healthy": row.get("healthy"),
            "instance_type": row.get("instance_type"),
            "zone": row.get("zone"), "is_self": row.get("self"),
            "primary": row.get("primary"),
            "added_by_capacity": row.get("added_by_capacity"),
        } for row in ((envelope.get("nodes") or {}).get("instances") or [])[:40]],
        "databases": [{
            "identifier": row.get("identifier"), "kind": row.get("kind"),
            "status": row.get("status"), "writer": row.get("writer"),
            "readers": list(row.get("readers") or [])[:20],
            "instance_class": (row.get("writer_instance_class")
                               or row.get("instance_class")),
        } for row in (envelope.get("databases") or [])[:20]],
        "caches": [{
            "identifier": row.get("identifier"), "status": row.get("status"),
            "node_type": row.get("node_type"),
            "replica_count": row.get("replica_count"),
            "min_replicas": row.get("min_replicas"),
            "cluster_enabled": row.get("cluster_enabled"),
            "resize_impact": row.get("resize_impact"),
            "blocked_reason": row.get("blocked_reason"),
        } for row in (envelope.get("caches") or [])[:20]],
        "egress": {
            "enabled": egress.get("enabled"),
            "available": egress.get("available"),
            "attached_count": len(egress.get("attached") or []),
            "reserved_count": len(egress.get("reserved") or []),
            "pending_node_count": len(egress.get("pending_nodes") or []),
            "to_allocate": egress.get("to_allocate"),
            "monthly_usd_per_address": egress.get("monthly_usd_per_address"),
        },
        "actions": envelope.get("actions") or {},
        "warnings": [str(row.get("code") or "")[:64]
                     for row in (envelope.get("warnings") or [])[:20]],
        "sizes": envelope.get("sizes") or {},
    }


def local_probe_note(source):
    """How to report the System Setup local API probe from a chat turn.

    There is no originating HTTP request here, so ``trusted_local_api_target``
    can only answer ``configured_static`` or ``default_80``. The Admin always
    has a request to take a port from, so a port-80 miss is a red row the Admin
    would never show — it is reported as UNAVAILABLE, naming the setting that
    would make it real evidence, rather than as a failure.
    """
    if source != "default_80":
        return {"source": source, "status": "attempted"}
    return {
        "source": source, "status": "unavailable",
        "setting": "SYSTEM_SETUP_LOCAL_API_URL",
        "message": (
            "No SYSTEM_SETUP_LOCAL_API_URL is configured, so the local API "
            "probe fell back to port 80 with no request to take a port from. "
            "Treat any local-API check row in this report as unproven rather "
            "than failing."),
    }


def project_drift(row):
    """The recorded managed-engine drift, from the newest in-window Event.

    ``row`` is what ``admin_platform._newest_drift_event()`` returns — a dict
    with ``metadata`` and ``created``, or None. Never runs a live scan: a
    chat-triggered scan would be a capability the Admin does not have and a
    provider fan-out per turn.
    """
    from mojo.helpers import infrastructure
    from mojo.apps.account.services import admin_platform

    if row is None:
        return {
            "status": "no_recent_scan",
            "mode": infrastructure.infrastructure_mode(),
            "recorded_at": None, "findings": [],
            "message": (
                "No managed-engine drift scan has been recorded inside the "
                "Dashboard's freshness window "
                f"({admin_platform.VERSION_DRIFT_MAX_AGE.days} days)."),
        }
    metadata = row.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    findings = metadata.get("findings")
    findings = findings if isinstance(findings, list) else []
    rows = []
    for finding in findings[:MAX_DRIFT_FINDINGS]:
        if not isinstance(finding, dict):
            continue
        rows.append({
            "kind": finding.get("kind"),
            "resource": finding.get("resource_id"),
            "engine": finding.get("engine"),
            "current_version": finding.get("current_version"),
            "available_major": finding.get("available_major"),
            "deadline": finding.get("deadline"),
            "days_remaining": finding.get("days_remaining"),
            "note": finding.get("note"),
        })
    created = row.get("created")
    return bounded({
        "status": "recorded",
        "mode": infrastructure.infrastructure_mode(),
        "recorded_at": created.isoformat() if created is not None else None,
        "region": metadata.get("region"),
        "findings": rows,
        "truncated": len(findings) > len(rows),
    }, depth=4)


def project_series(data, labels):
    """Per-slug series capped at the most recent buckets, plus min/max/avg."""
    labels = list(labels or [])
    keep = labels[-MAX_METRIC_BUCKETS:]
    series = {}
    for slug, values in list((data or {}).items())[:MAX_METRIC_SLUGS]:
        values = list(values or [])
        window = values[-MAX_METRIC_BUCKETS:]
        numbers = [value for value in window if isinstance(value, (int, float))]
        series[str(slug)[:80]] = {
            "values": [round(float(value), 4) if isinstance(value, (int, float))
                       else None for value in window],
            "min": round(min(numbers), 4) if numbers else None,
            "max": round(max(numbers), 4) if numbers else None,
            "avg": round(sum(numbers) / len(numbers), 4) if numbers else None,
            "truncated": len(values) > len(window),
        }
    return {"labels": keep, "series": series,
            "truncated": len(labels) > len(keep)}


# --- platform and dashboard ----------------------------------------------

@tool(
    name="get_platform_health",
    domain="cloud",
    permission=DASHBOARD_PERMS,
    description=(
        "The Admin Dashboard's operational picture: per-source health status "
        "and reason for every collector (load balancer, EC2, RDS, cache, "
        "certificates, public API, framework, SMS, email, last deployment, "
        "jobs, sanity, incidents, tickets), the overall availability verdict, "
        "and the attention line. Start here when asked whether anything is "
        "wrong. Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "description": (
                    "Bypass the short provider cache. Costs more provider "
                    "calls; every collector stays individually bounded."),
            },
        },
    },
)
def _tool_get_platform_health(params, user):
    from mojo.apps.account.services import admin_platform

    envelope = admin_platform.dashboard_overview(
        actor_request(user), refresh=bool(params.get("refresh")))
    sources = {}
    for name, value in (envelope.get("sources") or {}).items():
        value = value if isinstance(value, dict) else {}
        sources[name] = {
            "status": value.get("status"),
            "reason": value.get("reason"),
            "observed_at": value.get("observed_at"),
            "data": bounded(value.get("data"), depth=3),
        }
    return {
        "observed_at": envelope.get("observed_at"),
        "availability": bounded(envelope.get("availability"), depth=3),
        "attention": (envelope.get("attention") or {}).get("message"),
        "sources": sources,
    }


@tool(
    name="get_platform_overview",
    domain="cloud",
    permission=PLATFORM_PERMS,
    description=(
        "The Admin Platform page's section envelopes. You MUST name the "
        "sections you need (at most 4) — a bare call would collect all ten, "
        "including per-app HTTPS probes. Use 'deployments' for deploy history "
        "and the ids the deploy tools take, 'api' for what the public origin "
        "is serving, 'fleet' for edge runners, 'sanity' for node checks. "
        "Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": (
                    "Which sections to collect. Each keeps its own permission; "
                    "an unauthorized one comes back as 'unauthorized'."),
                "items": {"type": "string", "enum": list(PLATFORM_SECTIONS)},
            },
        },
        "required": ["sections"],
    },
)
def _tool_get_platform_overview(params, user):
    from mojo.apps.account.services import admin_platform

    wanted = [name for name in (params.get("sections") or [])
              if name in PLATFORM_SECTIONS]
    if not wanted:
        return refuse(
            f"Name at least one section: {', '.join(PLATFORM_SECTIONS)}.",
            "invalid_request")
    if len(wanted) > MAX_SECTIONS:
        return refuse(
            f"Ask for at most {MAX_SECTIONS} sections at a time; this call "
            f"named {len(wanted)}.", "invalid_request")
    # The filter honours only a STRING (admin_platform: isinstance(wanted, str)).
    # A JSON array would read as absent and fan out to all ten sections.
    request = actor_request(user, sections=",".join(wanted))
    envelope = admin_platform.platform_overview(request)
    sections = {}
    for name, value in (envelope.get("sections") or {}).items():
        depth = SECTION_DEPTH
        if name == "deployments":
            value = _project_deployments_section(value)
            depth = DEPLOYMENTS_DEPTH
        sections[name] = bounded(value, depth=depth)
    return {"requested": wanted, "sections": sections}


@tool(
    name="get_advanced_inventory",
    domain="cloud",
    permission=ADVANCED_PERMS,
    description=(
        "The Admin Advanced page's inventory: hosting posture, the AWS "
        "inventory envelope (EC2/RDS/ElastiCache/S3 as the Admin reports it), "
        "and the network security settings envelope. Read-only."),
    input_schema={"type": "object", "properties": {}},
)
def _tool_get_advanced_inventory(params, user):
    from mojo.apps.account.services import admin_platform

    envelope = admin_platform.advanced_overview(actor_request(user))
    return {"sections": {
        name: bounded(value, depth=5)
        for name, value in (envelope.get("sections") or {}).items()
    }}


@tool(
    name="get_framework_status",
    domain="cloud",
    permission=FRAMEWORK_PERMS,
    description=(
        "Which django-mojo version this fleet runs, what is published, whether "
        "an update can be applied right now, and the one thing blocking it "
        "when it cannot (update_unavailable / requires_superuser / "
        "no_converged_deployment / infrastructure_external). Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "description": "Re-check the published release instead of the cache.",
            },
        },
    },
)
def _tool_get_framework_status(params, user):
    from mojo.apps.account.services import admin_platform

    value = admin_platform.framework_overview(
        actor_request(user), refresh=bool(params.get("refresh")))
    return {key: value.get(key) for key in (
        "installed", "latest", "checked_at", "source", "update_available",
        "pin", "can_update", "blocked_reason")}


# --- capacity -------------------------------------------------------------

@tool(
    name="get_fleet_capacity",
    domain="cloud",
    permission="manage_aws",
    description=(
        "The capacity panel's whole read: app nodes with health and instance "
        "type, databases with their writer/reader picture, cache groups with "
        "replica counts, the stable-egress summary, and — importantly — the "
        "server's own per-action offered/blocked_reason map. Never propose a "
        "capacity change the 'actions' map does not currently offer. "
        "Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "description": "Bypass the 120-second report cache.",
            },
        },
    },
)
def _tool_get_fleet_capacity(params, user):
    try:
        envelope = capacity_service.report(refresh=bool(params.get("refresh")))
    except capacity_service.CapacityError as error:
        return refuse(error.message, error.error_code)
    return bounded(project_capacity(envelope), depth=4)


@tool(
    name="get_capacity_operation_status",
    domain="cloud",
    permission="manage_aws",
    description=(
        "Recorded progress for ONE capacity operation or ONE batch — the "
        "authoritative follow-up read after an approved capacity change. Name "
        "exactly one of 'operation' or 'batch'. Polling never advances the "
        "work. Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"type": "string",
                          "description": "A capacity operation id."},
            "batch": {"type": "string", "description": "A capacity batch id."},
        },
    },
)
def _tool_get_capacity_operation_status(params, user):
    operation = str(params.get("operation") or "").strip()
    batch = str(params.get("batch") or "").strip()
    if bool(operation) == bool(batch):
        return refuse("Name exactly one of operation or batch.",
                      "invalid_request")
    try:
        if batch:
            record = capacity_service.batch_status(batch)
        else:
            record = capacity_service.operation_status(operation)
    except capacity_service.CapacityError as error:
        return refuse(error.message, error.error_code)
    return bounded(record, depth=4)


# --- managed engine upgrades ---------------------------------------------

@tool(
    name="get_managed_upgrades",
    domain="cloud",
    permission="manage_aws",
    description=(
        "Pending managed-engine (RDS / ElastiCache) major-version upgrades "
        "this installation is offering, with each finding's current version, "
        "offered target, support deadline and live status. Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "description": "Re-scan instead of reading the ten-minute cache.",
            },
        },
    },
)
def _tool_get_managed_upgrades(params, user):
    try:
        envelope = maintenance_service.report(
            refresh=bool(params.get("refresh")))
    except maintenance_service.MaintenanceError as error:
        return refuse(error.message, error.error_code)
    findings = []
    for row in (envelope.get("findings") or [])[:20]:
        findings.append({
            "kind": row.get("kind"), "resource": row.get("resource_id"),
            "engine": row.get("engine"),
            "current_version": row.get("current_version"),
            "target_version": row.get("available_major"),
            "deadline": row.get("deadline"),
            "days_remaining": row.get("days_remaining"),
            "note": row.get("note"), "status": row.get("status"),
            "settled": row.get("settled"),
        })
    return bounded({
        "generated_at": envelope.get("generated_at"),
        "region": envelope.get("region"),
        "status": envelope.get("status"),
        "reason": envelope.get("reason"),
        "scheduled": envelope.get("scheduled"),
        "available": envelope.get("status") == "ok",
        "findings": findings,
        "warnings": [str(row.get("code") or "")[:64]
                     for row in (envelope.get("warnings") or [])[:20]],
    }, depth=4)


@tool(
    name="get_upgrade_status",
    domain="cloud",
    permission="manage_aws",
    description=(
        "Live progress for one resource moving to a target engine version. "
        "'upgraded' is the only success signal — 'settled' says AWS finished, "
        "not that the engine moved. Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(maintenance_service.KINDS),
                     "description": "Which managed service the resource is."},
            "resource": {"type": "string",
                         "description": "The resource identifier."},
            "target_version": {
                "type": "string",
                "description": "The version it is moving to, when known."},
        },
        "required": ["kind", "resource"],
    },
)
def _tool_get_upgrade_status(params, user):
    target = str(params.get("target_version") or "").strip() or None
    try:
        record = maintenance_service.resource_status(
            params["kind"], str(params["resource"]).strip(), target)
    except maintenance_service.MaintenanceError as error:
        return refuse(error.message, error.error_code)
    return bounded(record, depth=3)


# --- System Setup (read-only) --------------------------------------------
#
# Setup REPAIR is deliberately absent from this domain. Every system_setup
# mutation is bound to the browser Origin that started it
# (system_setup.request_origin / _assert_bound_origin) and refuses any session
# that is not an interactive same-origin superuser tab. Reaching it from chat
# would mean forging that binding. Audit and progress are reachable; repair is
# started and driven from the Admin.

@tool(
    name="get_setup_readiness",
    domain="cloud",
    permission="admin",
    description=(
        "Run ONE System Setup readiness section and return its checks. You "
        "must name the section — a bare run fans out across AWS, DNS and "
        "certificates. Requires an active superuser on an interactive Admin "
        "session. Read-only: this audits, it does not repair. Setup repair is "
        "only available in the Admin itself."),
    input_schema={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": (
                    "The readiness section code. Call with a wrong code to get "
                    "the current list back."),
            },
        },
        "required": ["section"],
    },
    authorize=can_system_admin,
)
def _tool_get_setup_readiness(params, user, *, request_meta=None):
    from mojo.apps.account.services import system_readiness

    denied = superuser_refusal(user) or interactive_refusal(request_meta)
    if denied is not None:
        return denied
    section = str(params.get("section") or "").strip()
    codes = [entry["code"] for entry in system_readiness.sections()]
    if section not in codes:
        return refuse(
            f"Unknown readiness section '{section}'. This installation has: "
            f"{', '.join(codes[:40])}.", "invalid_request")
    # No originating HTTP request here, so the local probe target can only be
    # the configured one or the port-80 default.
    target = system_readiness.trusted_local_api_target(None)
    report = system_readiness.run(section, {
        "local_url": target["url"], "local_source": target["source"],
        "timeout": 2.0, "retries": 1,
    })
    return bounded({
        "section": section,
        "overall": report.get("overall"),
        "summary": report.get("summary"),
        "generated_at": report.get("generated_at"),
        "truncated": report.get("truncated"),
        "local_probe": local_probe_note(target["source"]),
        "sections": report.get("sections"),
    }, depth=5)


@tool(
    name="get_setup_operation",
    domain="cloud",
    permission="admin",
    description=(
        "Honest progress for a System Setup operation a human started in the "
        "Admin: its steps, the current step, recorded choices and the recent "
        "log. Defaults to the active Fix Setup operation. Requires an active "
        "superuser on an interactive Admin session. Read-only — this tool "
        "cannot start, advance, choose or cancel."),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": (
                    "A setup operation id. Omit for the active fix operation."),
            },
        },
    },
    authorize=can_system_admin,
)
def _tool_get_setup_operation(params, user, *, request_meta=None):
    from mojo.apps.account.models import SystemSetupOperation
    from mojo.apps.account.services import system_setup

    denied = superuser_refusal(user) or interactive_refusal(request_meta)
    if denied is not None:
        return denied
    operation_id = str(params.get("operation") or "").strip()
    row = None
    if operation_id:
        # The pk is a UUID column: a model-invented id must be a refusal, not a
        # ValidationError from the query layer.
        import uuid as uuid_module

        try:
            parsed = uuid_module.UUID(operation_id)
        except (ValueError, TypeError, AttributeError):
            parsed = None
        if parsed is not None:
            row = SystemSetupOperation.objects.filter(pk=parsed).first()
        if row is None:
            return refuse("That setup operation is not on record.",
                          "unknown_resource")
    else:
        row = SystemSetupOperation.objects.filter(
            mode="fix", status__in=SystemSetupOperation.ACTIVE_STATUSES).first()
        if row is None:
            return {"active": False,
                    "message": "No Fix Setup operation is running."}
    record = system_setup.serialize(row) or {}
    steps = [{
        "id": step.get("id"), "label": step.get("label"),
        "kind": step.get("kind"), "state": step.get("state"),
        "section": step.get("section"),
    } for step in (record.get("steps") or [])[:40] if isinstance(step, dict)]
    return bounded({
        "active": True,
        "id": record.get("id"), "mode": record.get("mode"),
        "section": record.get("section"), "status": record.get("status"),
        "cursor": record.get("cursor"), "steps": steps,
        "current_step": record.get("current_step"),
        "choices": record.get("choices"),
        "finished_at": record.get("finished_at"),
        "log": (record.get("log") or [])[-MAX_SETUP_LOG:],
    }, depth=4)


# --- recorded drift -------------------------------------------------------

@tool(
    name="get_version_drift",
    domain="cloud",
    permission=FRAMEWORK_PERMS,
    description=(
        "The managed-engine version drift the last scheduled scan RECORDED, "
        "as the Dashboard reads it — never a live scan. Findings older than "
        "the Dashboard's freshness window are not reported. Read-only."),
    input_schema={"type": "object", "properties": {}},
)
def _tool_get_version_drift(params, user):
    from mojo.apps.account.services import admin_platform

    # The same query the Dashboard's own drift row runs, reused rather than
    # rewritten so the category filter and the freshness window cannot drift.
    return project_drift(admin_platform._newest_drift_event())


# --- CloudWatch -----------------------------------------------------------

def _helper():
    """One attempt per call, as the Admin's own CloudWatch endpoints use.

    botocore's default of three turns an unreachable or unauthorized endpoint
    into a multi-second wait, and a turn whose whole job may be to say "AWS is
    not answering" must say it quickly.
    """
    import functools

    from mojo.helpers.aws.client import get_client
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper

    return CloudWatchHelper(
        client_factory=functools.partial(get_client, max_attempts=1))


# Most actionable cause first, mirroring mojo/apps/aws/rest/cloudwatch.py: when
# several services fail for different reasons they share one session, so the
# strongest signal is the real one.
_REASON_PRIORITY = ("credentials_unavailable", "denied", "network_unavailable",
                    "service_error")


def _single_cause(reasons):
    for reason in _REASON_PRIORITY:
        if reason in reasons:
            return reason
    return "service_error"


@tool(
    name="list_cloud_resources",
    domain="cloud",
    permission="manage_aws",
    description=(
        "List the EC2 instances, RDS instances and ElastiCache clusters the "
        "configured credentials can see, each with the 'slug' that "
        "fetch_cloud_metrics takes. One refused or unreachable service leaves "
        "the other two listed. Read-only."),
    input_schema={"type": "object", "properties": {}},
)
def _tool_list_cloud_resources(params, user):
    helper = _helper()
    degraded = {}

    def listing(name, operation, load, project):
        try:
            rows = load()
        except Exception as exc:
            reason = provider_reason(exc, operation)
            if reason is None:
                raise
            degraded[name] = reason
            return []
        return [project(row) for row in list(rows)[:MAX_RESOURCE_ROWS]]

    ec2 = listing("ec2", "ec2.describe_instances", helper.list_ec2_instances,
                  lambda row: {
                      "id": row.get("id"),
                      "slug": row.get("name") or row.get("id"),
                      "name": row.get("name"), "type": row.get("instance_type"),
                      "state": row.get("state")})
    rds = listing("rds", "rds.describe_db_instances", helper.list_rds_instances,
                  lambda row: {
                      "id": row.get("id"), "slug": row.get("id"),
                      "name": row.get("id"), "type": row.get("instance_class"),
                      "state": row.get("status")})
    redis = listing("redis", "elasticache.describe_cache_clusters",
                    helper.list_elasticache_clusters,
                    lambda row: {
                        "id": row.get("id"), "slug": row.get("id"),
                        "name": row.get("id"), "type": row.get("node_type"),
                        "state": row.get("status")})

    available = len(degraded) < 3
    # The row cap is this tool's own (MAX_RESOURCE_ROWS, applied above), so the
    # projector is told to honour it rather than re-truncate to its default 40.
    return bounded({
        "ec2": ec2, "rds": rds, "redis": redis,
        "degraded": degraded, "available": available,
        "reason": None if available else _single_cause(set(degraded.values())),
    }, depth=4, max_items=MAX_RESOURCE_ROWS, max_nodes=RESOURCE_NODE_BUDGET)


@tool(
    name="fetch_cloud_metrics",
    domain="cloud",
    permission="manage_aws",
    description=(
        "CloudWatch time-series for one metric category across up to ten "
        "resources. Get the 'slugs' from list_cloud_resources. The window is "
        "stated in hours (1-168); the result keeps the most recent buckets "
        "with min/max/avg per series. Read-only."),
    input_schema={
        "type": "object",
        "properties": {
            "account": {"type": "string",
                        "enum": sorted(ACCOUNT_NAMESPACE.keys()),
                        "description": "Which resource type to read."},
            "category": {"type": "string",
                         "enum": sorted(CATEGORY_METRIC.keys()),
                         "description": "The metric shortname."},
            "slugs": {"type": "array", "items": {"type": "string"},
                      "description": (
                          "Resource slugs from list_cloud_resources. Omit for "
                          "every resource of this account type.")},
            "hours": {"type": "integer",
                      "description": "Look back this many hours (1-168, default 24)."},
            "granularity": {"type": "string",
                            "enum": ["minutes", "hours", "days", "weeks"],
                            "description": "Bucket size. Default hours."},
            "stat": {"type": "string", "enum": ["avg", "max", "min", "sum"],
                     "description": "Statistic per bucket. Default avg."},
        },
        "required": ["account", "category"],
    },
)
def _tool_fetch_cloud_metrics(params, user):
    import datetime

    from mojo.helpers import dates
    from mojo.helpers.aws.cloudwatch import resolve_metric

    account = params["account"]
    category = params["category"]
    try:
        resolve_metric(account, category)
    except ValueError as exc:
        return refuse(str(exc)[:200], "invalid_request")
    hours = params.get("hours")
    hours = 24 if hours is None else int(hours)
    if hours < 1 or hours > MAX_HOURS:
        return refuse(f"hours must be between 1 and {MAX_HOURS}.",
                      "invalid_request")
    slugs = params.get("slugs")
    if slugs is not None:
        slugs = [str(slug) for slug in slugs][:MAX_METRIC_SLUGS]
        if not slugs:
            slugs = None
    dt_end = dates.utcnow()
    dt_start = dt_end - datetime.timedelta(hours=hours)
    try:
        data = _helper().fetch(
            account=account, category=category, slugs=slugs,
            dt_start=dt_start, dt_end=dt_end,
            granularity=params.get("granularity") or "hours",
            stat=params.get("stat") or "avg")
    except Exception as exc:
        reason = provider_reason(exc, "cloudwatch.get_metric_statistics")
        if reason is None:
            raise
        return {"available": False, "reason": reason, "labels": [],
                "series": {}}
    projected = project_series(data.get("data"), data.get("labels"))
    projected.update({"available": True, "reason": None, "account": account,
                      "category": category, "hours": hours,
                      "granularity": params.get("granularity") or "hours",
                      "stat": params.get("stat") or "avg"})
    # project_series has already capped this to MAX_METRIC_SLUGS series of
    # MAX_METRIC_BUCKETS points; the projector honours those caps instead of
    # cutting every series back to its default 40.
    return bounded(projected, depth=4, max_items=MAX_METRIC_BUCKETS,
                   max_nodes=METRIC_NODE_BUDGET)
