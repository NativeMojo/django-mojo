"""
Standalone brokered firewall management module.

Sends semantic requests to a root-owned firewall broker.
Used by the broadcast job system to enforce fleet-wide IP blocks.

Must run as ec2-user (which may sudo only the empty-argv broker).
Called only from async jobs — never from the web process.
"""
import getpass
import json
import re
import subprocess
from mojo.helpers import logit
from mojo.apps.incident.services.firewall_truth import (
    FirewallTruthError,
    canonical_ipv4,
    canonical_ipv4_networks,
    canonical_set_name,
    permanent_set_name,
)

ALLOWED_USER = "ec2-user"

SUDO = "/usr/bin/sudo"
IPTABLES = "/sbin/iptables"
IPTABLES_SAVE = "/sbin/iptables-save"
IPSET = "/sbin/ipset"
BROKER = "/usr/local/sbin/mojo-firewall-broker"
_BROKER_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _broker_error(code):
    return {"ok": False, "error": {"code": code}}


def _log_broker_failure(operation, code, returncode=None, response_length=0):
    logit.error(
        "firewall broker failed %s code=%s returncode=%s response_length=%s",
        operation, str(code)[:64], returncode, response_length)


def _validate_ip(ip):
    """Return the canonical IPv4 network or fail closed."""
    try:
        return canonical_ipv4(ip)
    except FirewallTruthError as err:
        logit.error("Firewall target refused: %s", err.code)
        return None


def _validate_ipset_name(name):
    """Validate ipset name to prevent injection."""
    try:
        return canonical_set_name(name)
    except FirewallTruthError as err:
        logit.error("Invalid ipset name rejected: %s", err.code)
        return None


def _check_user():
    """Verify we are running as ec2-user. Returns True or logs error."""
    user = getpass.getuser()
    if user != ALLOWED_USER:
        logit.error(f"firewall.py must run as {ALLOWED_USER}, not {user}")
        return False
    return True


def _run(args, timeout=10):
    """Run a command via sudo. Returns (success, stdout, stderr)."""
    if not _check_user():
        return False, "", "wrong user"
    try:
        result = subprocess.run(
            [SUDO] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logit.error(f"Command timed out: {args}")
        return False, "", "timeout"
    except Exception as e:
        logit.error(f"Command failed: {args} — {e}")
        return False, "", str(e)


def _run_stdin(args, stdin_data, timeout=30):
    """Run a command via sudo with data piped to stdin. Returns (success, stdout, stderr)."""
    if not _check_user():
        return False, "", "wrong user"
    try:
        result = subprocess.run(
            [SUDO] + args,
            input=stdin_data, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logit.error(f"Command timed out (stdin): {args}")
        return False, "", "timeout"
    except Exception as e:
        logit.error(f"Command failed (stdin): {args} — {e}")
        return False, "", str(e)


def _broker_request(operation, timeout=20, **values):
    """Call the exact empty-argv broker with immutable engine identity."""
    if not _check_user():
        return _broker_error("broker_wrong_user")
    from mojo.apps.jobs.execution_context import current
    context = current()
    if context is None:
        logit.error("firewall operation rejected outside JobEngine execution context")
        return _broker_error("broker_context_unavailable")
    if (operation.startswith("set.") or operation.startswith("permanent.") or
            operation == "geolocated.normalize"):
        try:
            values["expected_permanent_set"] = permanent_set_name()
        except FirewallTruthError as err:
            logit.error("firewall reservation refused: %s", err.code)
            return _broker_error(err.code)
    payload = json.dumps(
        {"operation": operation, "context": context, **values},
        sort_keys=True, separators=(",", ":"))
    try:
        result = subprocess.run(
            [SUDO, "-n", "--", BROKER], input=payload,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log_broker_failure(operation, "broker_timeout")
        return _broker_error("broker_timeout")
    except OSError:
        _log_broker_failure(operation, "broker_start_failed")
        return _broker_error("broker_start_failed")
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    response_length = len(stdout)
    try:
        value = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        _log_broker_failure(
            operation, "broker_malformed_response", result.returncode,
            response_length)
        return _broker_error("broker_malformed_response")
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        _log_broker_failure(
            operation, "broker_invalid_response", result.returncode,
            response_length)
        return _broker_error("broker_invalid_response")
    if result.returncode or not value["ok"]:
        error = value.get("error") if isinstance(value.get("error"), dict) else {}
        raw_code = error.get("code")
        if isinstance(raw_code, str) and _BROKER_CODE.fullmatch(raw_code):
            code = raw_code
        elif not result.returncode and raw_code is None:
            code = "semantic_mismatch"
        else:
            code = "broker_invalid_response"
            value = _broker_error(code)
        if result.returncode and value["ok"]:
            code = "broker_invalid_response"
            value = _broker_error(code)
        _log_broker_failure(
            operation, code, result.returncode, response_length)
    return value


def ip_status(ip):
    ip = _validate_ip(ip)
    if not ip:
        return {"ok": False, "error": {"code": "unsupported_or_invalid_ip"}}
    return _broker_request("ip.status", source=ip) or {
        "ok": False, "error": {"code": "broker_unavailable"}}


def normalize_ip(ip, present):
    ip = _validate_ip(ip)
    if not ip:
        return {"ok": False, "error": {"code": "unsupported_or_invalid_ip"}}
    return _broker_request(
        "ip.normalize", source=ip, present=bool(present)) or {
            "ok": False, "error": {"code": "broker_unavailable"}}


def ipset_status(name):
    name = _validate_ipset_name(name)
    if not name:
        return {"ok": False, "error": {"code": "invalid_set_name"}}
    return _broker_request("set.status", set_name=name) or {
        "ok": False, "error": {"code": "broker_unavailable"}}


def normalize_ipset(name, cidrs, present=True):
    name = _validate_ipset_name(name)
    if not name:
        return {"ok": False, "error": {"code": "invalid_set_name"}}
    try:
        canonical = canonical_ipv4_networks(cidrs)
    except FirewallTruthError as err:
        return {"ok": False, "error": {"code": err.code}}
    return _broker_request(
        "set.normalize", timeout=125, set_name=name, cidrs=canonical,
        present=bool(present)) or {
            "ok": False, "error": {"code": "broker_unavailable"}}


def normalize_permanent_ipset(cidrs):
    """Normalize the root-owned aggregate without caller target authority."""
    try:
        canonical = canonical_ipv4_networks(cidrs)
    except FirewallTruthError as err:
        return {"ok": False, "error": {"code": err.code}}
    return _broker_request(
        "permanent.normalize", timeout=125, cidrs=canonical) or {
            "ok": False, "error": {"code": "broker_unavailable"}}


def normalize_geolocated_ip(ip, permanent_set_name, permanent_cidrs,
                            temporary_present):
    """Normalize both parts of one GeoLocatedIP while one broker lock is held."""
    ip = _validate_ip(ip)
    name = _validate_ipset_name(permanent_set_name)
    if not ip or not name:
        return {"ok": False, "error": {"code": "invalid_firewall_target"}}
    try:
        canonical = canonical_ipv4_networks(permanent_cidrs)
    except FirewallTruthError as err:
        return {"ok": False, "error": {"code": err.code}}
    return _broker_request(
        "geolocated.normalize", timeout=125, source=ip,
        cidrs=canonical, temporary_present=bool(temporary_present)) or {
            "ok": False, "error": {"code": "broker_unavailable"}}


# ---------------------------------------------------------------------------
# Single IP blocking (iptables)
# ---------------------------------------------------------------------------

def is_blocked(ip):
    """Check if an IP is currently blocked in iptables."""
    ip = _validate_ip(ip)
    if not ip:
        return False
    result = _broker_request("rules.contains", source=ip)
    return bool(result and result.get("present") is True)


def block(ip):
    """
    Block an IP via iptables. Returns True on success.
    Idempotent — skips if already blocked.
    """
    ip = _validate_ip(ip)
    if not ip:
        return False

    result = normalize_ip(ip, True)
    observed = result.get("observed") if isinstance(result, dict) else None
    ok = bool(result and result.get("ok") and isinstance(observed, dict) and
              observed.get("present") is True)
    if ok:
        logit.info(f"Blocked IP: {ip}")
    return ok


def unblock(ip):
    """
    Unblock an IP from iptables. Returns True on success.
    Idempotent — returns True if IP was not blocked.
    """
    ip = _validate_ip(ip)
    if not ip:
        return False

    result = normalize_ip(ip, False)
    observed = result.get("observed") if isinstance(result, dict) else None
    ok = bool(result and result.get("ok") and isinstance(observed, dict) and
              observed.get("input_count") == 0 and
              observed.get("forward_count") == 0)
    if ok:
        logit.info(f"Unblocked IP: {ip}")
    return ok


# ---------------------------------------------------------------------------
# Bulk blocking via ipset (countries, datacenters, abuse lists)
# ---------------------------------------------------------------------------

def ipset_add(name, ip):
    """
    Add a single IP to an ipset. Creates the set if it doesn't exist.
    Idempotent — safe to call multiple times for the same IP.

    Returns True on success, False on failure.
    """
    name = _validate_ipset_name(name)
    ip = _validate_ip(ip)
    if not name or not ip:
        return False
    if name != permanent_set_name():
        logit.error("permanent set add refused for operator-owned set %s", name)
        return False

    result = _broker_request("permanent.add", source=ip)
    if not result or not result["ok"]:
        logit.error(f"firewall broker failed set add for {name}/{ip}")
        return False
    ensured = _broker_request("permanent.rule_ensure")
    return bool(ensured and ensured["ok"])


def ipset_del(name, ip):
    """
    Remove a single IP from an ipset.
    Idempotent — safe to call if the IP is not in the set.

    Returns True on success, False on failure.
    """
    name = _validate_ipset_name(name)
    ip = _validate_ip(ip)
    if not name or not ip:
        return False
    if name != permanent_set_name():
        logit.error("permanent set delete refused for operator-owned set %s", name)
        return False

    result = _broker_request("permanent.delete", source=ip)
    return bool(result and result["ok"])


def _build_restore_script(name, cidrs):
    """Build an ipset restore script for atomic swap.

    Validates name and each CIDR, loads into a temp set, then swaps with the
    live set. Returns ("", 0) if name is invalid or no valid CIDRs remain
    (prevents accidentally wiping a live set with an empty swap).
    """
    name = _validate_ipset_name(name)
    if not name:
        return "", 0

    tmp_name = f"{name}_tmp"
    lines = [
        f"create {tmp_name} hash:net -exist",
        f"flush {tmp_name}",
    ]
    valid_count = 0
    for cidr in cidrs:
        cidr = _validate_ip(cidr)
        if not cidr:
            continue
        lines.append(f"add {tmp_name} {cidr}")
        valid_count += 1

    if valid_count == 0:
        # Don't swap — would silently wipe the live set
        lines.append(f"destroy {tmp_name}")
        return "\n".join(lines) + "\n", 0

    lines.append(f"swap {name} {tmp_name}")
    lines.append(f"destroy {tmp_name}")
    return "\n".join(lines) + "\n", valid_count


def ipset_load(name, cidrs):
    """
    Create/replace an ipset with the given CIDRs and attach an iptables rule.

    Uses `ipset restore` with atomic swap for bulk loading — one subprocess
    call regardless of CIDR count. The live set is never empty during the swap.

    Returns (success, loaded_count).
    """
    name = _validate_ipset_name(name)
    if not name:
        return False, 0

    try:
        values = canonical_ipv4_networks(cidrs)
    except FirewallTruthError:
        return False, 0
    result = normalize_ipset(name, values, present=True)
    observed = result.get("observed") if isinstance(result, dict) else None
    if (not result or not result.get("ok") or not isinstance(observed, dict) or
            observed.get("count") != len(values)):
        logit.error(f"firewall broker failed set normalization for {name}")
        return False, 0
    loaded = len(values)
    logit.info(f"ipset {name}: loaded {loaded}/{len(cidrs)} CIDRs")
    return True, loaded


def ipset_remove(name):
    """
    Remove an ipset and its iptables rule.
    Idempotent — safe to call if the set doesn't exist.
    """
    name = _validate_ipset_name(name)
    if not name:
        return False

    result = normalize_ipset(name, [], present=False)
    observed = result.get("observed") if isinstance(result, dict) else None
    if (not result or not result.get("ok") or not isinstance(observed, dict) or
            observed.get("exists") is not False):
        return False

    logit.info(f"ipset {name}: removed")
    return True
