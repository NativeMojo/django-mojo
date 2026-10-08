from mojo.decorators.cron import schedule
from mojo.apps import jobs


@schedule(minutes="0", hours="*")
def prune_notifications():
    jobs.publish(
        func="mojo.apps.account.asyncjobs.prune_notifications",
        channel="cleanup",
        payload={},
    )


@schedule(minutes="*/15")
def refresh_bouncer_sig_cache():
    jobs.publish(
        func="mojo.apps.account.asyncjobs.refresh_bouncer_sig_cache",
        channel="cleanup",
        payload={},
    )


@schedule(minutes="0", hours="3")
def inactive_sweep():
    jobs.publish(
        func="mojo.apps.account.asyncjobs.inactive_sweep",
        channel="cleanup",
        payload={},
    )
