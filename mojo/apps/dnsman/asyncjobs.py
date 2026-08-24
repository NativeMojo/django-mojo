"""
dnsman background job handlers.

Certificate issuance is a multi-minute conversation — a CA round trip per
authorization, a DNS propagation wait, then finalize and download — so it runs
on a job runner and never inside a request.

Handlers take the Job row (``def handler(job):``) and read ``job.payload``.
The job engine treats a raised exception as a job failure, so a failed
issuance is raised here *after* the Certificate row has already recorded its
own ``status``/``last_error``: the row is the durable truth, the raise is what
makes the failure visible in the jobs surface too.
"""

from mojo import errors as me
from mojo.apps import jobs
from mojo.helpers import logit


def _certificate(job):
    """Load the Certificate a job names, or None when it has since vanished."""
    from mojo.apps.dnsman.models import Certificate

    pk = (job.payload or {}).get("certificate")
    if not pk:
        raise me.ValueException("The job payload named no certificate")
    return Certificate.objects.filter(pk=pk).select_related("domain").last()


def _run(job, what):
    from mojo.apps.dnsman.services import certs

    pk = (job.payload or {}).get("certificate")
    cert = _certificate(job)
    if cert is None:
        # A domain delete cascades its certificates away; a job still in the
        # queue for one is stale, not broken.
        logit.info(f"dnsman: {what} — certificate {pk} no longer exists, skipping")
        return f"completed:missing={pk}"

    result = certs.issue(cert)
    if not result.ok:
        raise me.ValueException(
            f"dnsman: {what} failed for certificate {cert.pk} "
            f"({cert.common_name}): {result.error}")
    return f"completed:certificate={cert.pk}"


def issue_certificate(job):
    """Issue the certificate named in the payload."""
    return _run(job, "issue_certificate")


def renew_certificate(job):
    """
    Reissue the certificate named in the payload.

    Renewal is issuance: ACME has no separate renew operation, and reusing the
    same code path means a renewal cannot drift from a first issuance.
    """
    return _run(job, "renew_certificate")


def poll_domain_operations(job):
    """
    Advance every in-flight domain registration.

    Runs here rather than inline in the cron function because the sweep makes
    a registrar round trip PER open purchase row (operation detail, and for the
    crash window a `list_operations` probe plus zone/expiry lookups) and takes
    a row lock to settle each one. That is unbounded by row count and must not
    occupy whatever thread the cron matcher happens to fire on.

    `registrar.poll_pending()` is idempotent and status-guarded, so a retry —
    or an overlapping run if a sweep outlives its five-minute interval — settles
    each row at most once.
    """
    from mojo.apps.dnsman.services import registrar

    result = registrar.poll_pending()
    if result.errors:
        logit.error(f"dnsman: poll_pending finished with errors {result}")
    else:
        logit.info(f"dnsman: poll_pending {result}")
    return (f"completed:completed={result.completed},failed={result.failed},"
            f"adopted={result.adopted},expired={result.expired},errors={result.errors}")


def _report_acme_hub_sweep_errors(result, report=None):
    """File one suppressed incident for a sweep pass that hit provider errors.

    ``report`` is an injection seam for tests, replacing
    ``incident.reporter.report_event_suppressed``.

    This exists because the hub stopped blocking its HTTP reply on propagation
    (item #2851). A stuck reconciliation used to surface as a loud 503 on the
    tenant's own publish call; now it is a `reconciled_at IS NULL` row that
    nothing reads. One incident per hour is the operator-facing replacement.
    """
    if report is None:
        from mojo.apps.incident.reporter import report_event_suppressed

        report = report_event_suppressed
    try:
        report(
            f"ACME hub sweep finished with {result.errors} error(s): "
            f"expired={result.expired} reconciled={result.reconciled} "
            f"unconfirmed={result.get('unconfirmed', 0)}",
            key="sweep",
            title="dnsman ACME hub sweep errors",
            category="dnsman:acme_hub:sweep",
            level=3, window=3600, group=None)
    except Exception as err:
        # Bookkeeping must never turn a completed sweep into a failed job.
        logit.error(f"dnsman: could not report the ACME hub sweep errors: {err}")


def sweep_acme_hub_leases(job, report=None):
    """Expire and reconcile durable ACME hub challenge leases.

    ``report`` is an injection seam for tests, threaded to
    ``_report_acme_hub_sweep_errors``.
    """
    from mojo.apps.dnsman.services import acme_hub

    result = acme_hub.sweep()
    unconfirmed = result.get("unconfirmed", 0)
    if result.errors:
        logit.error(
            f"dnsman: ACME hub sweep finished with {result.errors} errors "
            f"(unconfirmed={unconfirmed})")
        _report_acme_hub_sweep_errors(result, report=report)
    else:
        logit.info(
            f"dnsman: ACME hub sweep expired={result.expired} "
            f"reconciled={result.reconciled} unconfirmed={unconfirmed}")
    return (f"completed:expired={result.expired},"
            f"reconciled={result.reconciled},errors={result.errors}")


def publish_certificate_expiry_metric(job, put_metric=None):
    """
    Publish the soonest certificate expiry as a CloudWatch custom metric.

    ``put_metric`` is an injection seam for tests, replacing
    ``cloudwatch.put_metric_data`` (None means the real CloudWatch write).

    One number for the whole deployment, because one alarm on it catches every
    cause of a stalled renewal at once — publisher down, challenge misrouted,
    credentials wrong, delegation record deleted. The alarm (created by
    `aws-check`) treats missing data as breaching, so this job going quiet is
    itself the signal.

    `status` deliberately includes FAILED. `certs._record_issue_failure` moves a
    certificate to FAILED once its material is no longer valid, so an
    ACTIVE-only filter would drop a certificate at the exact moment it expires —
    the published minimum would jump to the next-soonest certificate and
    CloudWatch would report the alarm RECOVERED while TLS is broken.
    """
    from django.db.models import Min

    from mojo.apps.aws.services.aws_check import (
        CERT_METRIC_NAME, CERT_METRIC_NAMESPACE, deployment_slug)
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE, STATUS_FAILED
    from mojo.helpers import dates
    from mojo.helpers.aws import cloudwatch

    soonest = Certificate.objects.filter(
        status__in=(STATUS_ACTIVE, STATUS_FAILED),
        not_after__isnull=False,
    ).aggregate(soonest=Min("not_after"))["soonest"]
    if soonest is None:
        # Publish nothing rather than a sentinel: a deployment with no
        # certificates should show as un-set-up, not as healthy.
        logit.info("dnsman: no certificates to report an expiry metric for")
        return "completed:published=0"

    # Not clamped at zero — an expired certificate reading -3 is more
    # diagnostic than 0, and the alarm threshold catches both.
    days = (soonest - dates.utcnow()).days
    slug = deployment_slug()
    if put_metric is None:
        put_metric = cloudwatch.put_metric_data
    put_metric(
        CERT_METRIC_NAMESPACE, CERT_METRIC_NAME, days,
        dimensions=[{"Name": "Deployment", "Value": slug}], unit="Count")
    logit.info(f"dnsman: published {CERT_METRIC_NAME}={days} for {slug}")
    return f"completed:days={days}"


def certificate_updated(job):
    """
    Broadcast handler for the certificate sync channel.

    Published to every runner on ``DNSMAN_CERT_SYNC_CHANNEL`` whenever a
    certificate is issued or renewed. The payload deliberately carries only
    identifiers — a consumer that serves this certificate reacts by pulling
    the material through the gated endpoint with its own credentials.

    dnsman itself has nothing to install — it hands off to the edge app, which
    owns the node-side generation lifecycle (vhosts, certificate material on
    disk, `nginx -t`, reload). The handoff is a separate broadcast rather than
    an inline call because installing is filesystem- and subprocess-bound and
    belongs on the edge channel with its own retries and job visibility.
    """
    payload = job.payload or {}
    logit.info(
        f"dnsman: certificate {payload.get('certificate')} for "
        f"{payload.get('domain')} changed (not_after={payload.get('not_after')})")

    # Best-effort, exactly like the broadcast that got us here: the certificate
    # is already issued and stored, and a failure to schedule the install must
    # not undo that. The edge convergence sweep picks it up regardless.
    try:
        from mojo.apps.edge.cronjobs import (
            CONVERGE_JOB, EDGE_CHANNEL, converge_pools)

        jobs.publish(
            func=CONVERGE_JOB,
            payload={"pools": converge_pools()},
            channel=EDGE_CHANNEL,
            broadcast=True)
    except Exception as err:
        logit.error(f"dnsman: could not schedule an edge install: {err}")

    return f"completed:certificate={payload.get('certificate')}"
