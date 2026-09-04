"""Canonical firewall desired/observed truth for checked fleet operations.

The canonicalization helpers intentionally have no Django imports so the
root-owned broker can use the exact same algorithm before settings exist.
Network dispatch imports stay inside the reconciliation functions.
"""

import hashlib
import ipaddress
import json
import re
import uuid


FIREWALL_SEMANTIC_SCHEMA = "mojo.firewall.semantic"
FIREWALL_SEMANTIC_VERSION = 1
MAX_FIREWALL_NETWORKS = 250000
MAX_FIREWALL_ERROR = 512
FIREWALL_OBSERVATION_TTL = 7200
DESIRED_STATE_LOCK = "mojo:firewall:desired-state-lock"
FENCE_PREFIX = "mojo:firewall:fence"
OBSERVATION_PREFIX = "mojo:firewall:observation"
_SET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,31}$")


class FirewallTruthError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical_ipv4(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 49:
        raise FirewallTruthError("invalid_network", "network is invalid")
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as err:
        raise FirewallTruthError("invalid_network", "network is invalid") from err
    if network.version != 4:
        raise FirewallTruthError(
            "unsupported_family", "only IPv4 firewall targets are supported")
    return str(network)


def canonical_ipv4_address(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 45:
        raise FirewallTruthError("invalid_network", "IP address is invalid")
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as err:
        raise FirewallTruthError("invalid_network", "IP address is invalid") from err
    if address.version != 4:
        raise FirewallTruthError(
            "unsupported_family", "only IPv4 firewall targets are supported")
    return str(address)


def canonical_ipv4_networks(values, limit=MAX_FIREWALL_NETWORKS):
    if not isinstance(values, (list, tuple)):
        raise FirewallTruthError("invalid_networks", "networks must be an array")
    if len(values) > limit:
        raise FirewallTruthError("network_limit", "too many firewall networks")
    return sorted(set(canonical_ipv4(value) for value in values))


def canonical_set_name(value):
    if not isinstance(value, str) or not _SET_NAME.fullmatch(value):
        raise FirewallTruthError("invalid_set_name", "firewall set name is invalid")
    if value.endswith("_tmp"):
        raise FirewallTruthError(
            "reserved_set_name", "temporary firewall set names are reserved")
    return value


def permanent_set_name():
    """Return the configured aggregate name with atomic-swap room proven."""
    from mojo.helpers.settings import settings
    value = canonical_set_name(settings.get_static(
        "FIREWALL_BLOCKED_IPSET_NAME", "mojo_blocked"))
    if len(value) + 4 > 31:
        raise FirewallTruthError(
            "invalid_set_name", "permanent set name is too long for replacement")
    return value


def network_digest(values):
    canonical = canonical_ipv4_networks(values)
    return hashlib.sha256("\n".join(canonical).encode("ascii")).hexdigest()


def state_fingerprint(value):
    try:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise FirewallTruthError(
            "invalid_desired_state", "desired state is not canonical JSON") from err
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _redis_client():
    from mojo.apps.jobs.adapters import get_adapter
    adapter = get_adapter()
    return adapter.get_client() if hasattr(adapter, "get_client") else adapter


class DesiredStateLease:
    def __init__(self, redis, token):
        self.redis = redis
        self.token = token


def acquire_desired_state(timeout=180.0):
    """Serialize desired snapshot, dispatch, and global finalization."""
    redis, token = _lease(DESIRED_STATE_LOCK, max(180.0, float(timeout)))
    if token is None:
        raise FirewallTruthError(
            "desired_state_busy", "firewall desired state is being reconciled")
    return DesiredStateLease(redis, token)


def release_desired_state(lease):
    if lease is not None:
        _release_lease(lease.redis, DESIRED_STATE_LOCK, lease.token)


def desired_state_is_current(lease):
    """Return whether this caller still owns the global desired-state lease."""
    if lease is None:
        return False
    value = lease.redis.get(DESIRED_STATE_LOCK)
    if isinstance(value, bytes):
        value = value.decode("utf-8", "strict")
    return value == lease.token


def renew_desired_state(lease, ttl=600):
    if lease is None:
        return False
    return bool(lease.redis.eval("""
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
""", 1, DESIRED_STATE_LOCK, lease.token,
        str(max(30, min(600, int(ttl))))))


def fence_key(kind, identity):
    if kind not in ("ip", "set", "permanent"):
        raise FirewallTruthError("invalid_fence", "firewall fence kind is invalid")
    if kind == "ip":
        identity = canonical_ipv4_address(identity)
    else:
        identity = canonical_set_name(identity)
    return f"{FENCE_PREFIX}:{kind}:{identity}"


def _parse_fence(value):
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError) as err:
        raise FirewallTruthError("fence_invalid", "firewall fence is invalid") from err
    if parsed < 0:
        raise FirewallTruthError("fence_invalid", "firewall fence is invalid")
    return parsed


def read_fences(redis, targets):
    return {
        (kind, identity): _parse_fence(redis.get(fence_key(kind, identity)))
        for kind, identity in targets
    }


def advance_fences(lease, targets):
    """Atomically advance each distinct desired-state generation."""
    unique = list(dict.fromkeys(targets))
    if not unique:
        return {}
    keys = [fence_key(kind, identity) for kind, identity in unique]
    script = """
local values = {}
for index, key in ipairs(KEYS) do
  values[index] = redis.call('incr', key)
end
return values
"""
    values = lease.redis.eval(script, len(keys), *keys)
    if not isinstance(values, (list, tuple)) or len(values) != len(unique):
        raise FirewallTruthError("fence_unavailable", "firewall fence advance failed")
    return {target: _parse_fence(value)
            for target, value in zip(unique, values)}


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def permanent_snapshot():
    """Canonical aggregate truth; quarantine bad legacy rows independently."""
    from mojo.apps.account.models import GeoLocatedIP
    from django.db.models import Q
    from mojo.helpers import dates

    active_whitelist = Q(is_whitelisted=True) & (
        Q(whitelisted_until__isnull=True) |
        Q(whitelisted_until__gt=dates.utcnow()))
    rows = list(GeoLocatedIP.objects.filter(
        is_blocked=True, blocked_until__isnull=True).exclude(
            active_whitelist).order_by("pk")[:MAX_FIREWALL_NETWORKS + 1])
    if len(rows) > MAX_FIREWALL_NETWORKS:
        raise FirewallTruthError("network_limit", "too many permanent firewall rows")
    members = []
    invalid = []
    generations = []
    for row in rows:
        try:
            canonical = canonical_ipv4_address(row.ip_address)
        except FirewallTruthError as err:
            invalid.append((row.pk, err.code))
            continue
        members.append(canonical)
        generations.append((row.pk, row.firewall_generation, canonical))
    cidrs = canonical_ipv4_networks(members)
    value = {
        "name": permanent_set_name(), "cidrs": cidrs,
        "generations": generations, "quarantined": invalid,
    }
    value["fingerprint"] = state_fingerprint(value)
    return value


def geolocated_snapshot(ip, permanent=None):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.helpers import dates

    canonical = canonical_ipv4_address(ip)
    row = GeoLocatedIP.objects.filter(ip_address=canonical).first()
    if row is None:
        raise FirewallTruthError("target_missing", "firewall target is missing")
    now = dates.utcnow()
    whitelisted = bool(
        row.is_whitelisted and
        (row.whitelisted_until is None or row.whitelisted_until > now))
    active = bool(
        row.is_blocked and
        (row.blocked_until is None or row.blocked_until > now) and
        not whitelisted)
    temporary = bool(active and row.blocked_until is not None)
    permanent = permanent or permanent_snapshot()
    desired = {
        "ip": {"ip": canonical, "present": temporary},
        "permanent": {
            "name": permanent["name"], "present": True,
            "count": len(permanent["cidrs"]),
            "digest": network_digest(permanent["cidrs"]),
        },
    }
    source = {
        "pk": row.pk, "generation": row.firewall_generation,
        "is_blocked": row.is_blocked, "blocked_until": _iso(row.blocked_until),
        "is_whitelisted": row.is_whitelisted,
        "whitelisted_until": _iso(row.whitelisted_until),
        "permanent_fingerprint": permanent["fingerprint"],
        "desired": desired,
    }
    return {
        "row": row, "canonical": canonical, "permanent": permanent,
        "desired": desired, "fingerprint": state_fingerprint(source),
    }


def ipset_snapshot(name):
    from mojo.apps.incident.models import IPSet

    name = canonical_set_name(name)
    if name == permanent_set_name():
        raise FirewallTruthError(
            "reserved_set_name", "configured permanent set name is reserved")
    row = IPSet.objects.filter(name=name).first()
    if row is None:
        raise FirewallTruthError("target_missing", "firewall set is missing")
    present = bool(row.is_enabled and not row.is_cache_only)
    cidrs = canonical_ipv4_networks(row.cidrs if present else [])
    if present and len(name) + 4 > 31:
        raise FirewallTruthError(
            "invalid_set_name", "set name is too long for replacement")
    desired = {
        "name": name, "present": present,
        "count": len(cidrs) if present else 0,
        "digest": network_digest(cidrs if present else []),
    }
    source = {
        "pk": row.pk, "modified": _iso(row.modified),
        "enabled": row.is_enabled, "cidrs": cidrs, "desired": desired,
    }
    return {"row": row, "cidrs": cidrs, "desired": desired,
            "fingerprint": state_fingerprint(source)}


def observation_key(kind, identity, fence, host):
    if kind not in ("ip", "geo", "set", "permanent"):
        raise FirewallTruthError(
            "invalid_observation", "firewall observation kind is invalid")
    identity = (canonical_ipv4_address(identity)
                if kind in ("ip", "geo") else canonical_set_name(identity))
    if isinstance(fence, bool) or not isinstance(fence, int) or fence <= 0:
        raise FirewallTruthError(
            "invalid_fence", "firewall observation fence is invalid")
    if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,253}", host):
        raise FirewallTruthError("invalid_host", "firewall host identity is invalid")
    return f"{OBSERVATION_PREFIX}:{kind}:{identity}:{fence}:{host}"


def record_host_observation(kind, identity, fence, fingerprint, desired):
    from mojo.apps.jobs.job_engine import host_channel
    if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint):
        raise FirewallTruthError(
            "invalid_observation", "firewall fingerprint is invalid")
    host = host_channel()
    value = {
        "schema": FIREWALL_SEMANTIC_SCHEMA, "version": FIREWALL_SEMANTIC_VERSION,
        "kind": kind, "identity": identity, "fence": int(fence),
        "fingerprint": fingerprint, "host": host, "desired": desired,
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         allow_nan=False)
    if len(payload.encode()) > 65536:
        raise FirewallTruthError("observation_overflow", "host observation is too large")
    if not _redis_client().set(
            observation_key(kind, identity, fence, host), payload,
            ex=FIREWALL_OBSERVATION_TTL):
        raise FirewallTruthError(
            "observation_unavailable", "host observation could not be recorded")
    return value


def exact_compatible_hosts(channel="default"):
    from mojo.apps import jobs
    try:
        manager = jobs.get_manager()
        rows = manager.get_runners_bounded(channel, limit=128, timeout=2.0)
        selected, expected, incompatible = manager._checked_host_roster(
            rows, channel)
    except Exception as err:
        raise FirewallTruthError(
            "runner_roster_unavailable", "runner roster is unavailable") from err
    if not expected:
        raise FirewallTruthError("runner_roster_empty", "runner roster is empty")
    if incompatible or set(selected) != set(expected):
        raise FirewallTruthError(
            "runner_roster_incompatible", "runner roster is not fully compatible")
    return expected


def aggregate_observations(kind, identity, fence, fingerprint, desired,
                           channel="default", hosts=None):
    try:
        verify_current_roster = hosts is None
        hosts = list(hosts) if hosts is not None else exact_compatible_hosts(channel)
        if not hosts or len(hosts) != len(set(hosts)):
            raise FirewallTruthError(
                "runner_roster_invalid", "runner roster is invalid")
        redis = _redis_client()
        rows = []
        for host in hosts:
            raw = redis.get(observation_key(kind, identity, fence, host))
            if not isinstance(raw, (bytes, str)):
                raise FirewallTruthError(
                    "host_observation_missing", "host observation is missing")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if len(raw.encode()) > 65536:
                raise FirewallTruthError(
                    "observation_overflow", "host observation is too large")
            row = json.loads(
                raw,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")),
                object_pairs_hook=_unique_object)
            if row != {
                    "schema": FIREWALL_SEMANTIC_SCHEMA,
                    "version": FIREWALL_SEMANTIC_VERSION,
                    "kind": kind, "identity": identity, "fence": int(fence),
                    "fingerprint": fingerprint, "host": host,
                    "desired": desired}:
                raise FirewallTruthError(
                    "host_observation_mismatch", "host observation is stale")
            rows.append(row)
        if verify_current_roster and exact_compatible_hosts(channel) != hosts:
            raise FirewallTruthError(
                "runner_roster_changed", "runner roster changed during aggregation")
        return {"status": "verified", "ok": True,
                "expected_hosts": hosts, "observations": rows}
    except (FirewallTruthError, UnicodeError, ValueError, TypeError) as err:
        code = err.code if isinstance(err, FirewallTruthError) else "observation_invalid"
        return _failure(code, err)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate observation key")
        value[key] = item
    return value


def current_ipset_enforcement(row, channel="default", hosts=None):
    """Return fleet aggregate truth for the row's exact current generation."""
    try:
        snapshot = ipset_snapshot(row.name)
        redis = _redis_client()
        fence = read_fences(redis, [("set", row.name)])[("set", row.name)]
        if fence <= 0:
            raise FirewallTruthError(
                "generation_unobserved", "IPSet generation has no fence")
        result = aggregate_observations(
            "set", row.name, fence, snapshot["fingerprint"],
            snapshot["desired"], channel, hosts=hosts)
        result.update(desired=snapshot["desired"], fence=fence,
                      fingerprint=snapshot["fingerprint"])
        return result
    except Exception as err:
        if isinstance(err, FirewallTruthError):
            return _failure(err.code, err)
        return _failure(
            "enforcement_unavailable", "firewall enforcement is unavailable")


def mark_superseded_pending(kind, identity):
    """Durably prevent a stale command from looking like verified truth."""
    message = "generation_superseded: delayed firewall command was refused"
    if kind == "set":
        from mojo.apps.incident.models import IPSet
        IPSet.objects.filter(name=identity).update(
            last_synced=None, sync_error=message)
        return
    if kind in ("geo", "permanent"):
        from mojo.apps.account.models import GeoLocatedIP
        if kind == "geo":
            GeoLocatedIP.objects.filter(ip_address=identity).update(
                firewall_pending=True, firewall_sync_error=message)
        GeoLocatedIP.objects.filter(
            is_blocked=True, blocked_until__isnull=True).update(
                firewall_pending=True, firewall_sync_error=message)


def _failure(code, message, checked=None):
    return {
        "status": "unknown" if checked is None else "partial",
        "ok": False,
        "error": {"code": code, "message": str(message)[:MAX_FIREWALL_ERROR]},
        "checked": checked,
    }


def _verified_checked(checked, kind, desired):
    """Verify every host's semantic observation, poisoning any mismatch."""
    if not isinstance(checked, dict) or checked.get("status") != "verified":
        return _failure(
            "fleet_unverified", "the compatible host snapshot was not verified",
            checked if isinstance(checked, dict) else None)
    expected = checked.get("expected_hosts")
    responded = checked.get("responded_hosts")
    succeeded = checked.get("succeeded_hosts")
    failed = checked.get("failed_hosts")
    missing = checked.get("missing_hosts")
    results = checked.get("results")
    raw_anomalies = checked.get("anomalies")
    if (not isinstance(expected, list) or not expected or
            any(not isinstance(host, str) for host in expected) or
            responded != expected or succeeded != expected or
            failed != [] or missing != [] or raw_anomalies != [] or
            not isinstance(results, list) or len(results) != len(expected)):
        return _failure(
            "checked_evidence_invalid", "checked host evidence is incomplete",
            checked)
    anomalies = []
    result_hosts = []
    for row in results:
        host = row.get("host", "unknown") if isinstance(row, dict) else "unknown"
        result_hosts.append(host)
        result = row.get("result") if isinstance(row, dict) else None
        if (not isinstance(result, dict) or
                result.get("schema") != FIREWALL_SEMANTIC_SCHEMA or
                result.get("version") != FIREWALL_SEMANTIC_VERSION or
                result.get("kind") != kind or result.get("ok") is not True or
                result.get("desired") != desired or
                result.get("observed") != desired):
            anomalies.append(f"semantic_mismatch:{host}")
    if result_hosts != expected:
        anomalies.append("checked_host_result_mismatch")
    if anomalies:
        value = dict(checked)
        value["status"] = "partial"
        value["anomalies"] = anomalies[:64]
        return _failure(
            "semantic_mismatch", "one or more hosts did not prove desired state",
            value)
    return {"status": "verified", "ok": True, "checked": checked}


def _lease(key, timeout):
    redis = _redis_client()
    token = uuid.uuid4().hex
    ttl = max(5, min(600, int(float(timeout)) + 30))
    if not redis.set(key, token, nx=True, ex=ttl):
        return redis, None
    return redis, token


def _release_lease(redis, key, token):
    if token is None:
        return
    redis.eval("""
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
""", 1, key, token)


def reconcile_ip(ip, present, channel="default", timeout=10.0,
                 correlation_id=None):
    try:
        canonical = canonical_ipv4_address(ip)
    except FirewallTruthError as err:
        return _failure(err.code, err)
    if not isinstance(present, bool):
        return _failure("invalid_request", "present must be a boolean")
    desired = {"ip": canonical, "present": present}
    from mojo.apps import jobs
    lease = None
    try:
        lease = acquire_desired_state(timeout)
        fences = advance_fences(lease, [("ip", canonical)])
        fence = fences[("ip", canonical)]
        fingerprint = state_fingerprint(desired)
        checked = jobs.broadcast_execute_checked(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_ip",
            {**desired, "fence": fence, "fingerprint": fingerprint,
             "lease_token": lease.token},
            timeout=timeout, channel=channel, correlation_id=correlation_id)
        if (not desired_state_is_current(lease) or
                read_fences(lease.redis, [("ip", canonical)])[
                    ("ip", canonical)] != fence or
                state_fingerprint(desired) != fingerprint):
            return {**_failure(
                "generation_superseded", "IP desired state changed", checked),
                "desired": desired}
        result = _verified_checked(checked, "ip", desired)
        if result.get("ok") is True:
            result = aggregate_observations(
                "ip", canonical, fence, fingerprint, desired, channel)
            result["checked"] = checked
        result["desired"] = desired
        return result
    except FirewallTruthError as err:
        return _failure(err.code, err)
    finally:
        release_desired_state(lease)


def reconcile_set(name, cidrs, present=True, channel="default", timeout=135.0,
                  correlation_id=None, lease=None):
    owned_lease = lease is None
    try:
        name = canonical_set_name(name)
        canonical = canonical_ipv4_networks(cidrs)
        if not isinstance(present, bool):
            raise FirewallTruthError("invalid_request", "present must be a boolean")
        if name == permanent_set_name():
            raise FirewallTruthError(
                "reserved_set_name", "configured permanent set name is reserved")
    except FirewallTruthError as err:
        return _failure(err.code, err)
    desired = {
        "name": name,
        "present": present,
        "count": len(canonical) if present else 0,
        "digest": network_digest(canonical if present else []),
    }
    try:
        if owned_lease:
            lease = acquire_desired_state(timeout)
        snapshot = ipset_snapshot(name)
        if snapshot["desired"] != desired:
            raise FirewallTruthError(
                "desired_state_changed", "IPSet desired state changed before dispatch")
        fences = read_fences(lease.redis, [("set", name)])
        fence = fences[("set", name)]
        if fence == 0:
            fence = advance_fences(lease, [("set", name)])[("set", name)]
        from mojo.apps import jobs
        checked = jobs.broadcast_execute_checked(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_set",
            {"name": name, "cidrs": canonical, "present": present,
             "fence": fence, "fingerprint": snapshot["fingerprint"],
             "lease_token": lease.token},
            timeout=timeout, channel=channel, correlation_id=correlation_id)
        current = ipset_snapshot(name)
        current_fence = read_fences(
            lease.redis, [("set", name)])[("set", name)]
        if (not desired_state_is_current(lease) or
                current["fingerprint"] != snapshot["fingerprint"] or
                current_fence != fence):
            type(current["row"]).objects.filter(pk=current["row"].pk).update(
                sync_error="generation_superseded: IPSet desired state changed")
            return {**_failure(
                "generation_superseded", "IPSet desired state changed", checked),
                "desired": desired}
        result = _verified_checked(checked, "set", desired)
        if result.get("ok") is True:
            result = aggregate_observations(
                "set", name, fence, snapshot["fingerprint"], desired, channel)
            result["checked"] = checked
        result["desired"] = desired
        result["fence"] = fence
        result["fingerprint"] = snapshot["fingerprint"]
        return result
    except Exception as err:
        if not isinstance(err, FirewallTruthError):
            err = FirewallTruthError(
                "desired_state_unavailable", "IPSet desired state is unavailable")
        result = _failure(err.code, err)
        result["desired"] = desired
        return result
    finally:
        if owned_lease:
            release_desired_state(lease)


def reconcile_geolocated_ip(ip, permanent_ips, temporary_present,
                            channel="default", timeout=135.0,
                            correlation_id=None, lease=None):
    """Reconcile one row and the permanent aggregate in one host snapshot."""
    owned_lease = lease is None
    try:
        canonical = canonical_ipv4_address(ip)
        permanent = canonical_ipv4_networks(permanent_ips)
        set_name = permanent_set_name()
        if not isinstance(temporary_present, bool):
            raise FirewallTruthError(
                "invalid_request", "temporary_present must be a boolean")
    except FirewallTruthError as err:
        return _failure(err.code, err)
    permanent_desired = {
        "name": set_name, "present": True,
        "count": len(permanent), "digest": network_digest(permanent),
    }
    ip_desired = {"ip": canonical, "present": temporary_present}
    desired = {"ip": ip_desired, "permanent": permanent_desired}
    try:
        if owned_lease:
            lease = acquire_desired_state(timeout)
        snapshot = geolocated_snapshot(canonical)
        if snapshot["desired"] != desired:
            raise FirewallTruthError(
                "desired_state_changed", "firewall desired state changed before dispatch")
        targets = [("ip", canonical), ("permanent", set_name)]
        fences = read_fences(lease.redis, targets)
        missing = [target for target, value in fences.items() if value == 0]
        if missing:
            fences.update(advance_fences(lease, missing))
        ip_fence = fences[("ip", canonical)]
        aggregate_fence = fences[("permanent", set_name)]
        from mojo.apps import jobs
        checked = jobs.broadcast_execute_checked(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_geolocated_ip",
            {"ip": canonical, "permanent_set_name": set_name,
             "permanent_ips": permanent,
             "temporary_present": temporary_present,
             "ip_fence": ip_fence,
             "aggregate_fence": aggregate_fence,
             "fingerprint": snapshot["fingerprint"],
             "aggregate_fingerprint": snapshot["permanent"]["fingerprint"],
             "lease_token": lease.token},
            timeout=timeout, channel=channel, correlation_id=correlation_id)
        current = geolocated_snapshot(canonical)
        current_fences = read_fences(lease.redis, targets)
        if (not desired_state_is_current(lease) or
                current["fingerprint"] != snapshot["fingerprint"] or
                current["permanent"]["fingerprint"] !=
                snapshot["permanent"]["fingerprint"] or
                current_fences != fences):
            from mojo.apps.account.models import GeoLocatedIP
            GeoLocatedIP.objects.filter(pk=current["row"].pk).update(
                firewall_pending=True,
                firewall_sync_error=(
                    "generation_superseded: firewall desired state changed"))
            GeoLocatedIP.objects.filter(
                is_blocked=True, blocked_until__isnull=True).update(
                    firewall_pending=True)
            return {**_failure(
                "generation_superseded", "firewall desired state changed", checked),
                "desired": desired}
        result = _verified_checked(checked, "geolocated_ip", desired)
        if result.get("ok") is True:
            geo_result = aggregate_observations(
                "geo", canonical, ip_fence, snapshot["fingerprint"],
                desired, channel)
            permanent_result = aggregate_observations(
                "permanent", set_name, aggregate_fence,
                snapshot["permanent"]["fingerprint"], permanent_desired,
                channel)
            result = geo_result if geo_result.get("ok") is not True \
                else permanent_result
            result["checked"] = checked
        result["desired"] = desired
        result["fence"] = ip_fence
        result["aggregate_fence"] = aggregate_fence
        result["fingerprint"] = snapshot["fingerprint"]
        return result
    except Exception as err:
        if not isinstance(err, FirewallTruthError):
            err = FirewallTruthError(
                "desired_state_unavailable", "firewall desired state is unavailable")
        result = _failure(err.code, err)
        result["desired"] = desired
        return result
    finally:
        if owned_lease:
            release_desired_state(lease)


def bounded_error(result):
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "fleet_unverified")[:64]
            message = str(error.get("message") or code)[:MAX_FIREWALL_ERROR]
            return code, message
        return str(result.get("status") or "fleet_unverified")[:64], \
            "firewall state was not verified"
    return "fleet_unverified", "firewall state was not verified"
