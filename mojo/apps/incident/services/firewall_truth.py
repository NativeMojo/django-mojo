"""Canonical firewall desired/observed truth for checked fleet operations.

The canonicalization helpers intentionally have no Django imports so the
root-owned broker can use the exact same algorithm before settings exist.
Network dispatch imports stay inside the reconciliation functions.
"""

import hashlib
import ipaddress
import re


FIREWALL_SEMANTIC_SCHEMA = "mojo.firewall.semantic"
FIREWALL_SEMANTIC_VERSION = 1
MAX_FIREWALL_NETWORKS = 250000
MAX_FIREWALL_ERROR = 512
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
    from mojo.apps.jobs.adapters import get_adapter
    import uuid
    redis = get_adapter()
    token = uuid.uuid4().hex
    ttl = max(5, min(310, int(float(timeout)) + 10))
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
    desired = {"ip": canonical, "present": bool(present)}
    from mojo.apps import jobs
    checked = jobs.broadcast_execute_checked(
        "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_ip",
        desired, timeout=timeout, channel=channel,
        correlation_id=correlation_id)
    result = _verified_checked(checked, "ip", desired)
    result["desired"] = desired
    return result


def reconcile_set(name, cidrs, present=True, channel="default", timeout=135.0,
                  correlation_id=None):
    try:
        name = canonical_set_name(name)
        canonical = canonical_ipv4_networks(cidrs)
    except FirewallTruthError as err:
        return _failure(err.code, err)
    desired = {
        "name": name,
        "present": bool(present),
        "count": len(canonical) if present else 0,
        "digest": network_digest(canonical if present else []),
    }
    from mojo.apps import jobs
    lock_key = f"mojo:firewall:set-reconcile:{name}"
    try:
        redis, token = _lease(lock_key, timeout)
    except Exception:
        result = _failure("set_lock_unavailable",
                          "set reconciliation serialization is unavailable")
        result["desired"] = desired
        return result
    if token is None:
        result = _failure("set_busy", "set reconciliation is busy")
        result["desired"] = desired
        return result
    try:
        checked = jobs.broadcast_execute_checked(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_firewall_set",
            {"name": name, "cidrs": canonical, "present": bool(present)},
            timeout=timeout, channel=channel, correlation_id=correlation_id)
    finally:
        try:
            _release_lease(redis, lock_key, token)
        except Exception:
            pass
    result = _verified_checked(checked, "set", desired)
    result["desired"] = desired
    return result


def reconcile_geolocated_ip(ip, permanent_ips, temporary_present,
                            channel="default", timeout=135.0,
                            correlation_id=None):
    """Reconcile one row and the permanent aggregate in one host snapshot."""
    try:
        canonical = canonical_ipv4_address(ip)
        permanent = canonical_ipv4_networks(permanent_ips)
        set_name = permanent_set_name()
    except FirewallTruthError as err:
        return _failure(err.code, err)
    permanent_desired = {
        "name": set_name, "present": True,
        "count": len(permanent), "digest": network_digest(permanent),
    }
    ip_desired = {"ip": canonical, "present": bool(temporary_present)}
    desired = {"ip": ip_desired, "permanent": permanent_desired}
    from mojo.apps import jobs
    lock_key = "mojo:firewall:permanent-reconcile-lock"
    try:
        redis, token = _lease(lock_key, timeout)
    except Exception:
        result = _failure(
            "aggregate_lock_unavailable",
            "permanent aggregate serialization is unavailable")
        result["desired"] = desired
        return result
    if token is None:
        result = _failure(
            "aggregate_busy", "permanent aggregate reconciliation is busy")
        result["desired"] = desired
        return result
    try:
        checked = jobs.broadcast_execute_checked(
            "mojo.apps.incident.asyncjobs.broadcast_reconcile_geolocated_ip",
            {"ip": canonical, "permanent_set_name": set_name,
             "permanent_ips": permanent,
             "temporary_present": bool(temporary_present)},
            timeout=timeout, channel=channel, correlation_id=correlation_id)
    finally:
        try:
            _release_lease(redis, lock_key, token)
        except Exception:
            pass
    result = _verified_checked(checked, "geolocated_ip", desired)
    result["desired"] = desired
    return result


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
