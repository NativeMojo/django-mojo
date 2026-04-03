"""
Standalone firewall management module.

Manages iptables and ipset rules directly — no dependency on OSSEC scripts.
Used by the broadcast job system to enforce fleet-wide IP blocks.

Must run as ec2-user (which has passwordless sudo for iptables/ipset).
Called only from async jobs — never from the web process.
"""
import getpass
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


# ---------------------------------------------------------------------------
# Single IP blocking (iptables)
# ---------------------------------------------------------------------------

def is_blocked(ip):
    """Check if an IP is currently blocked in iptables."""
    ip = _validate_ip(ip)
    if not ip:
        return False
    ok, stdout, _ = _run([IPTABLES_SAVE])
    if not ok:
        return False
    return ip in stdout


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

    ok, _, stderr = _run([IPTABLES, "-I", "INPUT", "-s", ip, "-j", "DROP"])
    if not ok:
        logit.error(f"iptables block INPUT failed for {ip}: {stderr}")
        return False

    # Also block forwarded traffic if forwarding is enabled
    try:
        with open("/proc/sys/net/ipv4/ip_forward") as f:
            if f.read().strip() == "1":
                _run([IPTABLES, "-I", "FORWARD", "-s", ip, "-j", "DROP"])
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

    _run([IPTABLES, "-D", "INPUT", "-s", ip, "-j", "DROP"])
    _run([IPTABLES, "-D", "FORWARD", "-s", ip, "-j", "DROP"])

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

    # Create the set if it doesn't exist (hash:net handles IPs as /32)
    _run([IPSET, "create", name, "hash:net", "-exist"])

    ok, _, stderr = _run([IPSET, "add", name, ip, "-exist"])
    if not ok:
        logit.error(f"ipset add failed for {name}/{ip}: {stderr}")
        return False

    # Ensure iptables rule exists for this set
    ok, stdout, _ = _run([IPTABLES_SAVE])
    if ok and f"--match-set {name}" not in stdout:
        _run([IPTABLES, "-I", "INPUT", "-m", "set", "--match-set", name, "src", "-j", "DROP"])

    return True


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

    ok, _, stderr = _run([IPSET, "del", name, ip, "-exist"])
    if not ok:
        logit.error(f"ipset del failed for {name}/{ip}: {stderr}")
        return False
    return True


def ipset_load(name, cidrs):
    """
    Create/replace an ipset with the given CIDRs and attach an iptables rule.

    This is the main entry point for bulk blocking. It:
    1. Creates the ipset if it doesn't exist
    2. Flushes any existing entries
    3. Loads all CIDRs
    4. Ensures an iptables DROP rule exists for the set

    Returns (success, loaded_count).
    """
    name = _validate_ipset_name(name)
    if not name:
        return False, 0

    # Create the set (hash:net for CIDR support, -exist to skip if exists)
    ok, _, stderr = _run([IPSET, "create", name, "hash:net", "-exist"])
    if not ok:
        logit.error(f"ipset create failed for {name}: {stderr}")
        return False, 0

    # Flush existing entries
    _run([IPSET, "flush", name])

    # Load CIDRs
    loaded = 0
    for cidr in cidrs:
        cidr = _validate_ip(cidr)
        if not cidr:
            continue
        ok, _, _ = _run([IPSET, "add", name, cidr, "-exist"])
        if ok:
            loaded += 1

    # Ensure iptables rule exists for this set (check first to avoid duplicates)
    ok, stdout, _ = _run([IPTABLES_SAVE])
    if ok and f"--match-set {name}" not in stdout:
        _run([IPTABLES, "-I", "INPUT", "-m", "set", "--match-set", name, "src", "-j", "DROP"])

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

    # Remove iptables rule first
    _run([IPTABLES, "-D", "INPUT", "-m", "set", "--match-set", name, "src", "-j", "DROP"])

    # Flush and destroy the set
    _run([IPSET, "flush", name])
    _run([IPSET, "destroy", name])

    logit.info(f"ipset {name}: removed")
    return True
