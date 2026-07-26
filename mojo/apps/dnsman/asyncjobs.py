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


def certificate_updated(job):
    """
    Broadcast handler for the certificate sync channel.

    Published to every runner on ``DNSMAN_CERT_SYNC_CHANNEL`` whenever a
    certificate is issued or renewed. The payload deliberately carries only
    identifiers — a consumer that serves this certificate reacts by pulling
    the material through the gated endpoint with its own credentials.

    dnsman itself has nothing to install, so the framework handler only logs.
    """
    payload = job.payload or {}
    logit.info(
        f"dnsman: certificate {payload.get('certificate')} for "
        f"{payload.get('domain')} changed (not_after={payload.get('not_after')})")
    return f"completed:certificate={payload.get('certificate')}"
