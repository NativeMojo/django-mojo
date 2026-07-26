from mojo.decorators.cron import schedule
from mojo.helpers import logit


# The cron field values MUST be strings. `minutes=5` raises a TypeError inside
# the matcher and the job then silently never runs — see mojo/helpers/cron.py
# and the note in mojo/apps/shortlink/cronjobs.py.
@schedule(minutes="*/5")
def poll_domain_operations():
    """
    Advance in-flight domain registrations.

    Also reconciles the crash window: a purchase row that is `submitted` with
    no operation id is one where we may have spent money without recording
    where it went, so the poller probes the registrar's operation list for it.
    """
    from mojo.apps.dnsman.services import registrar

    result = registrar.poll_pending()
    logit.info(f"dnsman: poll_pending {result}")
    return result


@schedule(minutes="0", hours="*/6")
def renew_certificates():
    """Queue renewal for every certificate past its renew_after mark."""
    from mojo.apps.dnsman.services import certs

    result = certs.renew_due()
    if result.count:
        logit.info(f"dnsman: queued {result.count} certificate renewals")
    return result
