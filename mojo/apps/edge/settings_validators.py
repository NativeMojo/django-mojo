"""Validation for protected fleet topology, node identity, and the framework hold."""

import re


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
POOL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_NODES = 64
MAX_POOLS = 32

# The protected Setting key holding the fleet's framework version hold. Named
# here rather than in deploy.py so the app's ready() can register it without
# importing the deploy service (which pulls in requests and Redis).
FRAMEWORK_VERSION_KEY = "EDGE_FRAMEWORK_VERSION"

# What an operator types when they mean "take the newest published release" —
# which is exactly what an UNSET hold does. Storing "latest" literally would
# make it a version string nobody publishes, and the deploy would die at pip.
UNSET_PIN_WORDS = frozenset({"", "latest", "none", "auto"})


def node_id(value):
    value = str(value or "").strip().lower()
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            "EDGE_NODE_ID must be 1-63 lowercase letters, digits, dots, dashes, or underscores")
    return value


def framework_pin(key, value):
    """Normalize the framework version hold, at write time AND at read time.

    Three accepted forms, and nothing else:

    - a published django-mojo version (leading digit, e.g. ``1.11.9``) — every
      deploy installs exactly that;
    - ``hold`` — stay on the framework version the fleet last converged onto,
      so a commit ships without also moving the framework;
    - empty, or one of the unset synonyms above — install the newest published
      release (the default, and the behavior that predates this setting).

    Anything else is refused HERE, at the portal, naming the accepted forms. A
    typo that only surfaces as a refused deploy an hour later is exactly the
    failure this validator exists to prevent. ``stable`` and ``v1.2.3`` are the
    two shapes operators reach for; both are refused, because neither is a
    version pip can install.

    Casefolding is safe: PEP 440 comparison is case-insensitive and normalizes
    to lowercase, so ``1.0.0RC1`` and ``1.0.0rc1`` are the same release.
    """
    from mojo.apps.edge.services import deploy

    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip().casefold()
    if value in UNSET_PIN_WORDS:
        return ""
    if value == deploy.FRAMEWORK_HOLD:
        return deploy.FRAMEWORK_HOLD
    # The leading-digit rule is what separates a version from a word: the
    # pattern alone would happily accept "stable".
    if value[0].isdigit() and deploy.VERSION_PATTERN.match(value):
        return value
    raise ValueError(
        f"{key} must be a published django-mojo version (for example 1.11.9), "
        f"'hold' to stay on the last converged fleet version, or empty "
        f"('latest', 'none', 'auto') for the newest published release")


def expected_topology(key, value):
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a JSON object")
    if set(value) - {"nodes", "pools"}:
        raise ValueError(f"{key} accepts only nodes and pools")
    nodes = value.get("nodes")
    pools = value.get("pools")
    if not isinstance(nodes, list) or not isinstance(pools, list):
        raise ValueError(f"{key} nodes and pools must be lists")
    if not nodes or not pools:
        raise ValueError(f"{key} requires at least one node and one pool")
    if len(nodes) > MAX_NODES or len(pools) > MAX_POOLS:
        raise ValueError(f"{key} exceeds the supported fleet size")
    clean_nodes = [node_id(item) for item in nodes]
    clean_pools = []
    for item in pools:
        item = str(item or "").strip().lower()
        if not POOL_RE.fullmatch(item):
            raise ValueError(
                f"{key} pools use lowercase letters, digits, dashes, or underscores")
        clean_pools.append(item)
    # Package A's protected-setting contract is canonicalizing: repeated UI
    # selections round-trip as one sorted identity rather than failing a save.
    return {"nodes": sorted(set(clean_nodes)), "pools": sorted(set(clean_pools))}
