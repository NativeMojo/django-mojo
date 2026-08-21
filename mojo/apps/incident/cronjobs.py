from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers.settings import settings

HEALTH_MONITORING_ENABLED = settings.get_static("HEALTH_MONITORING_ENABLED", False)
LLM_TRIAGE_ENABLED = settings.get_static("LLM_HANDLER_API_KEY", None)

_health_defaults_checked = False

# The exact RuleSet names ensure_health_rules() creates. The guard below must
# match on these, never on the `system:health:` category prefix: any other
# RuleSet in that namespace — one created by `aws-check --apply`, which the
# shipped docs run BEFORE the cron — would make a prefix guard permanently
# true, so Runner Down / Scheduler Missing / TCP Overload would never be
# installed and a level-10 runner-down event would fall through to the
# handler-less catch-all: no notify, no ticket.
HEALTH_RULE_NAMES = (
    "Health - Runner Down",
    "Health - Scheduler Missing",
    "Health - TCP Connection Overload",
)

def _ensure_health_defaults():
    global _health_defaults_checked
    if not _health_defaults_checked:
        try:
            from mojo.apps.incident.models import RuleSet
            installed = set(RuleSet.objects.filter(
                name__in=HEALTH_RULE_NAMES).values_list("name", flat=True))
            if installed != set(HEALTH_RULE_NAMES):
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


@schedule(minutes="15", hours="8")
def prune_mojosec_receipts(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.services.mojosec.prune_receipts",
        channel="cleanup", payload={})


@schedule(minutes="25", hours="8")
def prune_mojosec_learning(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.services.mojosec_learning.prune_learning_evaluations",
        channel="cleanup", payload={})


@schedule(minutes="*/5")
def replay_mojosec_handler_outbox(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.services.mojosec.replay_handler_outbox",
        channel="cleanup", payload={})


# Settles quiet MojoSec deployment cases, heals crashed case projections and
# re-drives stranded case-routed receipts. System transitions only — no Events.
@schedule(minutes="*/5")
def settle_mojosec_cases(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.services.mojosec_correlation.settle_sweep",
        channel="cleanup", payload={})


# Runs every 5 minutes — unblocks IPs whose blocked_until has passed
@schedule(minutes="*/5")
def sweep_expired_blocks(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.sweep_expired_blocks",
        payload={})


# Hourly — rebuild all ipsets from DB truth (startup recovery + drift reconciliation)
@schedule(minutes="0")
def sync_firewall(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.sync_firewall",
        channel="cleanup", payload={})


# Weekly — refresh IPSet sources (countries, abuse lists) and sync to fleet
@schedule(minutes="0", hours="3", weekdays="0")
def refresh_ipsets(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.refresh_ipsets",
        payload={})


# Every 6h — refresh the cache-only threat lists (tor_exits, blocklist_de)
# used by geoip detection. refresh_from_source() ONLY — never sync(): these
# rows are is_enabled=False and must never reach the kernel firewall.
@schedule(minutes="30", hours="*/6")
def refresh_threat_lists(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.refresh_threat_lists",
        payload={})


# Daily — recompute threat_level for recently-active IPs so a stale escalation
# can decay. Everything else in the system only ratchets up.
@schedule(minutes="20", hours="4")
def recheck_active_threats(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.recheck_active_threats",
        channel="cleanup", payload={})


# Twice a day — triage any new incidents that haven't been LLM-assessed yet
@schedule(hours="9,18")
def triage_new_incidents(force=False, verbose=False, now=None):
    if not LLM_TRIAGE_ENABLED:
        return
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.triage_new_incidents",
        channel="incident_handlers", payload={})


# Every 5 minutes — detect traffic concentration by one authenticated
# identity (DM-042). Reads the accounting counters the API throttle maintains;
# zero request-path cost.
@schedule(minutes="*/5")
def check_traffic_concentration(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.check_traffic_concentration",
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
