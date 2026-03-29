from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers.settings import settings

HEALTH_MONITORING_ENABLED = settings.get_static("HEALTH_MONITORING_ENABLED", False)

_health_defaults_checked = False

def _ensure_health_defaults():
    global _health_defaults_checked
    if not _health_defaults_checked:
        try:
            from mojo.apps.incident.models import RuleSet
            if not RuleSet.objects.filter(category__startswith="system:health:").exists():
                RuleSet.ensure_health_rules()
        except Exception:
            pass
        _health_defaults_checked = True


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


# Every 3 minutes — check system health across all runners
@schedule(minutes="*/3")
def check_system_health(force=False, verbose=False, now=None):
    if not HEALTH_MONITORING_ENABLED:
        return
    _ensure_health_defaults()
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.check_system_health",
        payload={})
