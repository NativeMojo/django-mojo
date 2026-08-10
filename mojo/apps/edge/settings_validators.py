"""Validation for protected fleet topology and node identity."""

import re


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
POOL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_NODES = 64
MAX_POOLS = 32


def node_id(value):
    value = str(value or "").strip().lower()
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            "EDGE_NODE_ID must be 1-63 lowercase letters, digits, dots, dashes, or underscores")
    return value


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
