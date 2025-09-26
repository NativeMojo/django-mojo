from mojo.decorators.cron import schedule
from mojo.apps import jobs


# Runs hourly at the configured minute (default 0)
@schedule(minutes="45", hours="9")
def prune_events(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.prune_events",
        channel="cleanup", payload={})
