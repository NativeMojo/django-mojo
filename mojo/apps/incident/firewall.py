"""
Standalone brokered firewall management module.

Sends semantic requests to a root-owned firewall broker.
Used by the broadcast job system to enforce fleet-wide IP blocks.

Must run as ec2-user (which may sudo only the empty-argv broker).
Called only from async jobs — never from the web process.
"""
import getpass
import json
import subprocess
import re
from mojo.helpers import logit

ALLOWED_USER = "ec2-user"

# Validate IP/CIDR to prevent command injection
_IP_PATTERN = re.compile(
    r'^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$|'  # IPv4 or IPv4/CIDR
    r'^[0-9a-fA-F:]+(/\d{1,3})?$'             # IPv6 or IPv6/CIDR
)

_IPSET_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

SUDO = "/usr/bin/sudo"
IPTABLES = "/sbin/iptables"
IPTABLES_SAVE = "/sbin/iptables-save"
IPSET = "/sbin/ipset"
BROKER = "/usr/local/sbin/mojo-firewall-broker"


def _validate_ip(ip):
    """Validate IP/CIDR format to prevent injection."""
    if not ip or not isinstance(ip, str):
        return None
    ip = ip.strip()
    if not _IP_PATTERN.match(ip):
        logit.error(f"Invalid IP format rejected: {ip}")
        return None
    return ip


def _validate_ipset_name(name):
    """Validate ipset name to prevent injection."""
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if not _IPSET_NAME_PATTERN.match(name):
        logit.error(f"Invalid ipset name rejected: {name}")
        return None
    return name


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
        return None
    from mojo.apps.jobs.execution_context import current
    context = current()
    if context is None:
        logit.error("firewall operation rejected outside JobEngine execution context")
        return None
    payload = json.dumps(
        {"operation": operation, "context": context, **values},
        sort_keys=True, separators=(",", ":"))
    try:
        result = subprocess.run(
            [SUDO, "-n", "--", BROKER], input=payload,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logit.error(f"firewall broker timed out during {operation}")
        return None
    except OSError as err:
        logit.error(f"firewall broker could not start during {operation}: {err}")
        return None
    if result.returncode:
        logit.error(f"firewall broker rejected {operation}: {result.stderr[:512]}")
        return None
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        logit.error(f"firewall broker returned malformed output for {operation}")
        return None
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        logit.error(f"firewall broker returned an invalid result for {operation}")
        return None
    return value


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

    if is_blocked(ip):
        return True

    result = _broker_request("rule.insert", chain="INPUT", source=ip)
    if not result or not result["ok"]:
        logit.error(f"firewall broker failed to block INPUT source {ip}")
        return False

    # Also block forwarded traffic if forwarding is enabled
    try:
        with open("/proc/sys/net/ipv4/ip_forward") as f:
            if f.read().strip() == "1":
                _broker_request("rule.insert", chain="FORWARD", source=ip)
    except (FileNotFoundError, PermissionError):
        pass

    logit.info(f"Blocked IP: {ip}")
    return True


def unblock(ip):
    """
    Unblock an IP from iptables. Returns True on success.
    Idempotent — returns True if IP was not blocked.
    """
    ip = _validate_ip(ip)
    if not ip:
        return False

    if not is_blocked(ip):
        return True

    _broker_request("rule.delete", chain="INPUT", source=ip)
    _broker_request("rule.delete", chain="FORWARD", source=ip)

    logit.info(f"Unblocked IP: {ip}")
    return True


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

    result = _broker_request("set.add", set_name=name, source=ip)
    if not result or not result["ok"]:
        logit.error(f"firewall broker failed set add for {name}/{ip}")
        return False
    ensured = _broker_request("set.rule_ensure", set_name=name)
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

    result = _broker_request("set.delete", set_name=name, source=ip)
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

    values = [value for value in (_validate_ip(cidr) for cidr in cidrs) if value]
    if not values:
        return False, 0
    result = _broker_request("set.replace", timeout=125, set_name=name, cidrs=values)
    if not result or not result["ok"]:
        logit.error(f"firewall broker failed set replace for {name}")
        return False, 0
    ensured = _broker_request("set.rule_ensure", set_name=name)
    if not ensured or not ensured["ok"]:
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

    result = _broker_request("set.remove", set_name=name)
    if not result or not result["ok"]:
        return False

    logit.info(f"ipset {name}: removed")
    return True
