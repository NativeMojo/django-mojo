"""Revision-bound configuration activation through the existing job transport."""
import re
import time

from django.utils import timezone
from django.core import signing

from mojo import errors as merrors
from mojo.helpers.settings import settings


JOB_FUNCTION = "mojo.apps.account.services.fleet_apply.run"
TRIGGER_FUNCTION = "mojo.deploy.fleet_config_node.trigger"
REPORT_FUNCTION = "mojo.deploy.fleet_config_node.report"
MAX_NODES = 128
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,252}$")


def expected_nodes():
    # Explicit config-consuming membership can include offline machines. Never
    # derive the completion denominator from the set currently answering jobs.
    nodes = settings.get_static("ADMIN_FLEET_CONFIG_EXPECTED_NODES", None)
    if nodes is None:
        from mojo.apps.account.services import system_settings
        topology = system_settings.get_value(system_settings.EXPECTED_EDGE_TOPOLOGY) or {}
        nodes = topology.get("nodes", [])
    if (not isinstance(nodes, (list, tuple)) or not nodes or len(nodes) > MAX_NODES
            or any(not isinstance(n, str) or not HOST_RE.fullmatch(n.lower()) for n in nodes)):
        raise merrors.ValueException("Define the expected configuration node hostnames before applying")
    result = sorted({n.lower() for n in nodes})
    if len(result) != len(nodes):
        raise merrors.ValueException("Expected configuration node hostnames must be unique")
    return result


def _channel():
    return settings.get_static("ADMIN_FLEET_CONFIG_CHANNEL", "edge")


def _nodes(revision, expected, reply=None, error=None):
    responses = {row.get("host"): row for row in (reply or {}).get("results", [])
                 if isinstance(row, dict) and row.get("status") == "success"}
    anomalies = bool((reply or {}).get("anomalies"))
    nodes = []
    for hostname in expected:
        result = responses.get(hostname, {}).get("result")
        result = result if isinstance(result, dict) else {}
        matches = (result.get("target_revision", result.get("revision")) == revision
                   and result.get("node", hostname) == hostname
                   and (not result.get("healthy") or result.get("node") == hostname))
        node = {
            "hostname": hostname, "revision": revision,
            "published": True, "installed": False, "restart_requested": False,
            "restarted": False, "healthy": False,
            "status": "unknown", "error_code": error or "node_did_not_reply",
        }
        if matches:
            for flag in ("installed", "restart_requested", "restarted", "healthy"):
                node[flag] = result.get(flag) is True
            # Never accept health on a predecessor or an unactivated file.
            node["healthy"] = (
                node["healthy"] and node["installed"] and node["restarted"]
                and not anomalies and (reply or {}).get("status") in ("verified", "partial"))
            node["status"] = next((status for flag, status in (
                ("healthy", "healthy"), ("restarted", "restarted"),
                ("restart_requested", "restart_requested"), ("installed", "downloaded"))
                if node[flag]), "pending")
            code = result.get("error_code")
            node["error_code"] = None
            if anomalies:
                node["error_code"] = "node_reply_ambiguous"
            elif isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code):
                node["error_code"] = code
            if node["error_code"]:
                node["status"] = "failed"
                node["healthy"] = False
        nodes.append(node)
    return nodes


def observe(revision, expected=None, *, manager=None):
    if not revision:
        return {"status": "unpublished", "nodes": [], "healthy_everywhere": False}
    try:
        expected = expected or expected_nodes()
    except merrors.ValueException:
        return {"status": "unconfigured", "nodes": [], "healthy_everywhere": False,
                "error_code": "expected_nodes_unconfigured"}
    if manager is None:
        from mojo.apps.jobs.manager import get_manager
        manager = get_manager()
    try:
        channel = _channel()
        roster = manager.get_runners_bounded(channel, limit=MAX_NODES, timeout=1.0)
        roster = [row for row in roster if str(row.get("hostname", "")).lower() in expected]
        reply = manager.broadcast_execute_checked(
            REPORT_FUNCTION, {"revision": revision}, timeout=5.0,
            channel=channel, roster=roster)
        nodes = _nodes(revision, expected, reply)
    except Exception:
        nodes = _nodes(revision, expected, error="node_transport_unavailable")
    healthy = bool(nodes) and all(node["healthy"] for node in nodes)
    return {"status": "healthy" if healthy else "pending", "nodes": nodes,
            "healthy_everywhere": healthy, "observed_at": timezone.now().isoformat(),
            "health_scope": "request_service_and_dependencies"}


def apply(actor, payload):
    from django.db import transaction
    from mojo.apps import jobs
    from mojo.apps.account.models import User
    from mojo.apps.account.services import fleet_config, provider_setup
    if not isinstance(payload, dict) or set(payload) != {"expected_revision"}:
        raise merrors.ValueException("Apply accepts only expected_revision")
    actor = provider_setup._superuser(actor)
    with transaction.atomic():
        User.objects.select_for_update().order_by("pk").first()
        actor = provider_setup._superuser(actor, lock=True)
        current = fleet_config.state(actor)
        revision = current.get("revision")
        if not revision or payload["expected_revision"] != revision:
            raise merrors.ValueException("Configuration changed; reload before applying", code=409, status=409)
        nodes = expected_nodes()
        from mojo.apps.jobs.models import Job
        # The installation lock serializes duplicate clicks; failed or finished
        # operations can still be retried through the same activation path.
        active_jobs = Job.objects.filter(
            func=JOB_FUNCTION, status__in=["pending", "running"],
            expires_at__gt=timezone.now()).order_by("-created")[:MAX_NODES]
        for active in active_jobs:
            try:
                active_intent = _intent(active, max_age=300)
            except merrors.PermissionDeniedException:
                continue
            if active_intent["revision"] != revision or active_intent["nodes"] != nodes:
                raise merrors.ValueException("A fleet apply is in progress; wait for its result", code=409, status=409)
            return operation(actor, active.pk)
        operation_id = jobs.publish(
            JOB_FUNCTION, {"revision": revision, "nodes": nodes, "actor_id": actor.pk},
            channel="default", max_retries=0, max_exec_seconds=180, expires_in=300)
        intent = {"operation_id": operation_id, "revision": revision,
                  "nodes": nodes, "actor_id": actor.pk}
        # Jobs are administrable elsewhere. Their editable payload is transport,
        # never authorization to act on behalf of a freshly authenticated admin.
        Job.objects.filter(pk=operation_id).update(payload={
            "intent": signing.dumps(intent, salt="mojo.fleet.apply.intent")})
        fleet_config._audit(actor, "apply_requested", revision, [])
    return {"operation_id": operation_id, "revision": revision, "status": "queued",
            "healthy_everywhere": False, "nodes": _nodes(revision, nodes)}


def _intent(job, max_age=None):
    try:
        value = signing.loads(job.payload.get("intent", ""),
                              salt="mojo.fleet.apply.intent", max_age=max_age)
        if value.get("operation_id") != job.pk:
            raise ValueError()
        return value
    except (signing.BadSignature, ValueError, TypeError, AttributeError):
        raise merrors.PermissionDeniedException("Invalid configuration operation authority") from None


def operation(actor, operation_id):
    from mojo.apps.account.services.provider_setup import _superuser
    from mojo.apps.jobs.models import Job
    _superuser(actor)
    if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise merrors.ValueException("Invalid configuration operation")
    job = Job.objects.filter(pk=operation_id, func=JOB_FUNCTION).first()
    if job is None:
        raise merrors.ValueException("Configuration operation not found", code=404, status=404)
    intent = _intent(job)
    report = {"status": "queued", "healthy_everywhere": False,
              "nodes": _nodes(intent["revision"], intent["nodes"])}
    if job.metadata.get("fleet_config"):
        try:
            signed = signing.loads(job.metadata["fleet_config"], salt="mojo.fleet.apply.result")
            if signed["intent"] != intent:
                raise ValueError()
            report = signed["report"]
        except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
            report.update(status="unknown", error_code="operation_evidence_invalid")
    if job.status in ("failed", "expired", "canceled"):
        report = dict(report, status=job.status, healthy_everywhere=False)
    return dict(report, operation_id=job.pk, revision=intent["revision"], job_status=job.status)


def run(job):
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.account.models import User
    from mojo.apps.account.services import fleet_config
    intent = _intent(job, max_age=300)
    revision, expected = intent["revision"], intent["nodes"]

    def save(report):
        report.setdefault("observed_at", timezone.now().isoformat())
        job.metadata["fleet_config"] = signing.dumps({"intent": intent, "report": report}, salt="mojo.fleet.apply.result")
        job.save(update_fields=["metadata", "modified"])

    try:
        actor = User.objects.get(pk=intent["actor_id"])
        current = fleet_config.state(actor)
        if current.get("revision") != revision:
            save({"status": "superseded", "healthy_everywhere": False,
                  "nodes": _nodes(revision, expected, error="publication_superseded")})
            return
        manager = get_manager()
        channel = _channel()
        roster = manager.get_runners_bounded(channel, limit=MAX_NODES, timeout=1.0)
        roster = [row for row in roster if str(row.get("hostname", "")).lower() in expected]
        trigger_reply = manager.broadcast_execute_checked(
            TRIGGER_FUNCTION, {"revision": revision, "authorization": job.payload["intent"]}, timeout=5.0, channel=channel, roster=roster)
        trigger_nodes = _nodes(revision, expected, trigger_reply)
        deadline = time.monotonic() + 120
        while True:
            job.refresh_from_db(fields=["cancel_requested"])
            if job.cancel_requested:
                save({"status": "canceled", "healthy_everywhere": False,
                      "nodes": _nodes(revision, expected, error="operation_canceled")})
                return
            if fleet_config.state(actor).get("revision") != revision:
                save({"status": "superseded", "healthy_everywhere": False,
                      "nodes": _nodes(revision, expected, error="publication_superseded")})
                return
            report = observe(revision, expected, manager=manager)
            for node, triggered in zip(report["nodes"], trigger_nodes):
                if not node["healthy"] and triggered["error_code"]:
                    node.update(status="failed", error_code=triggered["error_code"])
            if report["healthy_everywhere"]:
                save(report)
                return
            if time.monotonic() >= deadline:
                report["status"] = "timed_out"
                save(report)
                return
            save(report)
            time.sleep(2)
    except Exception:
        save({"status": "failed", "healthy_everywhere": False,
              "nodes": _nodes(revision, expected, error="apply_operation_failed")})
