"""
dnsman scheduled work.

Every function here is a THIN DISPATCHER: it publishes a job and returns. The
real work belongs on a job runner, which is where retries, timeouts and job
visibility live — a cron function runs synchronously on whatever process the
cron matcher fires on, so anything slow or network-bound occupies that process
for its whole duration. Same shape as every other app's cronjobs module.
"""

from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers import logit


POLL_JOB = "mojo.apps.dnsman.asyncjobs.poll_domain_operations"
ACME_HUB_SWEEP_JOB = "mojo.apps.dnsman.asyncjobs.sweep_acme_hub_leases"
CERT_EXPIRY_METRIC_JOB = "mojo.apps.dnsman.asyncjobs.publish_certificate_expiry_metric"


# The cron field values MUST be strings. `minutes=5` raises a TypeError inside
# the matcher and the job then silently never runs — see mojo/helpers/cron.py
# and the note in mojo/apps/shortlink/cronjobs.py.
@schedule(minutes="*/5")
def poll_domain_operations():
    """
    Queue the sweep that advances in-flight domain registrations.

    The sweep also reconciles the crash window: a purchase row that is
    `submitted` with no operation id is one where we may have spent money
    without recording where it went, so it probes the registrar's operation
    list for the real operation.
    """
    return jobs.publish(func=POLL_JOB, payload={})


@schedule(minutes="*/5")
def sweep_acme_hub_leases():
    """Queue durable ACME lease expiry/reconciliation work."""
    return jobs.publish(func=ACME_HUB_SWEEP_JOB, payload={})


@schedule(minutes="0", hours="*")
def publish_certificate_expiry():
    """
    Queue the deployment-wide certificate-expiry metric publish.

    Hourly, deliberately. The alarm that reads this metric treats missing data
    as breaching, so the evaluation window has to be comfortably longer than the
    publish interval — it is sized at three hourly periods, meaning three
    consecutive misses page rather than one.
    """
    return jobs.publish(func=CERT_EXPIRY_METRIC_JOB, payload={})


@schedule(minutes="0", hours="*/6")
def renew_certificates():
    """Queue renewal for every certificate past its renew_after mark."""
    from mojo.apps.dnsman.services import certs

    result = certs.renew_due()
    if result.count:
        logit.info(f"dnsman: queued {result.count} certificate renewals")
    return result
