from mojo.decorators.cron import schedule
from mojo.apps import jobs


@schedule(minutes="30", hours="3")
def prune_expired_shortlinks():
    jobs.publish(
        func="mojo.apps.shortlink.asyncjobs.prune_expired_shortlinks",
        channel="cleanup",
        payload={},
    )
