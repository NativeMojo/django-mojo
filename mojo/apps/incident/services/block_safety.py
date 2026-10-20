"""
Which addresses the framework may block (maestro #8885).

One predicate, asked at three boundaries so no path can put a DROP for an
unsafe address into the kernel:

- the desired-state write (`GeoLocatedIP._block_checked_locked`), which every
  block entry point goes through;
- the desired-state builders reconciliation uses (`firewall_truth`
  snapshots), which drop unsafe rows and carve unsafe ranges out of sets;
- every present-direction dispatch in `mojo/apps/incident/firewall.py`, the
  only caller of the root broker, as a fail-closed backstop.

Unsafe means any of: an address in `NON_GLOBAL_V4`, a member of
`MOJOSEC_PROTECTED_CIDRS` or `MOJOSEC_TRUSTED_PROXY_CIDRS`, or an actively
whitelisted address. Both settings are read from the settings file and accept
a list or a comma-separated string of at most 64 IPv4 CIDRs. Anything else is
`protected_config_invalid`, which refuses every new block: blocking the site's
own infrastructure is worse than a missed block.
"""
import bisect
import ipaddress

from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.apps.incident.services.firewall_truth import (
    MAX_FIREWALL_NETWORKS,
    FirewallTruthError,
    canonical_ipv4_address,
    canonical_ipv4_networks,
)

# Pinned, not `ipaddress.is_global`: that answer drifts across Python
# versions (3.12 calls 224.0.0.5, 239.1.1.1 and 192.0.0.9 global). These are
# the IANA special-purpose blocks that are not globally reachable, plus
# multicast and reserved space (240/4 covers 255.255.255.255).
NON_GLOBAL_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)

PROTECTED_SETTINGS = (
    ("MOJOSEC_PROTECTED_CIDRS", "mojosec_protected_cidrs"),
    ("MOJOSEC_TRUSTED_PROXY_CIDRS", "mojosec_trusted_proxy_cidrs"),
)
MAX_PROTECTED_CIDRS = 64

_UNSET = object()


def _range(network):
    return int(network.network_address), int(network.broadcast_address)


_NON_GLOBAL_RANGES = tuple(sorted(
    _range(ipaddress.IPv4Network(value)) for value in NON_GLOBAL_V4))


def _parse_cidrs(value):
    """A list of IPv4 networks, or None when the value is malformed.

    Unset or empty means none. A list, or a comma-separated string, of at
    most MAX_PROTECTED_CIDRS IPv4 CIDRs is accepted; anything else is None.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)) or len(value) > MAX_PROTECTED_CIDRS:
        return None
    networks = []
    for row in value:
        if not isinstance(row, str):
            return None
        try:
            network = ipaddress.ip_network(row.strip(), strict=False)
        except ValueError:
            return None
        if network.version != 4:
            return None
        networks.append(network)
    return networks


def protected_networks():
    """[(code, [networks])] for both settings, or None when either is malformed."""
    result = []
    for name, code in PROTECTED_SETTINGS:
        networks = _parse_cidrs(settings.get_static(name, None))
        if networks is None:
            logit.error(f"block safety: {name} is malformed; refusing new blocks")
            return None
        result.append((code, networks))
    return result


def _in_ranges(value, ranges):
    index = bisect.bisect_right(ranges, (value, float("inf"))) - 1
    return index >= 0 and ranges[index][0] <= value <= ranges[index][1]


def is_non_global(address):
    """True when the IPv4 address lies in NON_GLOBAL_V4."""
    return _in_ranges(int(ipaddress.IPv4Address(address)), _NON_GLOBAL_RANGES)


def _whitelisted_addresses():
    from django.db.models import Q
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.helpers import dates

    rows = GeoLocatedIP.objects.filter(is_whitelisted=True).filter(
        Q(whitelisted_until__isnull=True) |
        Q(whitelisted_until__gt=dates.utcnow())).values_list(
            "ip_address", flat=True)
    addresses = []
    for value in rows:
        try:
            addresses.append(canonical_ipv4_address(value))
        except FirewallTruthError:
            continue
    return addresses


def _is_whitelisted(canonical):
    from mojo.apps.account.models import GeoLocatedIP
    row = GeoLocatedIP.objects.filter(ip_address=canonical).first()
    return bool(row is not None and row.whitelist_active)


def check_block_target(ip, *, protected=_UNSET, whitelist=True):
    """(canonical_or_None, code) for one block target; code "" means safe.

    Codes, checked in this order: invalid_network, unsupported_family,
    non_global_address, protected_config_invalid, mojosec_protected_cidrs,
    mojosec_trusted_proxy_cidrs, whitelisted.

    `protected` takes a protected_networks() result already read, so a
    caller checking many rows parses the settings once. `whitelist=False`
    skips the whitelist lookup for callers whose rows already exclude it.
    """
    try:
        canonical = canonical_ipv4_address(ip)
    except FirewallTruthError as err:
        return None, err.code
    if is_non_global(canonical):
        return canonical, "non_global_address"
    if protected is _UNSET:
        protected = protected_networks()
    if protected is None:
        return canonical, "protected_config_invalid"
    address = ipaddress.IPv4Address(canonical)
    for code, networks in protected:
        if any(address in network for network in networks):
            return canonical, code
    if whitelist and _is_whitelisted(canonical):
        return canonical, "whitelisted"
    return canonical, ""


def _merged(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def safe_block_networks(cidrs, *, protected=_UNSET, whitelist=_UNSET):
    """(kept, carved) for a list of networks to block.

    `kept` is the canonical networks left after subtracting NON_GLOBAL_V4,
    both protected settings and every actively whitelisted address. `carved`
    lists the input networks that lost addresses. Raises FirewallTruthError
    `protected_config_invalid` on malformed settings and `network_limit` when
    the carve-out leaves more than MAX_FIREWALL_NETWORKS networks; it never
    truncates.

    `protected` and `whitelist` (a list of addresses) are keyword-only seams;
    production reads the settings and the database.
    """
    canonical = canonical_ipv4_networks(cidrs)
    if protected is _UNSET:
        protected = protected_networks()
    if protected is None:
        raise FirewallTruthError(
            "protected_config_invalid", "block protection settings are malformed")
    if whitelist is _UNSET:
        whitelist = _whitelisted_addresses()
    holes = list(_NON_GLOBAL_RANGES)
    for _code, networks in protected:
        holes.extend(_range(network) for network in networks)
    for address in whitelist:
        value = int(ipaddress.IPv4Address(address))
        holes.append((value, value))
    holes = _merged(holes)
    starts = [start for start, _end in holes]

    kept = []
    carved = []
    for value in canonical:
        network = ipaddress.IPv4Network(value)
        start, end = _range(network)
        index = max(bisect.bisect_right(starts, start) - 1, 0)
        cursor = start
        pieces = []
        while index < len(holes) and holes[index][0] <= end:
            hole_start, hole_end = holes[index]
            index += 1
            if hole_end < cursor:
                continue
            if hole_start > cursor:
                pieces.append((cursor, hole_start - 1))
            cursor = max(cursor, hole_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            pieces.append((cursor, end))
        if pieces == [(start, end)]:
            kept.append(value)
            continue
        carved.append(value)
        for piece_start, piece_end in pieces:
            kept.extend(str(net) for net in ipaddress.summarize_address_range(
                ipaddress.IPv4Address(piece_start), ipaddress.IPv4Address(piece_end)))
            if len(kept) > MAX_FIREWALL_NETWORKS:
                raise FirewallTruthError(
                    "network_limit", "carve-out leaves too many firewall networks")
    return canonical_ipv4_networks(kept), carved
