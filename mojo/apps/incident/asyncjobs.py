from mojo.apps.incident.models import Event
from django.utils import timezone
from datetime import timedelta
from mojo.helpers.settings import settings
from mojo.helpers import logit

# Default: check once an hour at minute 0 (can be overridden in settings)
INCIDENT_EVENT_PRUNE_DAYS = settings.get_static("INCIDENT_EVENT_PRUNE_DAYS", 30)


def prune_events(job):
    qset = Event.objects.filter(
        created__lt=timezone.now() - timedelta(days=INCIDENT_EVENT_PRUNE_DAYS),
        level__lt=6)
    qset.delete()


def block_ip(job):
    """
    Broadcast job: applies iptables blocks on the local instance.
    Called via jobs.broadcast_execute() so it runs on every runner.

    Expected payload:
        ips: list of IP strings to block
        ttl: seconds before auto-unblock (default 600, 0 = permanent)
    """
    from mojo.apps.incident import firewall

    payload = job.payload or {}
    ips = payload.get("ips", [])
    ttl = payload.get("ttl", 600)

    if not ips:
        job.add_log("block_ip called with no IPs", kind="warning")
        return

    blocked = []
    for ip in ips:
        ip = str(ip).strip()
        if not ip:
            continue
        if firewall.block(ip):
            blocked.append(ip)

    job.add_log(f"Blocked {len(blocked)}/{len(ips)} IPs (ttl={ttl}s): {blocked}")
    # No delayed unblock scheduled here — the sweep_expired_blocks cron
    # handles expiry every minute via GeoLocatedIP.blocked_until


def unblock_ip(job):
    """
    Broadcast job: removes iptables blocks on the local instance.
    Called by sweep_expired_blocks or manually via admin unblock.

    Expected payload:
        ips: list of IP strings to unblock
    """
    from mojo.apps.incident import firewall

    payload = job.payload or {}
    ips = payload.get("ips", [])

    if not ips:
        return

    unblocked = []
    for ip in ips:
        ip = str(ip).strip()
        if not ip:
            continue
        if firewall.unblock(ip):
            unblocked.append(ip)

    job.add_log(f"Unblocked {len(unblocked)}/{len(ips)} IPs: {unblocked}")


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
        "mojo.apps.incident.asyncjobs.unblock_ip",
        {"ips": expired},
    )

    job.add_log(f"Swept {len(expired)} expired blocks: {expired}")


def sync_ipset(job):
    """
    Broadcast job: loads an ipset on the local instance.
    Called via jobs.broadcast_execute() so every instance gets the same set.

    Expected payload:
        name: ipset name (e.g. "country_cn")
        cidrs: list of CIDR strings
    """
    from mojo.apps.incident import firewall

    payload = job.payload or {}
    name = payload.get("name")
    cidrs = payload.get("cidrs", [])

    if not name:
        job.add_log("sync_ipset called with no name", kind="warning")
        return

    ok, loaded = firewall.ipset_load(name, cidrs)
    job.add_log(f"ipset {name}: loaded {loaded} CIDRs, success={ok}")


def remove_ipset(job):
    """
    Broadcast job: removes an ipset from the local instance.

    Expected payload:
        name: ipset name to remove
    """
    from mojo.apps.incident import firewall

    payload = job.payload or {}
    name = payload.get("name")

    if not name:
        return

    firewall.ipset_remove(name)
    job.add_log(f"ipset {name}: removed")


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


def example(job):
    job.add_log("This is an example job")
