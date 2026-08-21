"""Deterministic Capacity fixtures for the Admin visual preview.

Answers the capacity report, one operation's progress, and the apply. States
are chosen so every branch the panel can render is reachable without an AWS
account: a healthy multi-node fleet with controls, a single-node fleet where
removal is refused, an add already in flight through its phase ladder, a denied
IAM read, a database with no reader, a fleet that pins EDGE_NODE_ID, and an
installation whose infrastructure is external.
"""

from urllib.parse import parse_qs

NAME = "capacity"

BALANCER_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                "loadbalancer/net/mojo-api-nlb/0123456789abcdef")
GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
             "targetgroup/mojo-api/abcdef0123456789")

# The phase ladder an add walks, in order. The status route steps one phase per
# poll so the panel's progress copy is exercised without a real wait.
ADD_PHASES = ("capturing", "launching", "booting", "converging", "proving",
              "registering", "settling")

NODES = [
    {"id": "i-0a1b2c3d4e5f60011", "name": "mojo-api-a", "state": "healthy",
     "instance_state": "running", "instance_type": "m6i.large",
     "zone": "us-east-1a", "healthy": True, "self": True, "primary": True,
     "added_by_capacity": False, "groups": [GROUP_ARN]},
    {"id": "i-0a1b2c3d4e5f60022", "name": "mojo-api-b", "state": "healthy",
     "instance_state": "running", "instance_type": "m6i.large",
     "zone": "us-east-1b", "healthy": True, "self": False, "primary": False,
     "added_by_capacity": False, "groups": [GROUP_ARN]},
    {"id": "i-0a1b2c3d4e5f60033", "name": "mojo-api-c", "state": "unused",
     "instance_state": "running", "instance_type": "m6i.large",
     "zone": "us-east-1c", "healthy": False, "self": False, "primary": False,
     "added_by_capacity": True, "groups": [GROUP_ARN]},
]

DATABASES = [
    {"identifier": "mojo-prod-aurora", "kind": "aurora",
     "engine": "aurora-postgresql", "status": "available",
     "writer": "mojo-prod-aurora-1", "readers": ["mojo-prod-aurora-2"],
     # Per-instance classes: a beefy writer with a deliberately smaller reader,
     # the shape the resize work targets.
     "writer_instance_class": "db.r6g.large",
     "reader_instance_classes": {"mojo-prod-aurora-2": "db.t4g.medium"},
     "reader_endpoint": "mojo-prod-aurora.cluster-ro-abc.us-east-1.rds.amazonaws.com",
     "endpoint": "mojo-prod-aurora.cluster-abc.us-east-1.rds.amazonaws.com"},
]

CACHES = [
    {"identifier": "mojo-prod-redis", "status": "available", "replica_count": 1,
     "cluster_enabled": False, "automatic_failover_on": True, "multi_az_on": True,
     # Node type is group-wide in ElastiCache, so it rides on the group and
     # every member shows it.
     "node_type": "cache.t4g.small",
     "members": [{"id": "mojo-prod-redis-001", "role": "primary"},
                 {"id": "mojo-prod-redis-002", "role": "replica"}],
     "min_replicas": 1, "blocked_reason": None},
]

DENIED_WARNING = {
    "code": "databases",
    "iam_action": "rds:DescribeDBClusters",
    "aws_code": "AccessDenied",
    "message": ("rds:DescribeDBClusters did not answer, so this section could "
                "not be read"),
}


# A node that was registered a moment ago and has not passed a health check
# yet — the honest shape of "an add just finished registering".
ADDING_NODE = {
    "id": "i-0a1b2c3d4e5f60044", "name": "mojo-api-b-clone", "state": "initial",
    "instance_state": "running", "instance_type": "m6i.large",
    "zone": "us-east-1b", "healthy": False, "self": False, "primary": False,
    "added_by_capacity": True, "groups": [GROUP_ARN],
}


# No describe(): the Capacity panel is published by the platform feature's
# capability mirror, so this provider serves pages and owns no bootstrap entry.
def reset(handler, fixtures, *, capacity_state="healthy", **options):
    handler.capacity_state = capacity_state
    handler.capacity_operations = {}
    if capacity_state == "external_mode":
        # The installation-wide mode, not a provider scenario — set here so the
        # bootstrap payload and the panel agree from the first render.
        handler.infrastructure_mode = "external"


def _nodes(state):
    if state == "single_node":
        return [dict(NODES[0])]
    if state == "adding":
        return [dict(row) for row in NODES] + [dict(ADDING_NODE)]
    return [dict(row) for row in NODES]


def _databases(state):
    if state == "denied":
        return []
    if state == "no_reader":
        return [dict(DATABASES[0], readers=[], reader_endpoint=None)]
    return [dict(row) for row in DATABASES]


def _actions(handler, state, nodes, databases):
    external = getattr(handler, "infrastructure_mode", "managed") == "external"
    healthy = [row for row in nodes if row["healthy"]]

    def offer(blocked):
        return {"offered": blocked is None, "blocked_reason": blocked}

    node_block = None
    if external:
        node_block = "infrastructure_external"
    elif state == "node_id_pinned":
        node_block = "node_id_pinned"
    elif not healthy:
        node_block = "no_source_node"
    remove_block = "infrastructure_external" if external else (
        "last_healthy_target" if len(healthy) <= 1 else None)
    return {
        "add_node": offer(node_block),
        "drain_node": offer(remove_block),
        "terminate_node": offer("infrastructure_external" if external else None),
        "add_reader": offer("infrastructure_external" if external else (
            None if databases else "no_database")),
        "remove_reader": offer("infrastructure_external" if external else (
            None if any(row["readers"] for row in databases) else "no_reader")),
        "set_cache_replicas": offer(
            "infrastructure_external" if external else None),
    }


def _report(handler):
    state = getattr(handler, "capacity_state", "healthy")
    nodes = _nodes(state)
    databases = _databases(state)
    external = getattr(handler, "infrastructure_mode", "managed") == "external"
    return 200, {
        "schema_version": 1,
        "region": "us-east-1",
        "mode": "external" if external else "managed",
        "generated_at": "2026-08-18T18:00:00Z",
        "node_id_pinned": state == "node_id_pinned",
        "nodes": {
            "balancers": [{"name": "mojo-api-nlb", "arn": BALANCER_ARN,
                           "type": "network", "state": "active"}],
            "groups": [{"arn": GROUP_ARN, "name": "mojo-api",
                        "target_type": "instance", "port": 443,
                        "protocol": "TCP"}],
            "instances": nodes,
            "self": nodes[0]["id"] if state != "denied" else None,
            # "denied" is the honest unavailable case: the portal could not
            # match itself to any instance, and says so rather than implying
            # the check passed.
            "self_check": "unavailable" if state == "denied" else "matched",
        },
        "databases": databases,
        "caches": [dict(row) for row in CACHES],
        "warnings": [dict(DENIED_WARNING)] if state == "denied" else [],
        "actions": _actions(handler, state, nodes, databases),
        # Reader routing is the serving process's self-report. "healthy" shows
        # both on so the Fleet page's chips and conditional copy render their
        # configured shape; every other state shows the off/unconfigured shape.
        "reader_routing": {
            "database": {
                "active": state == "healthy",
                "host": ("mojo-prod-aurora.cluster-ro-abc.us-east-1.rds"
                         ".amazonaws.com") if state == "healthy" else None,
                "skip_reason": None,
                "matches_reader_endpoint": True if state == "healthy" else None,
            },
            "redis": {"active": state == "healthy"},
        },
    }


def _operation(handler, parsed):
    query = parse_qs(parsed.query)
    operation_id = (query.get("operation") or [""])[0]
    record = getattr(handler, "capacity_operations", {}).get(operation_id)
    if record is None:
        return 404, {"status": False, "error": "That capacity operation is not "
                     "on record.", "error_code": "operation_not_found", "data": {}}
    phases = record["phases"]
    record["step"] = min(record["step"] + 1, len(phases))
    if record["step"] >= len(phases):
        record["state"] = "done"
        record["phase"] = "complete"
        record["message"] = record["done_message"]
    else:
        record["phase"] = phases[record["step"]]
    return 200, {key: record[key] for key in (
        "schema_version", "id", "action", "resource", "state", "phase",
        "phases", "message", "error_code", "warnings", "detail")}


ROUTES = {
    "/api/aws/capacity": lambda handler, parsed: _report(handler),
    "/api/aws/capacity/status": _operation,
}


def get(handler, parsed):
    route = ROUTES.get(parsed.path)
    return route(handler, parsed) if route else None


DONE_COPY = {
    "add_node": "i-0a1b2c3d4e5f60044 is serving as mojo-api-b-0a1b2c3d4e5f60044",
    "drain_node": "the node is drained and no longer serving traffic.",
    "terminate_node": "the node is terminated.",
    "add_reader": ("the reader is available. django-mojo does not read from a "
                   "reader endpoint today — wire it into DATABASES to use it."),
    "remove_reader": "the reader is deleted.",
    "set_cache_replicas": ("the group has been resized. A replica is failover "
                           "capacity, not read throughput."),
}


def post(handler, path, payload):
    if path != "/api/aws/capacity/apply":
        return None
    action = payload.get("action") or "add_node"
    phases = list(ADD_PHASES) if action == "add_node" else ["working"]
    operation_id = f"op-{len(getattr(handler, 'capacity_operations', {})) + 1}"
    record = {
        "schema_version": 1, "id": operation_id, "action": action,
        "resource": payload.get("resource") or "", "state": "running",
        "phase": phases[0], "phases": phases, "message": "requested",
        "error_code": None, "warnings": [], "detail": {},
        "step": 0, "done_message": DONE_COPY.get(action, "done."),
    }
    handler.capacity_operations[operation_id] = record
    return 200, {key: record[key] for key in (
        "schema_version", "id", "action", "resource", "state", "phase",
        "phases", "message", "error_code", "warnings", "detail")}
