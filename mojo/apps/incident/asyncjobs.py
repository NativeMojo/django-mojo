from mojo.apps.incident.models import Event
from django.utils import timezone
from datetime import timedelta
from mojo.helpers.settings import settings
from mojo.helpers import logit

# Default: check once an hour at minute 0 (can be overridden in settings)
INCIDENT_EVENT_PRUNE_DAYS = settings.get_static("INCIDENT_EVENT_PRUNE_DAYS", 30)
INCIDENT_PRUNE_DAYS = settings.get_static("INCIDENT_PRUNE_DAYS", 90)
FIREWALL_BLOCKED_IPSET_NAME = settings.get_static("FIREWALL_BLOCKED_IPSET_NAME", "mojo_blocked")


def prune_events(job):
    qset = Event.objects.filter(
        created__lt=timezone.now() - timedelta(days=INCIDENT_EVENT_PRUNE_DAYS),
        level__lt=6)
    qset.delete()


def prune_incidents(job):
    from django.db.models import Q
    from mojo.apps.incident.models import Incident
    cutoff = timezone.now() - timedelta(days=INCIDENT_PRUNE_DAYS)
    # Never prune incidents referenced by a ticket — the ticket is
    # evidence the incident was serious enough to keep.
    qset = Incident.objects.filter(
        created__lt=cutoff,
        status__in=("resolved", "closed", "ignored"),
        tickets__isnull=True,
    ).filter(
        Q(metadata__do_not_delete=False)
        | ~Q(metadata__has_key="do_not_delete"),
    )
    count = qset.count()
    if count:
        qset.delete()
        job.add_log(f"Pruned {count} incidents older than {INCIDENT_PRUNE_DAYS} days")
    else:
        job.add_log("No incidents to prune")


def broadcast_block_ip(data):
    """Broadcast handler — receives plain dict from pub/sub, not a Job.

    Applies iptables blocks on the local instance.
    Called via jobs.broadcast_execute() so it runs on every runner.

    Expected data keys:
        ips: list of IP strings to block
        ttl: seconds before auto-unblock (default 600, 0 = permanent)
    """
    from mojo.apps.incident import firewall

    ips = data.get("ips", [])
    ttl = data.get("ttl", 600)

    if not ips:
        logit.warning("broadcast_block_ip called with no IPs")
        return

    blocked = []
    for ip in ips:
        ip = str(ip).strip()
        if not ip:
            continue
        if firewall.block(ip):
            blocked.append(ip)

    logit.info("broadcast_block_ip: blocked %d/%d IPs (ttl=%ds): %s", len(blocked), len(ips), ttl, blocked)
    # No delayed unblock scheduled here — the sweep_expired_blocks cron
    # handles expiry every minute via GeoLocatedIP.blocked_until


def broadcast_unblock_ip(data):
    """Broadcast handler — receives plain dict from pub/sub, not a Job.

    Removes iptables blocks on the local instance.
    Called by sweep_expired_blocks or manually via admin unblock.

    Expected data keys:
        ips: list of IP strings to unblock
    """
    from mojo.apps.incident import firewall

    ips = data.get("ips", [])

    if not ips:
        return

    unblocked = []
    for ip in ips:
        ip = str(ip).strip()
        if not ip:
            continue
        if firewall.unblock(ip):
            unblocked.append(ip)

    logit.info("broadcast_unblock_ip: unblocked %d/%d IPs: %s", len(unblocked), len(ips), unblocked)


def broadcast_ipset_add_blocked(data):
    """Broadcast handler — adds a single IP to the permanent block ipset.

    Expected data keys:
        ip: IP address string to add
    """
    from mojo.apps.incident import firewall

    ip = data.get("ip")
    if not ip:
        return

    if firewall.ipset_add(FIREWALL_BLOCKED_IPSET_NAME, ip):
        logit.info("broadcast_ipset_add_blocked: added %s to %s", ip, FIREWALL_BLOCKED_IPSET_NAME)


def broadcast_ipset_del_blocked(data):
    """Broadcast handler — removes a single IP from the permanent block ipset.

    Expected data keys:
        ip: IP address string to remove
    """
    from mojo.apps.incident import firewall

    ip = data.get("ip")
    if not ip:
        return

    if firewall.ipset_del(FIREWALL_BLOCKED_IPSET_NAME, ip):
        logit.info("broadcast_ipset_del_blocked: removed %s from %s", ip, FIREWALL_BLOCKED_IPSET_NAME)


SYNC_FIREWALL_REDIS_KEY = "mojo:sync_firewall:last_sync"


def sync_firewall(job):
    """
    Hourly reconciliation job: restores ipsets from DB truth.
    Doubles as startup recovery — first run after restart restores all blocks.

    Skips ipsets that haven't changed since the last sync to stay lightweight.
    Uses ipset restore (batch stdin) for fast bulk loading.

    1. Load permanently blocked IPs into mojo_blocked (if changed)
    2. Load enabled IPSet records that have been modified since last sync
    """
    from mojo.apps.incident import firewall
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.helpers import dates

    redis_client = get_adapter()
    now = dates.utcnow()

    # Check when we last synced permanent blocks
    last_sync_raw = redis_client.get(SYNC_FIREWALL_REDIS_KEY)
    last_sync = None
    if last_sync_raw:
        try:
            last_sync = dates.parse(last_sync_raw)
        except Exception:
            pass

    # Permanent blocks → mojo_blocked ipset (only if changed since last sync)
    perm_query = GeoLocatedIP.objects.filter(
        is_blocked=True,
        blocked_until__isnull=True,
    )
    if last_sync and not perm_query.filter(modified__gt=last_sync).exists():
        job.add_log(f"sync_firewall: {FIREWALL_BLOCKED_IPSET_NAME} unchanged, skipped")
    else:
        permanent_ips = list(perm_query.values_list("ip_address", flat=True))
        ok, loaded = firewall.ipset_load(FIREWALL_BLOCKED_IPSET_NAME, permanent_ips)
        job.add_log(f"sync_firewall: loaded {loaded}/{len(permanent_ips)} permanent blocks into {FIREWALL_BLOCKED_IPSET_NAME}")

    # Enabled IPSets — skip those unchanged since last sync
    ipsets = IPSet.objects.filter(is_enabled=True)
    synced = 0
    skipped = 0
    for ipset in ipsets:
        if last_sync and ipset.last_synced and ipset.modified <= last_sync:
            skipped += 1
            continue
        cidrs = ipset.cidrs
        ok, count = firewall.ipset_load(ipset.name, cidrs)
        if ok:
            synced += 1
            job.add_log(f"sync_firewall: loaded {count}/{len(cidrs)} CIDRs into {ipset.name}")

    if skipped:
        job.add_log(f"sync_firewall: skipped {skipped} unchanged IPSets")

    # Record sync time for next run (TTL = 2x hourly interval as safety net)
    redis_client.set(SYNC_FIREWALL_REDIS_KEY, now.isoformat(), ex=7200)


def sweep_expired_blocks(job):
    """
    Cron job (every minute): finds all IPs where blocked_until has passed,
    unblocks them in the DB, and broadcasts fleet-wide iptables removal.
    """
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.helpers import dates
    from mojo.apps import jobs

    expired = list(
        GeoLocatedIP.objects.filter(
            is_blocked=True,
            blocked_until__isnull=False,
            blocked_until__lte=dates.utcnow(),
        ).values_list("ip_address", flat=True)
    )

    if not expired:
        return

    # DB update in bulk
    GeoLocatedIP.objects.filter(
        ip_address__in=expired
    ).update(
        is_blocked=False,
        blocked_reason="expired",
        blocked_until=None,
    )

    # Single broadcast to remove all expired blocks fleet-wide
    jobs.broadcast_execute(
        "mojo.apps.incident.asyncjobs.broadcast_unblock_ip",
        {"ips": expired},
    )

    job.add_log(f"Swept {len(expired)} expired blocks: {expired}")


def broadcast_sync_ipset(data):
    """Broadcast handler — receives plain dict from pub/sub, not a Job.

    Loads an ipset on the local instance.
    Called via jobs.broadcast_execute() so every instance gets the same set.

    Expected data keys:
        name: ipset name (e.g. "country_cn")
        cidrs: list of CIDR strings
    """
    from mojo.apps.incident import firewall

    name = data.get("name")
    cidrs = data.get("cidrs", [])

    if not name:
        logit.warning("broadcast_sync_ipset called with no name")
        return

    ok, loaded = firewall.ipset_load(name, cidrs)
    logit.info("broadcast_sync_ipset: ipset %s loaded %d CIDRs, success=%s", name, loaded, ok)


def broadcast_remove_ipset(data):
    """Broadcast handler — receives plain dict from pub/sub, not a Job.

    Removes an ipset from the local instance.

    Expected data keys:
        name: ipset name to remove
    """
    from mojo.apps.incident import firewall

    name = data.get("name")

    if not name:
        return

    firewall.ipset_remove(name)
    logit.info("broadcast_remove_ipset: ipset %s removed", name)


def refresh_ipsets(job):
    """
    Cron job: refreshes all enabled IPSets from their sources,
    then syncs to all instances.
    """
    from mojo.apps.incident.models import IPSet

    ipsets = IPSet.objects.filter(is_enabled=True).exclude(source="manual")
    refreshed = []
    for ipset in ipsets:
        if ipset.refresh_from_source():
            ipset.sync()
            refreshed.append(ipset.name)

    if refreshed:
        job.add_log(f"Refreshed {len(refreshed)} IPSets: {refreshed}")


def check_system_health(job):
    """
    Cron job (every 3 min): checks system health across all runners.
    Fires incident events when thresholds are breached. The rules engine
    handles escalation via threshold/bundling logic — a single spike is
    a blip, sustained problems trigger incidents.
    """
    from mojo.apps import jobs
    from mojo.apps.incident import reporter

    HEALTH_TCP_MAX = settings.get_static("HEALTH_TCP_MAX", 2000)
    HEALTH_CPU_CRIT = settings.get_static("HEALTH_CPU_CRIT", 90)
    HEALTH_MEM_CRIT = settings.get_static("HEALTH_MEM_CRIT", 90)
    HEALTH_DISK_CRIT = settings.get_static("HEALTH_DISK_CRIT", 85)

    # Check runner availability
    runners = jobs.get_runners()
    alive_ids = set()
    for runner in runners:
        if runner.get("alive"):
            alive_ids.add(runner["runner_id"])
        else:
            reporter.report_event(
                f"Runner {runner['runner_id']} is not responding",
                title=f"Runner down: {runner['runner_id']}",
                category="system:health:runner",
                level=10,
                scope="system",
                hostname=runner["runner_id"],
            )

    if not alive_ids:
        job.add_log("No alive runners found, skipping sysinfo collection")
        return

    # Collect sysinfo from all alive runners
    sysinfo = jobs.get_sysinfo(timeout=10.0)
    checked = 0

    for entry in sysinfo:
        runner_id = entry.get("runner_id", "unknown")
        if entry.get("status") != "success":
            continue

        result = entry.get("result", {})
        hostname = (result.get("os") or {}).get("hostname", runner_id)
        checked += 1

        # TCP connections
        tcp_cons = (result.get("network") or {}).get("tcp_cons", 0)
        if tcp_cons > HEALTH_TCP_MAX:
            reporter.report_event(
                f"TCP connections: {tcp_cons} (threshold: {HEALTH_TCP_MAX})",
                title=f"High TCP connections on {hostname}",
                category="system:health:tcp",
                level=8,
                scope="system",
                hostname=hostname,
            )

        # CPU
        cpu_load = result.get("cpu_load", 0)
        if cpu_load > HEALTH_CPU_CRIT:
            reporter.report_event(
                f"CPU load: {cpu_load}% (threshold: {HEALTH_CPU_CRIT}%)",
                title=f"High CPU on {hostname}",
                category="system:health:cpu",
                level=5,
                scope="system",
                hostname=hostname,
            )

        # Memory
        mem_pct = (result.get("memory") or {}).get("percent", 0)
        if mem_pct > HEALTH_MEM_CRIT:
            reporter.report_event(
                f"Memory usage: {mem_pct}% (threshold: {HEALTH_MEM_CRIT}%)",
                title=f"High memory on {hostname}",
                category="system:health:memory",
                level=5,
                scope="system",
                hostname=hostname,
            )

        # Disk
        disk_pct = (result.get("disk") or {}).get("percent", 0)
        if disk_pct > HEALTH_DISK_CRIT:
            reporter.report_event(
                f"Disk usage: {disk_pct}% (threshold: {HEALTH_DISK_CRIT}%)",
                title=f"High disk on {hostname}",
                category="system:health:disk",
                level=5,
                scope="system",
                hostname=hostname,
            )

    # Check scheduler leader lock
    try:
        from mojo.apps.jobs.keys import JobKeys
        from mojo.apps.jobs.adapters import get_adapter
        redis_client = get_adapter()
        keys = JobKeys()
        lock_key = keys.scheduler_lock()
        if not redis_client.get(lock_key):
            reporter.report_event(
                "Scheduler leader lock is missing — no scheduler may be running",
                title="Scheduler leader lock missing",
                category="system:health:scheduler",
                level=10,
                scope="system",
            )
    except Exception:
        pass

    job.add_log(f"Health check complete: {checked} runners checked, {len(alive_ids)} alive")


def triage_new_incidents(job):
    """
    Cron job: find all new, unassessed incidents and queue each for LLM triage.

    Runs periodically (every few minutes). Picks up incidents that arrived via
    rulesets without an llm:// handler — so the LLM sees everything, not just
    incidents from rules that explicitly opted in.

    Guards against double-pickup by moving each incident to "investigating"
    before publishing the job. The LLM agent takes over from there.
    """
    from mojo.apps.incident.models import Incident
    from mojo.apps import jobs

    if not settings.get("LLM_HANDLER_API_KEY"):
        return

    BATCH_SIZE = 20

    # Incidents still "new" with no LLM assessment recorded yet
    incidents = list(
        Incident.objects
        .filter(status="new")
        .exclude(metadata__has_key="llm_assessment")
        .order_by("-priority", "created")[:BATCH_SIZE]
    )

    if not incidents:
        return

    queued = 0
    for incident in incidents:
        event = Event.objects.filter(incident=incident).order_by("-created").first()
        if not event:
            continue

        # Mark investigating now so concurrent sweeps don't double-queue
        incident.status = "investigating"
        incident.save(update_fields=["status"])
        incident.add_history("handler:llm", note="[LLM Agent] Queued for automated triage")

        jobs.publish(
            "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
            {
                "event_id": event.pk,
                "incident_id": incident.pk,
                "ruleset_id": incident.rule_set_id,
            },
            channel="incident_handlers",
        )
        queued += 1

    job.add_log(f"Queued {queued}/{len(incidents)} incidents for LLM triage")


def example(job):
    job.add_log("This is an example job")
