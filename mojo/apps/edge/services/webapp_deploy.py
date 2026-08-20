"""Durable, per-WebApp fleet deployment coordination."""

import hashlib
import time

from django.db import transaction
from django.utils import timezone

from mojo.helpers import logit
from mojo.helpers.settings import settings

from mojo.apps.edge.models import WebApp, WebAppDeployment
from mojo.apps.edge.models.web_app_deployment import (
    STATUS_DEPLOYING, STATUS_FAILED, STATUS_LIVE, STATUS_QUEUED,
    STATUS_ROLLED_BACK, STATUS_ROLLING_BACK, STATUS_SUPERSEDED,
)
from mojo.apps.edge.models.web_app_release import (
    STATUS_LIVE as RELEASE_LIVE,
    STATUS_SUPERSEDED as RELEASE_SUPERSEDED,
    STATUS_UPLOADED as RELEASE_UPLOADED,
)


logger = logit.get_logger("edge", "edge.log")

ORCHESTRATE_JOB = "mojo.apps.edge.asyncjobs.webapp_deploy_orchestrate"
NODE_JOB = "mojo.apps.edge.asyncjobs.webapp_deploy_node"
EDGE_CHANNEL = "edge"
ORCHESTRATE_CHANNEL = "default"
POLL_INTERVAL = 2.0

JOB_TERMINAL = ("completed", "failed", "canceled", "expired")


def deploy_timeout():
    return int(settings.get_static("EDGE_WEBAPP_DEPLOY_TIMEOUT", 600))


def publish(deployment_id):
    """Queue one coordinator after the promotion transaction commits."""
    from mojo.apps import jobs

    return jobs.publish(
        func=ORCHESTRATE_JOB,
        payload={"deployment": deployment_id},
        channel=ORCHESTRATE_CHANNEL,
        idempotency_key=f"webapp-deploy-{deployment_id}",
        max_retries=0,
        expires_in=deploy_timeout(),
        max_exec_seconds=deploy_timeout())


def publish_or_restore(deployment_id):
    """Publish after commit; restore desired state if queueing itself fails."""
    try:
        return publish(deployment_id)
    except Exception as err:
        detail = f"deployment coordinator could not be queued: {err}"
        logger.error(f"edge WebApp deployment {deployment_id}: {detail}")
        restored = _restore_previous(deployment_id, detail)
        if restored is not None:
            _set_terminal(restored.pk, STATUS_ROLLED_BACK, detail)
        try:
            from mojo.apps.incident import reporter
            reporter.report_event(
                detail,
                title="WebApp deployment could not start",
                category="edge_webapp_deploy", level=7)
        except Exception:
            logger.exception(
                f"edge WebApp deployment {deployment_id}: incident failed")
        return None


def _alive_edge_runners():
    from mojo.apps import jobs

    rows = jobs.get_runners_bounded(
        channel=EDGE_CHANNEL, limit=128, max_scan_pages=16,
        timeout=1.0) or []
    return sorted({
        row.get("runner_id") for row in rows
        if row.get("alive") and row.get("runner_id")
    })


def _job_key(deployment_id, runner_id, rollback=False):
    digest = hashlib.sha256(runner_id.encode()).hexdigest()[:16]
    phase = "r" if rollback else "d"
    return f"webapp-{phase}-{deployment_id}-{digest}"


def _publish_targets(deployment, runner_ids, rollback=False):
    from mojo.apps import jobs

    expected_release = (
        deployment.previous_release_id if rollback else deployment.release_id)
    targets = []
    for runner_id in runner_ids:
        job_id = jobs.publish(
            func=NODE_JOB,
            payload={
                "deployment": deployment.pk,
                "expected_release": expected_release,
                "rollback": bool(rollback),
            },
            channel=runner_id,
            idempotency_key=_job_key(deployment.pk, runner_id, rollback),
            max_retries=0,
            expires_in=deploy_timeout(),
            max_exec_seconds=deploy_timeout())
        targets.append({"runner": runner_id, "job": job_id})
    return targets


def _job_rows(targets):
    from mojo.apps.jobs.models import Job

    ids = [row.get("job") for row in targets if row.get("job")]
    return {row.pk: row for row in Job.objects.filter(pk__in=ids)}


def target_status(targets):
    jobs_by_id = _job_rows(targets)
    rows = []
    for target in targets or []:
        job = jobs_by_id.get(target.get("job"))
        rows.append({
            "runner": target.get("runner"),
            "job": target.get("job"),
            "status": job.status if job else "missing",
            "error": (job.last_error or "")[:2000] if job else "job row missing",
            "result": (job.metadata or {}).get("webapp_deployment") if job else None,
        })
    return rows


def _wait(targets, deployment_id, expected_release_id):
    deadline = time.monotonic() + deploy_timeout()
    while time.monotonic() < deadline:
        deployment = WebAppDeployment.objects.select_related("webapp").get(
            pk=deployment_id)
        if deployment.webapp.current_release_id != expected_release_id:
            return "superseded", target_status(targets)

        rows = target_status(targets)
        if rows and all(row["status"] in JOB_TERMINAL for row in rows):
            if all(row["status"] == "completed" for row in rows):
                return "completed", rows
            return "failed", rows
        time.sleep(POLL_INTERVAL)
    return "timeout", target_status(targets)


def _set_terminal(deployment_id, status, detail=""):
    WebAppDeployment.objects.filter(pk=deployment_id).update(
        status=status, detail=detail, finished=timezone.now())


def _mark_live(deployment_id, runner_count):
    """Mark live only while this deployment still owns desired state."""
    with transaction.atomic():
        deployment = WebAppDeployment.objects.select_for_update().get(
            pk=deployment_id)
        webapp = WebApp.objects.select_for_update().get(pk=deployment.webapp_id)
        if webapp.current_release_id != deployment.release_id:
            deployment.status = STATUS_SUPERSEDED
            deployment.detail = "a newer deployment became desired"
            deployment.finished = timezone.now()
            deployment.save(update_fields=[
                "status", "detail", "finished", "modified"])
            return False
        deployment.status = STATUS_LIVE
        deployment.detail = f"live on {runner_count} active runner(s)"
        deployment.finished = timezone.now()
        deployment.save(update_fields=[
            "status", "detail", "finished", "modified"])
        return True


def _failure_detail(outcome, rows):
    failures = []
    for row in rows:
        if row.get("status") != "completed":
            message = row.get("error") or row.get("status")
            failures.append(f"{row.get('runner')}: {message}")
    suffix = "; ".join(failures[:10])
    return f"fleet deployment {outcome}" + (f": {suffix}" if suffix else "")


def _restore_previous(deployment_id, detail):
    """Restore desired state only while this deployment still owns it."""
    with transaction.atomic():
        # Lock only the deployment row. PostgreSQL refuses FOR UPDATE across
        # the nullable previous_release outer join.
        deployment = WebAppDeployment.objects.select_for_update().get(
            pk=deployment_id)
        webapp = WebApp.objects.select_for_update().get(pk=deployment.webapp_id)
        if webapp.current_release_id != deployment.release_id:
            _set_terminal(deployment.pk, STATUS_SUPERSEDED,
                          "superseded while deployment was failing")
            return None

        deployment.status = STATUS_ROLLING_BACK
        deployment.detail = detail
        deployment.save(update_fields=["status", "detail", "modified"])

        webapp.current_release = deployment.previous_release
        webapp.save(update_fields=["current_release", "modified"])

        deployment.release.status = (
            deployment.release_previous_status or RELEASE_UPLOADED)
        deployment.release.save(update_fields=["status", "modified"])
        if deployment.previous_release is not None:
            deployment.previous_release.status = RELEASE_LIVE
            deployment.previous_release.save(update_fields=["status", "modified"])
        return deployment


def orchestrate(deployment_id):
    """Converge the active edge-runner snapshot or restore prior state."""
    deployment = WebAppDeployment.objects.select_related(
        "webapp__vhost", "release", "previous_release").filter(
            pk=deployment_id).first()
    if deployment is None:
        # The WebApp and its deployments can be deleted (safe-delete) while a
        # deploy job is still queued. A vanished row is a superseded no-op, not
        # a crashing DoesNotExist — the vhost delete already dropped serving.
        return "superseded:deleted"
    if deployment.status not in (STATUS_QUEUED, STATUS_DEPLOYING):
        return f"ignored:{deployment.status}"
    if deployment.webapp.current_release_id != deployment.release_id:
        _set_terminal(deployment.pk, STATUS_SUPERSEDED,
                      "a newer deployment owns the WebApp")
        return "superseded"

    if deployment.webapp.vhost_id is None:
        _set_terminal(deployment.pk, STATUS_LIVE,
                      "verified release has no vhost fleet target")
        return "live:no-vhost"

    try:
        runners = _alive_edge_runners()
        roster_error = False
    except Exception:
        runners = []
        roster_error = True
    deployment.status = STATUS_DEPLOYING
    deployment.started = deployment.started or timezone.now()
    deployment.save(update_fields=["status", "started", "modified"])

    if roster_error:
        outcome, rows = "runner-roster-unavailable", []
    elif not runners:
        outcome, rows = "no-active-runners", []
    else:
        targets = _publish_targets(deployment, runners)
        deployment.targets = targets
        deployment.save(update_fields=["targets", "modified"])
        outcome, rows = _wait(targets, deployment.pk, deployment.release_id)

    if outcome == "superseded":
        _set_terminal(deployment.pk, STATUS_SUPERSEDED,
                      "a newer deployment became desired")
        return "superseded"
    if outcome == "completed":
        if _mark_live(deployment.pk, len(rows)):
            return f"live:{len(rows)}"
        return "superseded"

    detail = _failure_detail(outcome, rows)
    restored = _restore_previous(deployment.pk, detail)
    if restored is None:
        return "superseded"

    if not runners:
        _set_terminal(restored.pk, STATUS_ROLLED_BACK, detail)
        return "rolled_back"

    rollback_targets = _publish_targets(restored, runners, rollback=True)
    restored.rollback_targets = rollback_targets
    restored.save(update_fields=["rollback_targets", "modified"])
    rollback_outcome, rollback_rows = _wait(
        rollback_targets, restored.pk, restored.previous_release_id)
    if rollback_outcome == "superseded":
        _set_terminal(restored.pk, STATUS_SUPERSEDED,
                      "a newer deployment became desired during rollback")
        return "superseded"
    if rollback_outcome == "completed":
        _set_terminal(restored.pk, STATUS_ROLLED_BACK, detail)
        return "rolled_back"

    rollback_detail = _failure_detail(rollback_outcome, rollback_rows)
    _set_terminal(restored.pk, STATUS_FAILED,
                  f"{detail}; rollback convergence {rollback_detail}")
    return "failed"


def _alias_warnings(deployment, pending, excluded):
    """Prove the app's ALIAS addresses softly — never fail the node on one.

    The PRIMARY address is the deployment's contract and keeps its hard proof
    above. An alias is an extra custom domain: one of them lagging (bytes still
    landing, a vhost excluded from this generation) is a transient the fleet
    sweep retries on its own. Failing the node instead would roll the whole
    release back — and, because the identical check runs during ROLLBACK, one
    customer domain's transient problem would terminally fail every deploy for
    that app.

    Reported as named warnings on this node's job result (visible through
    `target_status`) and logged. They are deliberately NOT written onto
    `WebAppDeployment.detail`: several nodes write concurrently, and
    orchestrate overwrites that field with the terminal outcome anyway.
    """
    from mojo.apps.edge.models import Vhost

    warnings = []
    for alias in Vhost.objects.filter(
            alias_of_id=deployment.webapp_id, is_enabled=True).select_related(
                "domain").order_by("pk"):
        if str(alias.pk) in pending:
            warnings.append(
                f"{alias.server_name}: release bytes still pending "
                f"({pending[str(alias.pk)]})")
        elif alias.pk in excluded:
            warnings.append(
                f"{alias.server_name}: excluded from this generation")
    for warning in warnings:
        logger.warning(
            f"edge WebApp deployment {deployment.pk}: alias {warning}")
    return warnings


def install_node(job):
    """Install desired state and prove this deployment's vhost is healthy."""
    from mojo.apps.edge.services import installer

    payload = job.payload or {}
    deployment = WebAppDeployment.objects.select_related(
        "webapp__vhost").get(pk=payload.get("deployment"))
    expected_release = payload.get("expected_release")
    if deployment.webapp.current_release_id != expected_release:
        result = {"status": "superseded"}
        job.metadata["webapp_deployment"] = result
        job.save(update_fields=["metadata", "modified"])
        return "superseded"

    vhost = deployment.webapp.vhost
    installed = installer.install(pool=vhost.pool, force=True)
    pending = installed.get("www_pending") or {}
    excluded = installed.get("excluded") or []
    if str(vhost.pk) in pending:
        raise RuntimeError(
            f"release bytes remain pending for vhost {vhost.pk}: "
            f"{pending[str(vhost.pk)]}")
    if vhost.pk in excluded:
        raise RuntimeError(f"vhost {vhost.pk} was excluded from the generation")

    result = {
        "status": "installed",
        "generation": installed.get("generation"),
        "changed": bool(installed.get("changed")),
        "rollback": bool(payload.get("rollback")),
        "alias_warnings": _alias_warnings(deployment, pending, excluded),
    }
    job.metadata["webapp_deployment"] = result
    job.save(update_fields=["metadata", "modified"])
    return f"installed:{installed.get('generation')}"


def payload(deployment):
    """CI-safe deployment status; no generic jobs permission required."""
    return {
        "deployment": deployment.pk,
        "webapp": deployment.webapp_id,
        "release": deployment.release_id,
        "version": deployment.release.version,
        "status": deployment.status,
        "terminal": deployment.is_terminal,
        "success": deployment.status == STATUS_LIVE,
        "detail": deployment.detail,
        "created": deployment.created.isoformat(),
        "started": deployment.started.isoformat() if deployment.started else None,
        "finished": deployment.finished.isoformat() if deployment.finished else None,
        "targets": target_status(deployment.targets),
        "rollback_targets": target_status(deployment.rollback_targets),
    }
