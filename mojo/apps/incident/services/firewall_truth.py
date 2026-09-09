"""Canonical firewall desired/observed truth for checked fleet operations.

The canonicalization helpers intentionally have no Django imports so the
root-owned broker can use the exact same algorithm before settings exist.
Network dispatch imports stay inside the reconciliation functions.
"""

import hashlib
import ipaddress
import json
import re
import time
import uuid


FIREWALL_SEMANTIC_SCHEMA = "mojo.firewall.semantic"
FIREWALL_SEMANTIC_VERSION = 1
MAX_FIREWALL_NETWORKS = 250000
MAX_FIREWALL_ERROR = 512
FIREWALL_OBSERVATION_TTL = 7200
DESIRED_STATE_LOCK = "mojo:firewall:desired-state-lock"
DESIRED_STATE_GENERATION = "mojo:firewall:desired-state-generation"
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


def canonical_operator_ipset(name, values, present):
    """Validate the shared operator-IPSet namespace and stored IPv4 data."""
    name = canonical_set_name(name)
    if name.startswith("mojo_"):
        raise FirewallTruthError(
            "reserved_set_name", "framework firewall set names are reserved")
    if not isinstance(present, bool):
        raise FirewallTruthError("invalid_request", "present must be a boolean")
    # Absence names the safe set only; historical members are irrelevant.
    cidrs = canonical_ipv4_networks(values) if present else []
    if len(name) + 4 > 31:
        raise FirewallTruthError(
            "invalid_set_name", "set name is too long for replacement")
    return name, cidrs


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


def acquire_desired_state(timeout=1.0):
    """Acquire the short desired-state critical section with bounded retries."""
    try:
        wait = max(0.0, min(2.0, float(timeout)))
        redis = _redis_client()
        token = uuid.uuid4().hex
        deadline = time.monotonic() + wait
        while True:
            if redis.set(DESIRED_STATE_LOCK, token, nx=True, ex=30):
                return DesiredStateLease(redis, token)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.025, remaining))
    except Exception as err:
        raise FirewallTruthError(
            "desired_state_unavailable",
            "firewall desired-state lease is unavailable") from err
    raise FirewallTruthError(
        "desired_state_busy", "firewall desired state is being reconciled; retry")


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
    unique = list(dict.fromkeys(targets))
    keys = [fence_key(kind, identity) for kind, identity in unique]
    values = redis.mget(keys) if keys and hasattr(redis, "mget") else [
        redis.get(key) for key in keys]
    return {target: _parse_fence(value)
            for target, value in zip(unique, values)}


def read_desired_generation(redis):
    return _parse_fence(redis.get(DESIRED_STATE_GENERATION))


def advance_fences(lease, targets):
    """Atomically advance each distinct desired-state generation."""
    if lease is None:
        raise FirewallTruthError(
            "desired_state_busy", "firewall desired-state lease is missing")
    unique = list(dict.fromkeys(targets))
    if not unique:
        return {}
    keys = [fence_key(kind, identity) for kind, identity in unique]
    script = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
  return false
end
redis.call('incr', KEYS[2])
local values = {}
for index = 3, #KEYS do
  values[index - 2] = redis.call('incr', KEYS[index])
end
return values
"""
    values = lease.redis.eval(
        script, len(keys) + 2, DESIRED_STATE_LOCK,
        DESIRED_STATE_GENERATION, *keys, lease.token)
    if values is None or values is False:
        raise FirewallTruthError(
            "desired_state_busy", "firewall desired-state lease expired")
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


def geolocated_snapshot_from_row(row, permanent=None):
    from mojo.helpers import dates

    canonical = canonical_ipv4_address(row.ip_address)
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


def geolocated_snapshot(ip, permanent=None):
    from mojo.apps.account.models import GeoLocatedIP
    canonical = canonical_ipv4_address(ip)
    row = GeoLocatedIP.objects.filter(ip_address=canonical).first()
    if row is None:
        raise FirewallTruthError("target_missing", "firewall target is missing")
    return geolocated_snapshot_from_row(row, permanent=permanent)


def ipset_snapshot_from_row(row):
    name = canonical_set_name(row.name)
    if name == permanent_set_name():
        raise FirewallTruthError(
            "reserved_set_name", "configured permanent set name is reserved")
    present = bool(row.is_enabled and not row.is_cache_only)
    name, cidrs = canonical_operator_ipset(name, row.cidrs, present)
    desired = {
        "name": name, "present": present,
        "count": len(cidrs) if present else 0,
        "digest": network_digest(cidrs if present else []),
    }
    source = {
        "pk": row.pk, "enabled": row.is_enabled,
        "cidrs": cidrs, "desired": desired,
    }
    return {"row": row, "cidrs": cidrs if present else [], "desired": desired,
            "fingerprint": state_fingerprint(source)}


def ipset_snapshot(name):
    from mojo.apps.incident.models import IPSet

    name = canonical_set_name(name)
    row = IPSet.objects.filter(name=name).first()
    if row is None:
        raise FirewallTruthError("target_missing", "firewall set is missing")
    return ipset_snapshot_from_row(row)


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


def current_host_runner_target():
    from mojo.apps.jobs.execution_context import current_runner_incarnation
    from mojo.apps.jobs.job_engine import host_channel

    incarnation = current_runner_incarnation()
    if (not isinstance(incarnation, dict) or
            not isinstance(incarnation.get("started"), str) or
            not 1 <= len(incarnation["started"]) <= 96):
        raise FirewallTruthError(
            "runner_incarnation_unavailable",
            "current runner heartbeat incarnation is unavailable")
    return {"host": host_channel(), "runner_id": incarnation["runner_id"],
            "started": incarnation["started"]}


def current_host_incarnation():
    target = current_host_runner_target()
    return {"host": target["host"], "started": target["started"]}


def record_host_observation(kind, identity, fence, fingerprint, desired,
                            incarnation=None):
    if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint):
        raise FirewallTruthError(
            "invalid_observation", "firewall fingerprint is invalid")
    incarnation = incarnation or current_host_incarnation()
    if (not isinstance(incarnation, dict) or
            set(incarnation) != {"host", "started"} or
            not isinstance(incarnation.get("host"), str) or
            not isinstance(incarnation.get("started"), str) or
            not 1 <= len(incarnation["started"]) <= 96):
        raise FirewallTruthError(
            "invalid_observation", "runner incarnation is invalid")
    host = incarnation["host"]
    value = {
        "schema": FIREWALL_SEMANTIC_SCHEMA, "version": FIREWALL_SEMANTIC_VERSION,
        "kind": kind, "identity": identity, "fence": int(fence),
        "fingerprint": fingerprint, "host": host,
        "started": incarnation["started"], "desired": desired,
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


_EXPECTED_FROM_FILE = object()


def expected_hosts(value=_EXPECTED_FROM_FILE):
    """Fleet membership is exclusively an explicit, strict settings-file list."""
    if value is _EXPECTED_FROM_FILE:
        from mojo.helpers.settings import settings
        value = settings.get_static("FIREWALL_EXPECTED_HOSTS", None)
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 128:
        raise FirewallTruthError("expected_hosts_missing", "FIREWALL_EXPECTED_HOSTS must name the complete fleet")
    if (any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,253}", host)
            for host in value) or len(set(value)) != len(value)):
        raise FirewallTruthError("expected_hosts_invalid", "FIREWALL_EXPECTED_HOSTS is invalid")
    return sorted(value)


def firewall_roster(*, rows=None, expected=_EXPECTED_FROM_FILE, manager=None):
    """Keep expected, currently capable, and unavailable hosts distinct."""
    from mojo.apps.jobs.manager import get_manager
    expected = expected_hosts(expected)
    try:
        manager = manager or get_manager()
        rows = (manager.get_runners_bounded("firewall", limit=128, timeout=2.0)
                if rows is None else rows)
        eligible = [row for row in rows if isinstance(row, dict)
                    and row.get("hostname") in expected
                    and isinstance(row.get("capabilities"), dict)
                    and type(row["capabilities"].get("firewall_reconcile")) is int
                    and row["capabilities"]["firewall_reconcile"] == 1
                    and isinstance(row.get("channels"), list)
                    and row.get("runner_id") in row["channels"]]
        selected, unused_expected, incompatible = manager._checked_host_roster(eligible, "firewall")
    except Exception as err:
        raise FirewallTruthError(
            "runner_roster_unavailable", "runner roster is unavailable") from err
    return {"expected_hosts": expected,
            "selected": [selected[host] for host in sorted(selected)],
            "selected_hosts": sorted(selected),
            "unavailable_hosts": sorted(set(expected) - set(selected))}


def repair_runner_roster():
    return [{"host": row["hostname"], "runner_id": row["runner_id"], "started": row["started"]}
            for row in firewall_roster()["selected"]]


def require_firewall_channel(channel):
    if channel != "firewall":
        raise FirewallTruthError(
            "invalid_firewall_channel", "firewall operations require the firewall channel")


def exact_compatible_runner_roster(channel="firewall"):
    require_firewall_channel(channel)
    fleet = firewall_roster()
    if fleet["unavailable_hosts"]:
        raise FirewallTruthError(
            "expected_hosts_unavailable", "one or more expected firewall hosts are unavailable")
    return [{"host": row["hostname"], "runner_id": row["runner_id"], "started": row["started"]}
            for row in fleet["selected"]]


def exact_compatible_roster(channel="firewall"):
    return [{"host": row["host"], "started": row["started"]}
            for row in exact_compatible_runner_roster(channel)]


def exact_compatible_hosts(channel="firewall"):
    """Compatibility projection for callers that only display host names."""
    return [row["host"] for row in exact_compatible_roster(channel)]


def aggregate_observations(kind, identity, fence, fingerprint, desired,
                           channel="firewall", roster=None):
    try:
        require_firewall_channel(channel)
        verify_current_roster = roster is None
        roster = ([dict(row) for row in roster] if roster is not None
                  else exact_compatible_roster(channel))
        if (not roster or any(
                not isinstance(row, dict) or
                set(row) != {"host", "started"} or
                not isinstance(row.get("host"), str) or
                not isinstance(row.get("started"), str) or
                not 1 <= len(row["started"]) <= 96
                for row in roster)):
            raise FirewallTruthError(
                "runner_roster_invalid", "runner roster is invalid")
        hosts = [row["host"] for row in roster]
        if hosts != sorted(hosts) or len(hosts) != len(set(hosts)):
            raise FirewallTruthError(
                "runner_roster_invalid", "runner roster is invalid")
        redis = _redis_client()
        rows = []
        for expected in roster:
            host = expected["host"]
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
                    "started": expected["started"],
                    "desired": desired}:
                raise FirewallTruthError(
                    "host_observation_mismatch", "host observation is stale")
            rows.append(row)
        if (verify_current_roster and
                exact_compatible_roster(channel) != roster):
            raise FirewallTruthError(
                "runner_roster_changed", "runner roster changed during aggregation")
        return {"status": "verified", "ok": True,
                "expected_hosts": hosts, "expected_roster": roster,
                "observations": rows}
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


def current_ipset_enforcement(row, channel="firewall", roster=None):
    """Return fleet aggregate truth for the row's exact current generation."""
    try:
        require_firewall_channel(channel)
        snapshot = ipset_snapshot(row.name)
        redis = _redis_client()
        fence = read_fences(redis, [("set", row.name)])[("set", row.name)]
        if fence <= 0:
            raise FirewallTruthError(
                "generation_unobserved", "IPSet generation has no fence")
        result = aggregate_observations(
            "set", row.name, fence, snapshot["fingerprint"],
            snapshot["desired"], channel, roster=roster)
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
    roster = checked.get("expected_roster")
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
            not isinstance(roster, list) or len(roster) != len(expected) or
            any(not isinstance(row, dict) or
                set(row) != {"host", "started"} or
                not isinstance(row.get("host"), str) or
                not isinstance(row.get("started"), str) or
                not 1 <= len(row["started"]) <= 96 for row in roster) or
            [row.get("host") for row in roster
             if isinstance(row, dict)] != expected or
            not isinstance(results, list) or len(results) != len(expected)):
        return _failure(
            "checked_evidence_invalid", "checked host evidence is incomplete",
            checked)
    anomalies = []
    result_hosts = []
    incarnations = {
        row["host"]: row.get("started") for row in roster
        if isinstance(row, dict) and set(row) == {"host", "started"}
    }
    if len(incarnations) != len(expected):
        return _failure(
            "checked_evidence_invalid", "checked host incarnation is incomplete",
            checked)
    for row in results:
        host = row.get("host", "unknown") if isinstance(row, dict) else "unknown"
        result_hosts.append(host)
        result = row.get("result") if isinstance(row, dict) else None
        result_started = row.get("started") if isinstance(row, dict) else None
        if (not isinstance(result, dict) or
                result.get("schema") != FIREWALL_SEMANTIC_SCHEMA or
                result.get("version") != FIREWALL_SEMANTIC_VERSION or
                result.get("kind") != kind or result.get("ok") is not True or
                result_started != incarnations.get(host) or
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


def _checked_current_roster(checked, channel):
    """Bind final observation reads to the exact dispatched incarnations."""
    expected = checked.get("expected_roster")
    current = exact_compatible_roster(channel)
    if current != expected:
        raise FirewallTruthError(
            "runner_roster_changed",
            "runner roster changed during checked reconciliation")
    return current


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


def _dispatch_firewall(func, payload, timeout, correlation_id, *,
                       manager=None, expected=_EXPECTED_FROM_FILE):
    from mojo.apps.jobs.manager import get_manager
    manager = manager or get_manager()
    fleet = firewall_roster(manager=manager, expected=expected)
    checked = manager.broadcast_execute_checked(
        func, payload, timeout=timeout, channel="firewall",
        roster=fleet["selected"], correlation_id=correlation_id)
    checked["fleet_expected_hosts"] = fleet["expected_hosts"]
    checked["selected_hosts"] = fleet["selected_hosts"]
    checked["unavailable_hosts"] = fleet["unavailable_hosts"]
    if fleet["unavailable_hosts"] and checked.get("status") == "verified":
        checked["status"] = "partial"
    return checked


def reconcile_ip(ip, present, channel="firewall", timeout=10.0,
                 correlation_id=None):
    try:
        require_firewall_channel(channel)
        canonical = canonical_ipv4_address(ip)
    except FirewallTruthError as err:
        return _failure(err.code, err)
    if not isinstance(present, bool):
        return _failure("invalid_request", "present must be a boolean")
    desired = {"ip": canonical, "present": present}
    from mojo.apps import jobs
    lease = None
    checked = None
    try:
        lease = acquire_desired_state()
        fences = advance_fences(lease, [("ip", canonical)])
        fence = fences[("ip", canonical)]
        fingerprint = state_fingerprint(desired)
    except FirewallTruthError as err:
        return _failure(err.code, err)
    finally:
        release_desired_state(lease)
        lease = None
    try:
        checked = _dispatch_firewall(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_ip",
            {**desired, "fence": fence, "fingerprint": fingerprint},
            timeout=timeout, correlation_id=correlation_id)
        lease = acquire_desired_state()
        if (read_fences(lease.redis, [("ip", canonical)])[
                ("ip", canonical)] != fence or
                state_fingerprint(desired) != fingerprint):
            return {**_failure(
                "generation_superseded", "IP desired state changed", checked),
                "desired": desired}
        result = _verified_checked(checked, "ip", desired)
        if result.get("ok") is True:
            roster = _checked_current_roster(checked, channel)
            result = aggregate_observations(
                "ip", canonical, fence, fingerprint, desired, channel,
                roster=roster)
            if exact_compatible_roster(channel) != roster:
                raise FirewallTruthError(
                    "runner_roster_changed",
                    "runner roster changed during checked finalization")
            result["checked"] = checked
        result["desired"] = desired
        return result
    except FirewallTruthError as err:
        result = _failure(err.code, err, checked)
        result["desired"] = desired
        return result
    finally:
        release_desired_state(lease)
        lease = None


def reconcile_set(name, cidrs, present=True, channel="firewall", timeout=135.0,
                  correlation_id=None):
    try:
        require_firewall_channel(channel)
        name = canonical_set_name(name)
        name, canonical = canonical_operator_ipset(name, cidrs, present)
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
    lease = None
    checked = None
    try:
        lease = acquire_desired_state()
        snapshot = ipset_snapshot(name)
        if snapshot["desired"] != desired:
            raise FirewallTruthError(
                "desired_state_changed", "IPSet desired state changed before dispatch")
        fences = read_fences(lease.redis, [("set", name)])
        fence = fences[("set", name)]
        if fence == 0:
            fence = advance_fences(lease, [("set", name)])[("set", name)]
        if not desired_state_is_current(lease):
            raise FirewallTruthError(
                "desired_state_busy", "IPSet desired-state lease expired")
    except Exception as err:
        if not isinstance(err, FirewallTruthError):
            err = FirewallTruthError(
                "desired_state_unavailable", "IPSet desired state is unavailable")
        result = _failure(err.code, err)
        result["desired"] = desired
        return result
    finally:
        release_desired_state(lease)
        lease = None
    try:
        from mojo.apps import jobs
        checked = _dispatch_firewall(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_set",
            {"name": name, "cidrs": canonical, "present": present,
             "fence": fence, "fingerprint": snapshot["fingerprint"]},
            timeout=timeout, correlation_id=correlation_id)
        lease = acquire_desired_state()
        current = ipset_snapshot(name)
        current_fence = read_fences(
            lease.redis, [("set", name)])[("set", name)]
        if (current["fingerprint"] != snapshot["fingerprint"] or
                current_fence != fence):
            type(current["row"]).objects.filter(pk=current["row"].pk).update(
                sync_error="generation_superseded: IPSet desired state changed")
            return {**_failure(
                "generation_superseded", "IPSet desired state changed", checked),
                "desired": desired}
        result = _verified_checked(checked, "set", desired)
        if result.get("ok") is True:
            roster = _checked_current_roster(checked, channel)
            result = aggregate_observations(
                "set", name, fence, snapshot["fingerprint"], desired, channel,
                roster=roster)
            if exact_compatible_roster(channel) != roster:
                raise FirewallTruthError(
                    "runner_roster_changed",
                    "runner roster changed during checked finalization")
            result["checked"] = checked
        result["desired"] = desired
        result["fence"] = fence
        result["fingerprint"] = snapshot["fingerprint"]
        return result
    except Exception as err:
        if not isinstance(err, FirewallTruthError):
            err = FirewallTruthError(
                "desired_state_unavailable", "IPSet desired state is unavailable")
        result = _failure(err.code, err, checked)
        result["desired"] = desired
        return result
    finally:
        release_desired_state(lease)


def reconcile_geolocated_ip(ip, permanent_ips, temporary_present,
                            channel="firewall", timeout=135.0,
                            correlation_id=None):
    """Reconcile one row and the permanent aggregate in one host snapshot."""
    try:
        require_firewall_channel(channel)
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
    lease = None
    checked = None
    try:
        lease = acquire_desired_state()
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
        if not desired_state_is_current(lease):
            raise FirewallTruthError(
                "desired_state_busy", "firewall desired-state lease expired")
    except Exception as err:
        if not isinstance(err, FirewallTruthError):
            err = FirewallTruthError(
                "desired_state_unavailable", "firewall desired state is unavailable")
        result = _failure(err.code, err)
        result["desired"] = desired
        return result
    finally:
        release_desired_state(lease)
        lease = None
    try:
        from mojo.apps import jobs
        checked = _dispatch_firewall(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_geolocated_ip",
            {"ip": canonical, "permanent_set_name": set_name,
             "permanent_ips": permanent,
             "temporary_present": temporary_present,
             "ip_fence": ip_fence,
             "aggregate_fence": aggregate_fence,
             "fingerprint": snapshot["fingerprint"],
             "aggregate_fingerprint": snapshot["permanent"]["fingerprint"]},
            timeout=timeout, correlation_id=correlation_id)
        lease = acquire_desired_state()
        current = geolocated_snapshot(canonical)
        current_fences = read_fences(lease.redis, targets)
        if (current["fingerprint"] != snapshot["fingerprint"] or
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
            roster = _checked_current_roster(checked, channel)
            geo_result = aggregate_observations(
                "geo", canonical, ip_fence, snapshot["fingerprint"],
                desired, channel, roster=roster)
            permanent_result = aggregate_observations(
                "permanent", set_name, aggregate_fence,
                snapshot["permanent"]["fingerprint"], permanent_desired,
                channel, roster=roster)
            if exact_compatible_roster(channel) != roster:
                raise FirewallTruthError(
                    "runner_roster_changed",
                    "runner roster changed during checked finalization")
            result = geo_result if geo_result.get("ok") is not True \
                else permanent_result
            result["checked"] = checked
        result["desired"] = desired
        result["fence"] = ip_fence
        result["aggregate_fence"] = aggregate_fence
        result["fingerprint"] = snapshot["fingerprint"]
        result["aggregate_fingerprint"] = snapshot["permanent"]["fingerprint"]
        return result
    except Exception as err:
        if not isinstance(err, FirewallTruthError):
            err = FirewallTruthError(
                "desired_state_unavailable", "firewall desired state is unavailable")
        result = _failure(err.code, err, checked)
        result["desired"] = desired
        return result
    finally:
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
