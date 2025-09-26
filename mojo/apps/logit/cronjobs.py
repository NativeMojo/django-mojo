from mojo.decorators.cron import schedule
from mojo.apps import jobs


# Runs hourly at the configured minute (default 0)
@schedule(minutes="10", hours="9")
def prune_logit_logs(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.logit.asyncjobs.prune_logit_logs",
        channel="cleanup",
        payload={})
