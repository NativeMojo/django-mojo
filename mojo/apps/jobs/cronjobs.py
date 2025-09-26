from mojo.decorators.cron import schedule
from mojo.apps import jobs


# Runs hourly at the configured minute (default 0)
@schedule(minutes="30", hours="10")
def prune_jobs(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.jobs.asyncjobs.prune_jobs",
        channel="cleanup",
        payload={})
