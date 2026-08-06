"""Async AWS maintenance jobs."""

from urllib.parse import urlparse

from mojo.apps.aws.services import version_drift
from mojo.apps.aws.services.aws_check import _safe_slug
from mojo.apps.incident import reporter
from mojo.helpers import logit
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_versions", "aws.log")

# The ticket category the opt-in RuleSet's ticket:// handler stamps.
TICKET_CATEGORY = "aws-version-drift"


def _setting(name, default=None):
    try:
        return settings.get_static(name, default)
    except Exception:
        return default


def _deployment_slug():
    configured = _setting("AWS_MONITORING_NAME", "") or ""
    host = urlparse(_setting("BASE_URL", "") or "").hostname or "deployment"
    return _safe_slug(configured or host)


def _title(report):
    findings = report.get("findings") or []
    if not findings:
        return "AWS version drift scan was incomplete"
    worst = min(
        (finding["days_remaining"] for finding in findings
         if finding.get("days_remaining") is not None),
        default=None,
    )
    if worst is not None and worst < 0:
        return f"AWS managed-service support has ENDED for {len(findings)} resource(s)"
    if worst is not None:
        return f"AWS managed-service support ends in {worst} days ({len(findings)} resource(s))"
    return f"AWS major version upgrades available for {len(findings)} resource(s)"


def _details(report):
    lines = [
        f"AWS managed-service version drift in {report.get('region')}:",
        "",
    ]
    for finding in report.get("findings") or []:
        deadline = finding.get("deadline") or "no published end-of-life date"
        line = (f"- {finding.get('kind')} {finding.get('resource_id')}: "
                f"{finding.get('engine')} {finding.get('current_version')} -> "
                f"{finding.get('available_major')} (standard support ends {deadline}")
        if finding.get("days_remaining") is not None:
            line += f", {finding['days_remaining']} days remaining"
        line += ")"
        if finding.get("extended_deadline"):
            line += f"\n    paid extended support ends {finding['extended_deadline']}"
        lines.append(line)
    warnings = report.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Some APIs could not be inventoried:")
        for warning in warnings:
            lines.append(f"- {warning.get('message')}")
    return "\n".join(lines)


def check_version_drift(job):
    """Inventory managed-service versions and file at most ONE incident event.

    No level-1 "everything is current" liveness event is filed. The catch-all
    RuleSet matches Level >= 1 with no handler, and `rule_set or level >=
    threshold` is then true, so a liveness event would manufacture a permanent
    Incident in the operator's queue on every run. Cron liveness is already
    covered by the heartbeats aws_check.check_cron reads.
    """
    report = version_drift.scan()
    if report.get("status") != "ok":
        message = f"AWS version drift scan unavailable: {report.get('reason')}"
        logger.warning(message)
        job.add_log(message)
        return

    findings = report.get("findings") or []
    warnings = report.get("warnings") or []
    level = int(report.get("level") or 1)
    if level <= 1:
        job.add_log("AWS managed-service versions are current; no event filed")
        return

    details = _details(report)
    reporter.report_event(
        details,
        title=_title(report),
        # The EVENT category keeps the health-strip row; the RuleSet is matched
        # through `scope`, which is deliberately outside system:health:.
        category=version_drift.CATEGORY,
        level=level,
        scope=version_drift.RULESET_CATEGORY,
        hostname=_deployment_slug(),
        findings=findings,
        warnings=warnings,
        region=report.get("region"),
        drift_schema_version=report.get("schema_version"),
        group=None,
    )
    job.add_log(
        f"Filed AWS version drift event at level {level} "
        f"({len(findings)} findings, {len(warnings)} warnings)")
    _update_open_ticket(details, level, job)


def _update_open_ticket(description, level, job):
    """Keep the already-open drift ticket current.

    TicketHandler's reuse branch only adds a "Recurring incident #N" note — it
    touches neither description nor priority — and Ticket.enqueue_sync fires
    only from REST saves. Without this the board item would stay frozen at
    whatever the first run said while the deadline approaches, and escalation
    is the entire point of the feature.
    """
    from mojo.apps.incident.models import Ticket

    try:
        ticket = Ticket.objects.filter(category=TICKET_CATEGORY).exclude(
            status__in=("resolved", "closed")).order_by("-created").first()
        if ticket is None:
            return
        priority = max(int(level), 8)
        changed = []
        if ticket.description != description:
            ticket.description = description
            changed.append("description")
        if ticket.priority != priority:
            ticket.priority = priority
            changed.append("priority")
        if not changed:
            return
        ticket.save(update_fields=changed + ["modified"])
        job.add_log(f"Updated open drift ticket #{ticket.pk}: {', '.join(changed)}")
        from mojo.apps.incident.services import maestro_sync
        for link_id in ticket.maestro_links.values_list("id", flat=True):
            maestro_sync.enqueue_sync(link_id, sorted(changed))
    except Exception as err:
        logger.exception("version drift: failed to update the open drift ticket")
        job.add_log(f"Could not update the open drift ticket: {err}", kind="error")
