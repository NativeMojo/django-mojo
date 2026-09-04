from mojo.apps.incident.models import Event
from django.utils import timezone
from datetime import timedelta
from mojo.helpers.settings import settings
from mojo.helpers import logit

# Default: check once an hour at minute 0 (can be overridden in settings)
INCIDENT_EVENT_PRUNE_DAYS = settings.get_static("INCIDENT_EVENT_PRUNE_DAYS", 30)
INCIDENT_PRUNE_DAYS = settings.get_static("INCIDENT_PRUNE_DAYS", 90)


def _raw_redis():
    from mojo.apps.jobs.adapters import get_adapter
    adapter = get_adapter()
    return adapter.get_client() if hasattr(adapter, "get_client") else adapter


def _redis_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "strict")
    return value


def _checked_host_lock(redis_client):
    import uuid
    unused_last, unused_force, key = _sync_firewall_keys()
    token = uuid.uuid4().hex
    if not redis_client.set(
            key, token, nx=True, ex=SYNC_FIREWALL_LOCK_TTL):
        return key, None
    return key, token


def _release_checked_host_lock(redis_client, key, token):
    if token is not None:
        redis_client.eval(_LOCK_RELEASE_LUA, 1, key, token)


def _valid_checked_generation(data, redis_client, targets):
    """Validate the dispatch lease and each monotonic object fence."""
    from mojo.apps.incident.services import firewall_truth
    lease_token = data.get("lease_token")
    if (not isinstance(lease_token, str) or len(lease_token) != 32 or
            any(char not in "0123456789abcdef" for char in lease_token) or
            _redis_text(redis_client.get(
                firewall_truth.DESIRED_STATE_LOCK)) != lease_token):
        raise firewall_truth.FirewallTruthError(
            "generation_superseded", "desired-state lease is no longer current")
    expected = {}
    for field, kind, identity in targets:
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise firewall_truth.FirewallTruthError(
                "invalid_fence", "firewall fence is invalid")
        expected[(kind, identity)] = value
    if firewall_truth.read_fences(redis_client, expected) != expected:
        raise firewall_truth.FirewallTruthError(
            "generation_superseded", "firewall fence is stale")
    return expected


def _semantic(kind, desired, broker_result):
    """Project a broker response into the bounded checked wire contract."""
    from mojo.apps.incident.services.firewall_truth import (
        FIREWALL_SEMANTIC_SCHEMA, FIREWALL_SEMANTIC_VERSION)

    observed = broker_result.get("observed") if isinstance(broker_result, dict) else None
    if kind == "ip" and isinstance(observed, dict):
        actual = {"ip": observed.get("ip"),
                  "present": observed.get("present") is True}
    elif kind == "set" and isinstance(observed, dict):
        actual = {
            "name": observed.get("name"),
            "present": observed.get("present") is True,
            "count": observed.get("count"),
            "digest": observed.get("digest"),
        }
    else:
        actual = None
    result = {
        "schema": FIREWALL_SEMANTIC_SCHEMA,
        "version": FIREWALL_SEMANTIC_VERSION,
        "kind": kind,
        "desired": desired,
        "observed": actual,
        "ok": bool(broker_result and broker_result.get("ok") and actual == desired),
    }
    if not result["ok"]:
        error = broker_result.get("error") if isinstance(broker_result, dict) else None
        code = error.get("code") if isinstance(error, dict) else "semantic_mismatch"
        result["error"] = str(code)[:64]
    return result


def broadcast_reconcile_firewall_ip(data):
    """Checked handler: normalize one IPv4 source then return exact truth."""
    from mojo.apps.incident import firewall
    from mojo.apps.incident.services import firewall_truth

    if not isinstance(data, dict) or set(data) != {
            "ip", "present", "fence", "fingerprint", "lease_token"} or \
            not isinstance(data.get("present"), bool):
        return _semantic("ip", {}, {"ok": False,
                                    "error": {"code": "invalid_request"}})
    try:
        ip = firewall_truth.canonical_ipv4_address(data["ip"])
    except firewall_truth.FirewallTruthError as err:
        return _semantic("ip", {}, {"ok": False,
                                    "error": {"code": err.code}})
    desired = {"ip": ip, "present": data["present"]}
    if data.get("fingerprint") != firewall_truth.state_fingerprint(desired):
        return _semantic("ip", desired, {
            "ok": False, "error": {"code": "fingerprint_mismatch"}})
    redis_client = _raw_redis()
    lock_key, lock_token = _checked_host_lock(redis_client)
    if lock_token is None:
        return _semantic("ip", desired, {
            "ok": False, "error": {"code": "host_busy"}})
    try:
        targets = [("fence", "ip", ip)]
        _valid_checked_generation(data, redis_client, targets)
        broker_result = firewall.normalize_ip(ip, data["present"])
        _valid_checked_generation(data, redis_client, targets)
        result = _semantic("ip", desired, broker_result)
        if result["ok"]:
            firewall_truth.record_host_observation(
                "ip", ip, data["fence"], data["fingerprint"], desired)
        return result
    except firewall_truth.FirewallTruthError as err:
        return _semantic("ip", desired, {
            "ok": False, "error": {"code": err.code}})
    finally:
        _release_checked_host_lock(redis_client, lock_key, lock_token)


def broadcast_reconcile_firewall_set(data):
    """Checked handler: normalize one hash:net set and exact rule counts."""
    from mojo.apps.incident import firewall
    from mojo.apps.incident.services import firewall_truth

    if not isinstance(data, dict) or set(data) != {
            "name", "cidrs", "present", "fence", "fingerprint",
            "lease_token"} \
            or not isinstance(data.get("present"), bool):
        return _semantic("set", {}, {"ok": False,
                                     "error": {"code": "invalid_request"}})
    try:
        name = firewall_truth.canonical_set_name(data["name"])
        if name == firewall_truth.permanent_set_name():
            raise firewall_truth.FirewallTruthError(
                "reserved_set_name", "configured permanent set name is reserved")
        cidrs = firewall_truth.canonical_ipv4_networks(data["cidrs"])
    except firewall_truth.FirewallTruthError as err:
        return _semantic("set", {}, {"ok": False,
                                     "error": {"code": err.code}})
    desired = {
        "name": name, "present": data["present"],
        "count": len(cidrs) if data["present"] else 0,
        "digest": firewall_truth.network_digest(cidrs if data["present"] else []),
    }
    redis_client = _raw_redis()
    lock_key, lock_token = _checked_host_lock(redis_client)
    if lock_token is None:
        return _semantic("set", desired, {
            "ok": False, "error": {"code": "host_busy"}})
    try:
        targets = [("fence", "set", name)]
        _valid_checked_generation(data, redis_client, targets)
        snapshot = firewall_truth.ipset_snapshot(name)
        if (snapshot["fingerprint"] != data.get("fingerprint") or
                snapshot["desired"] != desired):
            raise firewall_truth.FirewallTruthError(
                "generation_superseded", "IPSet desired state is stale")
        broker_result = firewall.normalize_ipset(name, cidrs, data["present"])
        _valid_checked_generation(data, redis_client, targets)
        current = firewall_truth.ipset_snapshot(name)
        if (current["fingerprint"] != data["fingerprint"] or
                current["desired"] != desired):
            raise firewall_truth.FirewallTruthError(
                "generation_superseded", "IPSet desired state changed")
        result = _semantic("set", desired, broker_result)
        if result["ok"]:
            firewall_truth.record_host_observation(
                "set", name, data["fence"], data["fingerprint"], desired)
        return result
    except firewall_truth.FirewallTruthError as err:
        if err.code == "generation_superseded":
            firewall_truth.mark_superseded_pending("set", name)
        return _semantic("set", desired, {
            "ok": False, "error": {"code": err.code}})
    finally:
        _release_checked_host_lock(redis_client, lock_key, lock_token)


def broadcast_reconcile_geolocated_ip(data):
    """Normalize direct-IP and permanent-set truth as one checked result."""
    from mojo.apps.incident import firewall
    from mojo.apps.incident.services import firewall_truth
    if not isinstance(data, dict) or set(data) != {
            "ip", "permanent_set_name", "permanent_ips",
            "temporary_present", "ip_fence", "aggregate_fence",
            "fingerprint", "aggregate_fingerprint", "lease_token"}:
        return _semantic("geolocated_ip", {}, {
            "ok": False, "error": {"code": "invalid_payload"}})
    try:
        ip = firewall_truth.canonical_ipv4_address(data["ip"])
        set_name = firewall_truth.canonical_set_name(
            data["permanent_set_name"])
        if set_name != firewall_truth.permanent_set_name():
            raise firewall_truth.FirewallTruthError(
                "reserved_set_name", "permanent set name does not match configuration")
        if len(set_name) + 4 > 31:
            raise firewall_truth.FirewallTruthError(
                "invalid_set_name", "permanent set name is too long")
        permanent = firewall_truth.canonical_ipv4_networks(
            data["permanent_ips"])
    except firewall_truth.FirewallTruthError as err:
        return _semantic("geolocated_ip", {}, {
            "ok": False, "error": {"code": err.code}})
    if not isinstance(data["temporary_present"], bool):
        return _semantic("geolocated_ip", {}, {
            "ok": False, "error": {"code": "invalid_payload"}})
    ip_desired = {"ip": ip, "present": data["temporary_present"]}
    set_desired = {
        "name": set_name, "present": True,
        "count": len(permanent),
        "digest": firewall_truth.network_digest(permanent),
    }
    desired = {"ip": ip_desired, "permanent": set_desired}
    redis_client = _raw_redis()
    lock_key, lock_token = _checked_host_lock(redis_client)
    if lock_token is None:
        return _semantic("geolocated_ip", desired, {
            "ok": False, "error": {"code": "host_busy"}})
    try:
        targets = [
            ("ip_fence", "ip", ip),
            ("aggregate_fence", "permanent", set_name),
        ]
        _valid_checked_generation(data, redis_client, targets)
        snapshot = firewall_truth.geolocated_snapshot(ip)
        if (snapshot["fingerprint"] != data.get("fingerprint") or
                snapshot["permanent"]["fingerprint"] !=
                data.get("aggregate_fingerprint") or
                snapshot["desired"] != desired):
            raise firewall_truth.FirewallTruthError(
                "generation_superseded", "GeoLocatedIP desired state is stale")
        broker_result = firewall.normalize_geolocated_ip(
            ip, set_name, permanent, data["temporary_present"])
        _valid_checked_generation(data, redis_client, targets)
        current = firewall_truth.geolocated_snapshot(ip)
        if (current["fingerprint"] != data["fingerprint"] or
                current["permanent"]["fingerprint"] !=
                data["aggregate_fingerprint"] or
                current["desired"] != desired):
            raise firewall_truth.FirewallTruthError(
                "generation_superseded", "GeoLocatedIP desired state changed")
        combined = (broker_result.get("observed")
                    if isinstance(broker_result, dict) else None)
        direct_observed = (combined.get("ip")
                           if isinstance(combined, dict) else None)
        set_observed = (combined.get("permanent")
                        if isinstance(combined, dict) else None)
        observed = {
            "ip": ({"ip": direct_observed.get("ip"),
                    "present": direct_observed.get("present") is True}
                   if isinstance(direct_observed, dict) else None),
            "permanent": ({
                "name": set_observed.get("name"),
                "present": set_observed.get("present") is True,
                "count": set_observed.get("count"),
                "digest": set_observed.get("digest"),
            } if isinstance(set_observed, dict) else None),
        }
        ok = (isinstance(broker_result, dict) and
              broker_result.get("ok") is True and observed == desired)
        result = {
            "schema": firewall_truth.FIREWALL_SEMANTIC_SCHEMA,
            "version": firewall_truth.FIREWALL_SEMANTIC_VERSION,
            "kind": "geolocated_ip", "ok": ok,
            "desired": desired, "observed": observed,
            "error": None if ok else {"code": "state_mismatch"},
        }
        if ok:
            firewall_truth.record_host_observation(
                "geo", ip, data["ip_fence"], data["fingerprint"], desired)
            firewall_truth.record_host_observation(
                "permanent", set_name, data["aggregate_fence"],
                data["aggregate_fingerprint"], set_desired)
        return result
    except firewall_truth.FirewallTruthError as err:
        if err.code == "generation_superseded":
            firewall_truth.mark_superseded_pending("geo", ip)
        return _semantic("geolocated_ip", desired, {
            "ok": False, "error": {"code": err.code}})
    finally:
        _release_checked_host_lock(redis_client, lock_key, lock_token)


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
    from mojo.apps.incident.services import firewall_truth

    ip = data.get("ip")
    if not ip:
        return

    set_name = firewall_truth.permanent_set_name()
    if firewall.ipset_add(set_name, ip):
        logit.info("broadcast_ipset_add_blocked: added %s to %s", ip, set_name)


def broadcast_ipset_del_blocked(data):
    """Broadcast handler — removes a single IP from the permanent block ipset.

    Expected data keys:
        ip: IP address string to remove
    """
    from mojo.apps.incident import firewall
    from mojo.apps.incident.services import firewall_truth

    ip = data.get("ip")
    if not ip:
        return

    set_name = firewall_truth.permanent_set_name()
    if firewall.ipset_del(set_name, ip):
        logit.info("broadcast_ipset_del_blocked: removed %s from %s", ip, set_name)


SYNC_FIREWALL_REDIS_PREFIX = "mojo:sync_firewall"
# 2x the hourly interval, so a marker outlives one missed cycle but not two.
SYNC_FIREWALL_MARKER_TTL = 7200
# Long enough for a full reconcile of every enabled set (each set.replace has
# its own 125s broker ceiling) without wedging the next hour if a run dies.
SYNC_FIREWALL_LOCK_TTL = 900
SYNC_FIREWALL_MAX_OBJECTS = 10000
_LOCK_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_LOCK_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_DELETE_VALUE_LUA = _LOCK_RELEASE_LUA


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
    """Write only this host's bounded observations for exact desired fences."""
    import uuid
    from django.db.models import Q
    from mojo.apps import jobs
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident import firewall
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import firewall_truth
    from mojo.helpers import dates

    redis_client = _raw_redis()
    last_sync_key, force_key, lock_key = _sync_firewall_keys()
    lease = None
    token = None
    try:
        # All snapshot reads, local mutations and observation publication share
        # one global desired-state generation. Checked handlers prove the same
        # lease token; the hourly local path owns it directly.
        lease = firewall_truth.acquire_desired_state(SYNC_FIREWALL_LOCK_TTL)
        token = uuid.uuid4().hex
        if not redis_client.set(
                lock_key, token, nx=True, ex=SYNC_FIREWALL_LOCK_TTL):
            job.add_log(
                "sync_firewall: another reconcile is in flight on this host, skipped")
            return False
        force_value = redis_client.get(force_key)
        ipsets = list(IPSet.objects.order_by("pk")[
            :SYNC_FIREWALL_MAX_OBJECTS + 1])
        geos = list(GeoLocatedIP.objects.filter(
            Q(is_blocked=True) | Q(firewall_pending=True) |
            Q(is_whitelisted=True)).order_by("pk")[
                :SYNC_FIREWALL_MAX_OBJECTS + 1])
        if (len(ipsets) > SYNC_FIREWALL_MAX_OBJECTS or
                len(geos) > SYNC_FIREWALL_MAX_OBJECTS):
            job.add_log("sync_firewall: desired object bound exceeded; no writes")
            return False

        failures = 0
        quarantined = 0

        def renew():
            return bool(redis_client.eval(
                _LOCK_RENEW_LUA, 1, lock_key, token,
                str(SYNC_FIREWALL_LOCK_TTL)) and
                firewall_truth.renew_desired_state(lease))

        try:
            permanent = firewall_truth.permanent_snapshot()
            aggregate_name = permanent["name"]
            aggregate_target = ("permanent", aggregate_name)
            aggregate_fence = firewall_truth.read_fences(
                redis_client, [aggregate_target])[aggregate_target]
            if aggregate_fence == 0:
                aggregate_fence = firewall_truth.advance_fences(
                    lease, [aggregate_target])[aggregate_target]
        except firewall_truth.FirewallTruthError as err:
            job.add_log(
                f"sync_firewall: permanent desired state refused ({err.code})")
            return False

        set_plans = []
        for row in ipsets:
            try:
                snapshot = firewall_truth.ipset_snapshot(row.name)
                target = ("set", snapshot["desired"]["name"])
                fence = firewall_truth.read_fences(
                    redis_client, [target])[target]
                if fence == 0:
                    fence = firewall_truth.advance_fences(
                        lease, [target])[target]
                set_plans.append((snapshot, fence))
            except firewall_truth.FirewallTruthError as err:
                quarantined += 1
                job.add_log(
                    f"sync_firewall: IPSet {row.pk} quarantined ({err.code})")

        geo_plans = []
        for row in geos:
            try:
                snapshot = firewall_truth.geolocated_snapshot(
                    row.ip_address, permanent=permanent)
                target = ("ip", snapshot["canonical"])
                fence = firewall_truth.read_fences(
                    redis_client, [target])[target]
                if fence == 0:
                    fence = firewall_truth.advance_fences(
                        lease, [target])[target]
                geo_plans.append((snapshot, fence))
            except firewall_truth.FirewallTruthError as err:
                quarantined += 1
                job.add_log(
                    f"sync_firewall: GeoLocatedIP {row.pk} quarantined ({err.code})")

        if not renew():
            job.add_log("sync_firewall: lock ownership lost before writes")
            return False
        set_desired = permanent["desired"] if "desired" in permanent else {
            "name": aggregate_name, "present": True,
            "count": len(permanent["cidrs"]),
            "digest": firewall_truth.network_digest(permanent["cidrs"]),
        }
        result = firewall.normalize_ipset(
            aggregate_name, permanent["cidrs"], True)
        semantic = _semantic("set", set_desired, result)
        current_permanent = firewall_truth.permanent_snapshot()
        current_aggregate_fence = firewall_truth.read_fences(
            redis_client, [aggregate_target])[aggregate_target]
        aggregate_ok = bool(
            semantic["ok"] and
            current_permanent["fingerprint"] == permanent["fingerprint"] and
            current_aggregate_fence == aggregate_fence and
            firewall_truth.desired_state_is_current(lease))
        if aggregate_ok:
            firewall_truth.record_host_observation(
                "permanent", aggregate_name, aggregate_fence,
                permanent["fingerprint"], set_desired)
        else:
            failures += 1

        for snapshot, fence in set_plans:
            if not renew():
                failures += 1
                break
            desired = snapshot["desired"]
            result = firewall.normalize_ipset(
                desired["name"], snapshot["cidrs"], desired["present"])
            current = firewall_truth.ipset_snapshot(desired["name"])
            target = ("set", desired["name"])
            current_fence = firewall_truth.read_fences(
                redis_client, [target])[target]
            if (_semantic("set", desired, result)["ok"] and
                    current["fingerprint"] == snapshot["fingerprint"] and
                    current_fence == fence and
                    firewall_truth.desired_state_is_current(lease)):
                firewall_truth.record_host_observation(
                    "set", desired["name"], fence,
                    snapshot["fingerprint"], desired)
            else:
                failures += 1

        for snapshot, fence in geo_plans:
            if not renew():
                failures += 1
                break
            desired = snapshot["desired"]
            ip_desired = desired["ip"]
            result = firewall.normalize_ip(
                ip_desired["ip"], ip_desired["present"])
            current = firewall_truth.geolocated_snapshot(
                ip_desired["ip"], permanent=permanent)
            target = ("ip", ip_desired["ip"])
            current_fence = firewall_truth.read_fences(
                redis_client, [target])[target]
            if (_semantic("ip", ip_desired, result)["ok"] and aggregate_ok and
                    current["fingerprint"] == snapshot["fingerprint"] and
                    current_fence == fence and
                    firewall_truth.desired_state_is_current(lease)):
                firewall_truth.record_host_observation(
                    "geo", ip_desired["ip"], fence,
                    snapshot["fingerprint"], desired)
            else:
                failures += 1

        # Publish even after an object-local failure: the fleet aggregator can
        # finalize fully observed siblings while keeping the failed object
        # pending. Only the whole-host success marker remains all-or-nothing.
        jobs.publish(
            func="mojo.apps.incident.asyncjobs.aggregate_firewall_truth",
            payload={}, channel="default")
        if failures:
            job.add_log(
                f"sync_firewall: {failures} object(s) unverified; marker not advanced")
            return False
        redis_client.set(last_sync_key, dates.utcnow().isoformat(),
                         ex=SYNC_FIREWALL_MARKER_TTL)
        if force_value:
            redis_client.eval(_DELETE_VALUE_LUA, 1, force_key, force_value)
        job.add_log(
            f"sync_firewall: observed {len(set_plans)} set tombstone(s) and "
            f"{len(geo_plans)} IP desired state(s) on this host; "
            f"quarantined={quarantined}")
        return True
    except firewall_truth.FirewallTruthError as err:
        job.add_log(f"sync_firewall: refused ({err.code})")
        return False
    finally:
        try:
            if token is not None:
                redis_client.eval(_LOCK_RELEASE_LUA, 1, lock_key, token)
        except Exception:
            logit.exception("sync_firewall: failed to release owned host lock")
        firewall_truth.release_desired_state(lease)


def aggregate_firewall_truth(job):
    """Finalize shared truth only from one exact current compatible roster."""
    from django.db.models import Q
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import firewall_truth
    from mojo.helpers import dates

    lease = None
    try:
        lease = firewall_truth.acquire_desired_state(180)
        hosts = firewall_truth.exact_compatible_hosts()
        redis_client = lease.redis
        permanent = firewall_truth.permanent_snapshot()
        aggregate_target = ("permanent", permanent["name"])
        aggregate_fence = firewall_truth.read_fences(
            redis_client, [aggregate_target])[aggregate_target]
        permanent_desired = {
            "name": permanent["name"], "present": True,
            "count": len(permanent["cidrs"]),
            "digest": firewall_truth.network_digest(permanent["cidrs"]),
        }
        aggregate = firewall_truth.aggregate_observations(
            "permanent", permanent["name"], aggregate_fence,
            permanent["fingerprint"], permanent_desired, hosts=hosts)

        ipsets = list(IPSet.objects.order_by("pk")[
            :SYNC_FIREWALL_MAX_OBJECTS + 1])
        geos = list(GeoLocatedIP.objects.filter(
            Q(is_blocked=True) | Q(firewall_pending=True) |
            Q(is_whitelisted=True)).order_by("pk")[
                :SYNC_FIREWALL_MAX_OBJECTS + 1])
        if (len(ipsets) > SYNC_FIREWALL_MAX_OBJECTS or
                len(geos) > SYNC_FIREWALL_MAX_OBJECTS):
            job.add_log("aggregate_firewall_truth: desired object bound exceeded")
            return False

        failures = 0
        ipset_updates = []
        for row in ipsets:
            try:
                snapshot = firewall_truth.ipset_snapshot(row.name)
                target = ("set", snapshot["desired"]["name"])
                fence = firewall_truth.read_fences(
                    redis_client, [target])[target]
                result = firewall_truth.aggregate_observations(
                    "set", row.name, fence, snapshot["fingerprint"],
                    snapshot["desired"], hosts=hosts)
                if result.get("ok") is True:
                    values = {"last_synced": dates.utcnow(), "sync_error": None}
                else:
                    code, message = firewall_truth.bounded_error(result)
                    values = {"last_synced": None,
                              "sync_error": f"{code}: {message}"[:512]}
                    failures += 1
                ipset_updates.append((row, values))
            except firewall_truth.FirewallTruthError as err:
                failures += 1
                ipset_updates.append((row, {
                    "last_synced": None,
                    "sync_error": f"quarantined: {err.code}"[:512],
                }))

        aggregate_ok = aggregate.get("ok") is True
        geo_updates = []
        for row in geos:
            try:
                snapshot = firewall_truth.geolocated_snapshot(
                    row.ip_address, permanent=permanent)
                target = ("ip", snapshot["canonical"])
                fence = firewall_truth.read_fences(
                    redis_client, [target])[target]
                result = firewall_truth.aggregate_observations(
                    "geo", snapshot["canonical"], fence,
                    snapshot["fingerprint"], snapshot["desired"], hosts=hosts)
                verified = aggregate_ok and result.get("ok") is True
                if verified:
                    values = {
                        "firewall_pending": False, "firewall_sync_error": "",
                        "firewall_observed_at": dates.utcnow(),
                    }
                else:
                    source = aggregate if not aggregate_ok else result
                    code, message = firewall_truth.bounded_error(source)
                    values = {
                        "firewall_pending": True,
                        "firewall_sync_error": f"{code}: {message}"[:512],
                    }
                    failures += 1
                geo_updates.append((row, values))
            except firewall_truth.FirewallTruthError as err:
                failures += 1
                geo_updates.append((row, {
                    "firewall_pending": True,
                    "firewall_sync_error": f"quarantined: {err.code}"[:512],
                }))

        # A join/leave during observation reads changes the required proof set.
        # Refuse before any shared marker changes, then retry with the new
        # exact roster rather than accepting a mixed membership generation.
        if firewall_truth.exact_compatible_hosts() != hosts:
            job.add_log("aggregate_firewall_truth: runner roster changed")
            return False
        for row, values in ipset_updates:
            IPSet.objects.filter(
                pk=row.pk, modified=row.modified).update(**values)
        for row, values in geo_updates:
            GeoLocatedIP.objects.filter(
                pk=row.pk,
                firewall_generation=row.firewall_generation).update(**values)
        job.add_log(
            f"aggregate_firewall_truth: roster={len(hosts)} failures={failures}")
        return failures == 0
    except firewall_truth.FirewallTruthError as err:
        job.add_log(f"aggregate_firewall_truth: refused ({err.code})")
        return False
    finally:
        firewall_truth.release_desired_state(lease)


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
    import uuid
    get_adapter().set(force_key, uuid.uuid4().hex,
                      ex=SYNC_FIREWALL_MARKER_TTL)
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
    expired = list(
        GeoLocatedIP.objects.filter(
            is_blocked=True,
            blocked_until__isnull=False,
            blocked_until__lte=dates.utcnow(),
        ).order_by("pk")[:SYNC_FIREWALL_MAX_OBJECTS + 1]
    )

    if not expired:
        return {"verified": 0, "unverified": 0}
    if len(expired) > SYNC_FIREWALL_MAX_OBJECTS:
        job.add_log("sweep_expired_blocks: scope exceeds safety bound")
        return {"verified": 0, "unverified": len(expired)}
    verified = 0
    for row in expired:
        result = row.unblock_checked(reason="expired")
        if result.get("status") == "verified" and result.get("ok") is True:
            verified += 1
    unverified = len(expired) - verified
    job.add_log(
        f"sweep_expired_blocks: verified {verified}; unverified {unverified}")
    return {"verified": verified, "unverified": unverified}


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
    from mojo.apps.incident.services import firewall_truth

    name = data.get("name")
    cidrs = data.get("cidrs", [])

    if not name:
        logit.warning("broadcast_sync_ipset called with no name")
        return
    try:
        name = firewall_truth.canonical_set_name(name)
        if name == firewall_truth.permanent_set_name():
            raise firewall_truth.FirewallTruthError(
                "reserved_set_name", "configured permanent set is reserved")
        cidrs = firewall_truth.canonical_ipv4_networks(cidrs)
    except firewall_truth.FirewallTruthError as err:
        logit.warning("broadcast_sync_ipset refused: %s", err.code)
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
    from mojo.apps.incident.services import firewall_truth

    name = data.get("name")

    if not name:
        return
    try:
        name = firewall_truth.canonical_set_name(name)
        if name == firewall_truth.permanent_set_name():
            raise firewall_truth.FirewallTruthError(
                "reserved_set_name", "configured permanent set is reserved")
    except firewall_truth.FirewallTruthError as err:
        logit.warning("broadcast_remove_ipset refused: %s", err.code)
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
    from mojo.apps.incident.services import llm_dispatch

    from mojo.apps.account.services import llm_safety
    enabled, watermark = llm_safety.autonomous_triage_state()
    if not enabled or watermark is None \
            or not llm_safety.route_state("incident_triage")["ready"]:
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

        _, created = llm_dispatch.claim_incident(
            incident, event_id=event.pk, ruleset_id=incident.rule_set_id)
        if created:
            incident.add_history(
                "handler:llm", note="[LLM Agent] Queued for automated triage")
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
