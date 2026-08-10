"""Validation for protected fleet topology and node identity."""

import re


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
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
    clean_pools = [node_id(item) for item in pools]
    if len(set(clean_nodes)) != len(clean_nodes) or len(set(clean_pools)) != len(clean_pools):
        raise ValueError(f"{key} nodes and pools must not contain duplicates")
    return {"nodes": sorted(clean_nodes), "pools": sorted(clean_pools)}
