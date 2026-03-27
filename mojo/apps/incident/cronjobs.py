from mojo.decorators.cron import schedule
from mojo.apps import jobs


# Runs hourly at the configured minute (default 0)
@schedule(minutes="45", hours="9")
def prune_events(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.prune_events",
        channel="cleanup", payload={})


# Runs every minute — unblocks IPs whose blocked_until has passed
@schedule(minutes="*")
def sweep_expired_blocks(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.sweep_expired_blocks",
        payload={})


# Weekly — refresh IPSet sources (countries, abuse lists) and sync to fleet
@schedule(minutes="0", hours="3", days_of_week="0")
def refresh_ipsets(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.refresh_ipsets",
        payload={})
