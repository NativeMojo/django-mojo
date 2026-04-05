from mojo.decorators.cron import schedule
from mojo.apps import jobs


@schedule(minutes="0", hours="*")
def prune_notifications():
    jobs.publish(
        func="mojo.apps.account.asyncjobs.prune_notifications",
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
