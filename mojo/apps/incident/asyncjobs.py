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
        maestro_links__isnull=True,
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


SYNC_FIREWALL_REDIS_PREFIX = "mojo:sync_firewall"
# 2x the hourly interval, so a marker outlives one missed cycle but not two.
SYNC_FIREWALL_MARKER_TTL = 7200
# Long enough for a full reconcile of every enabled set (each set.replace has
# its own 125s broker ceiling) without wedging the next hour if a run dies.
SYNC_FIREWALL_LOCK_TTL = 900


def _firewall_host():
    """Per-HOST identity for the reconcile keys.

    Two engines on one box share ONE kernel firewall, so the marker, the force
    flag and the lock are all scoped to the host rather than the runner id —
    otherwise the second engine would redo the first one's work and, worse,
    the lock would not actually protect the kernel they share. host_channel()
    is the runner id minus its '-engine' suffix and needs no execution
    context, so this also works from a manual invocation.
    """
    from mojo.apps.jobs.job_engine import host_channel
    return host_channel()


def _sync_firewall_keys(host=None):
    """(last_sync, force, lock) Redis keys for one host."""
    host = host or _firewall_host()
    return (f"{SYNC_FIREWALL_REDIS_PREFIX}:last_sync:{host}",
            f"{SYNC_FIREWALL_REDIS_PREFIX}:force:{host}",
            f"{SYNC_FIREWALL_REDIS_PREFIX}:lock:{host}")


def sync_firewall(job):
    """
    Reconcile THIS node's kernel ipsets against DB truth.

    Published two ways, both landing here (item #2716):
      * hourly by cronjobs.sync_firewall as a BROADCAST — one job per runner,
        because each node has its own kernel and only the node running the job
        can repair it;
      * box-direct with {"force": True} by on_engine_start below, so a node
        that just booted recovers immediately instead of waiting up to an hour.

    Skips ipsets unchanged since THIS HOST last synced, so the steady-state
    cost stays low. The marker only advances on a clean run — a node whose
    loads failed must retry next cycle, not record success and suppress
    itself forever.

    1. Load permanently blocked IPs into mojo_blocked (if changed)
    2. Load enabled IPSet records modified since this host's last sync
    """
    import uuid

    from mojo.apps.incident import firewall
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.helpers import dates

    redis_client = get_adapter()
    now = dates.utcnow()
    last_sync_key, force_key, lock_key = _sync_firewall_keys()

    # One reconcile at a time per host. The broker builds its temp set name as
    # "<name>_tmp" with no per-invocation suffix, so two concurrent
    # set.replace calls for one set interleave and can swap in a live set that
    # is missing entries. Skipping is safe because the force flag lives in
    # Redis, not in this job: a forced run that loses the lock leaves the flag
    # set and the next reconcile on this host honors it.
    token = uuid.uuid4().hex
    if not redis_client.set(lock_key, token, nx=True, ex=SYNC_FIREWALL_LOCK_TTL):
        job.add_log("sync_firewall: another reconcile is in flight on this host, skipped")
        return

    try:
        forced = bool((job.payload or {}).get("force")) or bool(
            redis_client.get(force_key))

        # Check when THIS HOST last synced. A forced run ignores it entirely:
        # the marker is in shared Redis and therefore survives the very reboot
        # the startup hook exists to recover from.
        last_sync = None
        if not forced:
            last_sync_raw = redis_client.get(last_sync_key)
            if last_sync_raw:
                try:
                    last_sync = dates.parse(last_sync_raw)
                except Exception:
                    pass

        # A load counts as failed only when there was something to load:
        # ipset_load also returns (False, 0) for an empty CIDR list, which is
        # its deliberate refusal to wipe a live set with an empty swap.
        failures = 0

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
            if permanent_ips and not ok:
                failures += 1
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
            elif cidrs:
                failures += 1

        if skipped:
            job.add_log(f"sync_firewall: skipped {skipped} unchanged IPSets")

        if failures:
            # Do NOT advance the marker or clear the force flag: this host is
            # not in the state the marker would claim, and the next reconcile
            # must reload rather than skip.
            job.add_log(
                f"sync_firewall: {failures} load(s) failed — marker not advanced; "
                "the next reconcile will retry")
        else:
            redis_client.set(last_sync_key, now.isoformat(),
                             ex=SYNC_FIREWALL_MARKER_TTL)
            if forced:
                redis_client.delete(force_key)
    finally:
        # Only release a lock we still hold — a lock that expired mid-run
        # belongs to whoever took it next.
        if redis_client.get(lock_key) == token:
            redis_client.delete(lock_key)


def on_engine_start(engine):
    """Reconcile THIS node's firewall because its engine started (item #2716).

    Publishes rather than reconciling inline: every firewall write goes through
    the root-owned broker, which refuses outside a JobEngine execution context,
    and a startup hook has none. The box-direct channel — the runner id, which
    carries the '-engine' suffix precisely so the channel named after it is
    publishable — puts the work on this engine's own worker pool with a real
    Job row, its logs, and a real execution context.

    The Redis force flag is set BEFORE publishing, and is what makes recovery
    converge: the host's last-sync marker survives the reboot this hook exists
    to recover from, and the flag outlives a job that never got to run.
    """
    from mojo.apps import jobs
    from mojo.apps.jobs.adapters import get_adapter

    if engine.runner_id not in (engine.channels or []):
        logit.warning(
            "incident: skipping startup firewall recovery — this engine does "
            f"not consume its box-direct channel {engine.runner_id} "
            "(JOBS_HOSTNAME_CHANNEL is False)")
        return "skipped:no box-direct channel"

    _, force_key, _ = _sync_firewall_keys()
    get_adapter().set(force_key, "1", ex=SYNC_FIREWALL_MARKER_TTL)
    jobs.publish(
        func="mojo.apps.incident.asyncjobs.sync_firewall",
        payload={"force": True},
        channel=engine.runner_id)
    return f"queued:sync_firewall force={engine.runner_id}"


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


# Daily decay pass. Nothing else ever recomputes threat_level downward:
# update_threat_from_incident() and block() only ratchet up, so before this a
# single level-8 event stamped an address 'medium' forever — including every
# user behind a shared egress. Re-running check_threats() lets a recomputed
# lower level replace the stored one.
#
# mojo-provider records are excluded on purpose: their never-downgrade rule is
# the federation contract with the upstream, not a local scoring decision.
RECHECK_THREATS_MAX = settings.get_static("GEOLOCATION_RECHECK_THREATS_MAX", 500)


def recheck_active_threats(job):
    """Recompute threat_level for recently-active, non-clean IPs."""
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.helpers.geoip import threat_intel
    from mojo.helpers import dates

    window_hours = settings.get(
        "GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS",
        threat_intel.INTERNAL_THREAT_WINDOW_HOURS, kind="int")
    limit = settings.get("GEOLOCATION_RECHECK_THREATS_MAX",
                         RECHECK_THREATS_MAX, kind="int")

    # Most recently active first — if the bound clips the tail it should clip
    # the quietest addresses, not the busiest.
    cutoff = dates.utcnow() - timedelta(hours=window_hours)
    candidates = list(
        GeoLocatedIP.objects.filter(
            last_seen__gte=cutoff,
            threat_level__isnull=False,
        ).exclude(
            provider="mojo",
        ).order_by("-last_seen")[:limit]
    )

    if not candidates:
        job.add_log("recheck_active_threats: nothing active to recheck")
        return

    lowered = 0
    failed = 0
    order = GeoLocatedIP.THREAT_LEVEL_ORDER
    for geo in candidates:
        before = geo.threat_level
        try:
            # skip_external: this pass is about decaying LOCAL evidence, and a
            # daily outbound lookup per row is not affordable. A recorded
            # blocklist hit is carried forward by check_threats().
            geo.check_threats(skip_external=True)
        except Exception:
            failed += 1
            logit.exception(f"recheck_active_threats failed for {geo.ip_address}")
            continue
        before_idx = order.index(before) if before in order else 0
        after_idx = order.index(geo.threat_level) if geo.threat_level in order else 0
        if after_idx < before_idx:
            lowered += 1

    job.add_log(
        f"recheck_active_threats: rechecked {len(candidates)} IPs, "
        f"{lowered} decayed, {failed} errored")


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


def refresh_threat_lists(job):
    """
    Cron job (every 6h): refreshes the cache-only threat lists (tor_exits,
    blocklist_de) consumed by mojo.helpers.geoip detection.

    refresh_from_source() ONLY — never sync(). These rows are is_enabled=False
    by design and must never be pushed to the kernel firewall.
    """
    from mojo.apps.incident.models import IPSet

    for ipset in IPSet.ensure_threat_caches():
        if ipset.refresh_from_source():
            job.add_log(f"Refreshed threat cache {ipset.name} ({ipset.cidr_count} entries)")
        else:
            job.add_log(f"Threat cache {ipset.name} refresh failed: {ipset.sync_error}")


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

    if not settings.get(
            "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED", False, kind="bool"):
        return
    watermark = settings.get("LLM_AUTONOMOUS_INCIDENT_TRIAGE_ACTIVATED_AT", None)
    if not watermark or not settings.get("LLM_HANDLER_API_KEY"):
        return

    BATCH_SIZE = 20

    # Incidents still "new" with no LLM assessment recorded yet
    incidents = list(
        Incident.objects
        .filter(status="new", created__gte=watermark)
        .exclude(metadata__has_key="llm_assessment")
        .order_by("created", "pk")[:BATCH_SIZE]
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


def run_concentration_check(now=None):
    """Detect traffic concentration by a single authenticated identity (DM-042).

    Reads the traffic:top:{bucket} zsets and traffic:total:{bucket} counters
    that check_api_throttle maintains (5-minute buckets, incremented directly
    on every authenticated request). Alerts when an
    identity is over TRAFFIC_CONCENTRATION_RPM for
    TRAFFIC_CONCENTRATION_SUSTAIN_WINDOWS consecutive complete buckets, or
    holds more than TRAFFIC_CONCENTRATION_SHARE of a bucket's total when the
    total is at least TRAFFIC_CONCENTRATION_MIN_TOTAL (the floor keeps a dev
    box where one user IS the traffic from paging).

    One incident event per identity per hour (SET NX dedup) — the prebuilt
    "traffic:concentration" ruleset bundles them per identity and notifies
    manage_security holders. Returns the list of alerts emitted.
    """
    import time as _time
    from mojo.helpers.redis import get_connection
    from mojo.apps import incident
    from mojo.apps.incident.models import RuleSet
    from mojo.decorators.limits import TRAFFIC_BUCKET_SECONDS

    try:
        if not RuleSet.objects.filter(category="traffic:concentration").exists():
            RuleSet.ensure_traffic_rules()
    except Exception:
        pass

    rpm_threshold = settings.get("TRAFFIC_CONCENTRATION_RPM", 120, kind="int")
    sustain = max(1, settings.get("TRAFFIC_CONCENTRATION_SUSTAIN_WINDOWS", 2, kind="int"))
    share_threshold = settings.get("TRAFFIC_CONCENTRATION_SHARE", 0.20, kind="float")
    min_total = settings.get("TRAFFIC_CONCENTRATION_MIN_TOTAL", 1000, kind="int")

    now = int(now or _time.time())
    bucket_minutes = TRAFFIC_BUCKET_SECONDS / 60.0
    current = now // TRAFFIC_BUCKET_SECONDS * TRAFFIC_BUCKET_SECONDS
    # Most recent COMPLETE bucket first, then the ones before it.
    buckets = [current - TRAFFIC_BUCKET_SECONDS * (i + 1) for i in range(sustain)]
    newest = buckets[0]

    r = get_connection()
    top = r.zrevrange(f"traffic:top:{newest}", 0, 19, withscores=True)
    if not top:
        return []
    total_raw = r.get(f"traffic:total:{newest}")
    total = int(total_raw) if total_raw else 0
    # Source attribution is informational and cardinality-capped separately;
    # it must never displace authenticated identities from this top-20 scan.
    top_ips = r.zrevrange(f"traffic:top_ip:{newest}", 0, 2)

    alerts = []
    for member, score in top:
        rpm = score / bucket_minutes
        share = (score / total) if total else 0.0

        sustained = rpm >= rpm_threshold
        if sustained:
            for bucket in buckets[1:]:
                prev = r.zscore(f"traffic:top:{bucket}", member)
                if not prev or (prev / bucket_minutes) < rpm_threshold:
                    sustained = False
                    break
        share_hit = total >= min_total and share >= share_threshold
        if not sustained and not share_hit:
            continue

        # At most one alert per identity per hour — the abuser is already
        # known; re-paging every 5 minutes is noise.
        if not r.set(f"traffic:alerted:{member}", 1, nx=True, ex=3600):
            continue

        kind, _, pk = member.partition(":")
        reasons = []
        if sustained:
            reasons.append(f"{rpm:.0f} req/min sustained over {sustain * bucket_minutes:.0f} min")
        if share_hit:
            reasons.append(f"{share:.0%} of {total} requests in {bucket_minutes:.0f} min")
        details = f"Traffic concentration: {member} — " + "; ".join(reasons)
        event_kwargs = {
            "model_name": f"traffic:{kind}",
            "model_id": int(pk) if pk.isdigit() else None,
            "identity": member,
            "rpm": round(rpm, 1),
            "share": round(share, 4),
            "bucket_total": total,
            "top_ips": top_ips,
        }
        if kind == "user" and pk.isdigit():
            event_kwargs["uid"] = int(pk)
        try:
            incident.report_event(
                details,
                title=f"Traffic concentration: {member}",
                category="traffic:concentration",
                scope="api",
                level=6,
                **event_kwargs,
            )
        except Exception:
            logit.exception(f"traffic concentration: failed to report {member}")
            continue
        alerts.append({"identity": member, "rpm": rpm, "share": share, "total": total})
    return alerts


def check_traffic_concentration(job):
    alerts = run_concentration_check()
    job.add_log(f"Traffic concentration check: {len(alerts)} alert(s)")
    for alert in alerts:
        job.add_log(
            f"  {alert['identity']}: {alert['rpm']:.0f} rpm, "
            f"{alert['share']:.0%} of {alert['total']}"
        )


def _maestro_fail(job, err, what):
    """Shared failure policy for maestro sync jobs (DM-040, fail-open).

    Retriable errors re-raise so the jobs engine retries (max_retries=3 with
    backoff); terminal errors (4xx — revoked key, validation) drop with a
    local log. Nothing here ever propagates back to a ticket save.
    """
    if getattr(err, "retriable", False):
        logit.warning("maestro sync failed (attempt %s/%s) %s: %s",
                      job.attempt, job.max_retries, what, err)
        raise err
    logit.warning("maestro sync dropped %s: %s", what, err)
    job.add_log(f"maestro sync dropped {what}: {err}", kind="error")


def maestro_push_source(job):
    """Create/update the Maestro item for a Ticket or Incident."""
    from mojo.apps.incident.models import Incident, Ticket
    from mojo.apps.incident.services import maestro_sync

    source_kind = job.payload.get("source_kind")
    model = {"ticket": Ticket, "incident": Incident}.get(source_kind)
    source = model.objects.filter(pk=job.payload.get("source_id")).first() if model else None
    if source is None:
        job.add_log("maestro_push_source: source missing — skipped")
        return
    try:
        maestro_sync.push_source(source, job.payload.get("board_id"))
    except maestro_sync.MaestroRequestError as err:
        _maestro_fail(job, err, f"pushing {source_kind} {source.pk}")
    except Exception as err:
        # Configuration/validation failures are terminal for queued work.
        _maestro_fail(job, err, f"pushing {source_kind} {source.pk}")


def maestro_sync_change(job):
    """Push changed fields of a linked local source."""
    from mojo.apps.incident.models import MaestroItemLink
    from mojo.apps.incident.services import maestro_sync

    link = MaestroItemLink.objects.filter(pk=job.payload.get("link_id")).select_related(
        "ticket", "incident").first()
    if link is None:
        job.add_log("maestro_sync_change: link missing — skipped")
        return
    try:
        maestro_sync.sync_change(link, job.payload.get("changed") or [])
    except maestro_sync.MaestroRequestError as err:
        _maestro_fail(job, err, f"syncing {link.source_kind} {link.source_id}")
    except Exception as err:
        _maestro_fail(job, err, f"syncing {link.source_kind} {link.source_id}")


def maestro_push_note(job):
    """Mirror a TicketNote or IncidentHistory as a Maestro comment."""
    from mojo.apps.incident.models import IncidentHistory, MaestroItemLink, TicketNote
    from mojo.apps.incident.services import maestro_sync

    link = MaestroItemLink.objects.filter(pk=job.payload.get("link_id")).first()
    note_model = {
        "ticket": TicketNote,
        "incident": IncidentHistory,
    }.get(job.payload.get("note_kind"))
    note = note_model.objects.filter(pk=job.payload.get("note_id")).first() if note_model else None
    if link is None or note is None:
        job.add_log("maestro_push_note: link or note missing — skipped")
        return
    try:
        maestro_sync.push_note(link, note)
    except maestro_sync.MaestroRequestError as err:
        _maestro_fail(job, err, f"pushing note {note.pk} to item {link.remote_item_id}")
    except Exception as err:
        _maestro_fail(job, err, f"pushing note {note.pk} to item {link.remote_item_id}")


def example(job):
    job.add_log("This is an example job")
