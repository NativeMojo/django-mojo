"""Deterministic Capacity fixtures for the Admin visual preview.

Answers the capacity report, one operation's progress, the single apply, and
the batch plan/apply pair (a deterministic plan-1/batch-1 whose status
advances one step per poll). States are chosen so every branch the panel can
render is reachable without an AWS account: a healthy multi-node fleet with
controls, a single-node fleet where removal is refused, an add already in
flight through its phase ladder, a denied IAM read, a database with no reader,
a fleet that pins EDGE_NODE_ID, and an installation whose infrastructure is
external.

``transitioning`` is the "the provider is mid-change" report: a node EC2 never
registered, a node the balancer will not let go, a database reader AWS is
modifying and a cache replica being replaced — every per-member truth the two
portals gate their controls on, in one page.

The stable-outbound-address states are their own four: ``stable_ips_off``
(policy off, reservations kept), ``stable_ips_partial`` (on, one node still
without an address), ``egress_unknown`` (the policy read failed — unknown, not
off) and ``balancer_less`` (nothing registered behind a balancer, one box
holding its own address outside this portal). Every other state serves a
converged fleet, so the roster and the disable path are always reachable.
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

# Per-node capabilities are the SERVER's verdict, exactly as
# mojo/apps/aws/services/capacity.py ``_node_rows`` computes them:
# ``registered`` is "the balancer knows about it", ``lifecycle_state`` is the
# instance state normalized by ``_lifecycle`` (running → available), and
# ``can_drain`` is registered AND running AND not already leaving — which is
# why the "unused" node below is not drainable.
NODES = [
    {"id": "i-0a1b2c3d4e5f60011", "name": "mojo-api-a", "state": "healthy",
     "instance_state": "running", "lifecycle_state": "available",
     "instance_type": "m6i.large",
     "zone": "us-east-1a", "healthy": True, "registered": True,
     "can_drain": True, "self": True, "primary": True,
     "added_by_capacity": False, "groups": [GROUP_ARN]},
    {"id": "i-0a1b2c3d4e5f60022", "name": "mojo-api-b", "state": "healthy",
     "instance_state": "running", "lifecycle_state": "available",
     "instance_type": "m6i.large",
     "zone": "us-east-1b", "healthy": True, "registered": True,
     "can_drain": True, "self": False, "primary": False,
     "added_by_capacity": False, "groups": [GROUP_ARN]},
    {"id": "i-0a1b2c3d4e5f60033", "name": "mojo-api-c", "state": "unused",
     "instance_state": "running", "lifecycle_state": "available",
     "instance_type": "m6i.large",
     "zone": "us-east-1c", "healthy": False, "registered": True,
     "can_drain": False, "self": False, "primary": False,
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
     "reader_statuses": {"mojo-prod-aurora-2": "available"},
     # Per-member provider truth, the shape ``_database_rows`` emits: the raw
     # provider ``status`` beside the normalized ``lifecycle_state``, and the
     # two capability flags the panels gate their controls on. A writer is
     # never removable; a settled reader is.
     "members": [
         {"id": "mojo-prod-aurora-1", "role": "writer", "status": "available",
          "lifecycle_state": "available", "instance_class": "db.r6g.large",
          "can_resize": True, "can_remove": False},
         {"id": "mojo-prod-aurora-2", "role": "reader", "status": "available",
          "lifecycle_state": "available", "instance_class": "db.t4g.medium",
          "can_resize": True, "can_remove": True},
     ],
     "blocked_reason": None,
     "reader_endpoint": "mojo-prod-aurora.cluster-ro-abc.us-east-1.rds.amazonaws.com",
     "endpoint": "mojo-prod-aurora.cluster-abc.us-east-1.rds.amazonaws.com"},
]

CACHES = [
    {"identifier": "mojo-prod-redis", "status": "available", "replica_count": 1,
     "cluster_enabled": False, "automatic_failover_on": True, "multi_az_on": True,
     # Node type is group-wide in ElastiCache, so it rides on the group and
     # every member shows it. cache.t4g.small is deliberately NOT a curated
     # rung — it exercises the "Current — <type>" select shape.
     "node_type": "cache.t4g.small",
     # Failover + a replica: the server pre-states a rolling interruption.
     "resize_impact": "rolling",
     # ``_cache_rows`` stamps every member with the cluster's own status and
     # its normalized lifecycle — a group reads "available" while one of its
     # nodes is still being replaced, so the member is the only honest source.
     "members": [{"id": "mojo-prod-redis-001", "role": "primary",
                  "status": "available", "lifecycle_state": "available"},
                 {"id": "mojo-prod-redis-002", "role": "replica",
                  "status": "available", "lifecycle_state": "available"}],
     "min_replicas": 1, "blocked_reason": None},
]

# Mirrors the server's report["sizes"] — the curated resize allowlist from
# mojo/deploy/provision/spec.py CACHE_SIZES/DB_SIZES with COST_TABLE prices.
SIZES = {
    "cache": [
        {"size": "small", "label": "Small", "type": "cache.t4g.micro",
         "monthly_usd": 12.0},
        {"size": "medium", "label": "Medium", "type": "cache.t4g.medium",
         "monthly_usd": 50.0},
        {"size": "large", "label": "Large", "type": "cache.r7g.large",
         "monthly_usd": 120.0},
        {"size": "xlarge", "label": "Extra large", "type": "cache.r7g.xlarge",
         "monthly_usd": 240.0},
    ],
    "database": [
        {"size": "small", "label": "Small", "type": "db.t4g.medium",
         "monthly_usd": 50.0},
        {"size": "medium", "label": "Medium", "type": "db.r6g.large",
         "monthly_usd": 175.0},
        {"size": "large", "label": "Large", "type": "db.r6g.xlarge",
         "monthly_usd": 350.0},
        {"size": "xlarge", "label": "Extra large", "type": "db.r6g.2xlarge",
         "monthly_usd": 700.0},
    ],
}

# ── stable outbound addresses ───────────────────────────────────────────────
#
# One Elastic IP per fleet node, so the "give providers" line and the roster
# have something real to show. The addresses are deterministic per instance so
# a screenshot is stable across runs.
EIP_MONTHLY_USD = 3.6

EGRESS_IPS = {
    "i-0a1b2c3d4e5f60011": "52.10.0.11",
    "i-0a1b2c3d4e5f60022": "52.10.0.22",
    "i-0a1b2c3d4e5f60033": "52.10.0.33",
    "i-0a1b2c3d4e5f60044": "52.10.0.44",
}

# A balancer-less install: one box holding its own address, attached outside
# this portal. Read-only in the panel, and deliberately untagged.
FALLBACK_ATTACHED = [
    {"instance": "i-0f5e4d3c2b1a09988", "instance_name": "mojo-solo",
     "public_ip": "52.10.0.77", "allocation_id": "eipalloc-0solo0000000001",
     "managed": False},
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
    "instance_state": "running", "lifecycle_state": "available",
    "instance_type": "m6i.large",
    "zone": "us-east-1b", "healthy": False, "registered": True,
    "can_drain": True, "self": False, "primary": False,
    "added_by_capacity": True, "groups": [GROUP_ARN],
}


# A clone that never proved: EC2 knows it, the balancer does not. The server
# reports it because the fleet inventory covers unregistered members too —
# ``state`` falls back to "unregistered", the instance's own state is the only
# thing that describes it, and "stopped" normalizes to the "error" lifecycle.
UNREGISTERED_NODE = {
    "id": "i-0a1b2c3d4e5f60055", "name": "mojo-api-b-clone-2",
    "state": "unregistered", "instance_state": "stopped",
    "lifecycle_state": "error", "instance_type": "m6i.large",
    "zone": "us-east-1b", "healthy": False, "registered": False,
    "can_drain": False, "self": False, "primary": False,
    "added_by_capacity": True, "groups": [],
}


# No describe(): the Capacity panel is published by the platform feature's
# capability mirror, so this provider serves pages and owns no bootstrap entry.
def reset(handler, fixtures, *, capacity_state="healthy", **options):
    handler.capacity_state = capacity_state
    handler.capacity_operations = {}
    handler.capacity_plans = {}
    handler.capacity_batches = {}
    # A dict, not an attribute: reset() is handed the handler CLASS while an
    # apply is handed the request instance, so only a mutation of a shared
    # container survives the request that made it. None = "whatever this state
    # starts as"; an enable/disable apply pins it, so the report re-read after
    # the operation shows the new truth.
    handler.capacity_flags = {"egress_enabled": None}
    if capacity_state == "external_mode":
        # The installation-wide mode, not a provider scenario — set here so the
        # bootstrap payload and the panel agree from the first render.
        handler.infrastructure_mode = "external"


def _nodes(state):
    if state == "single_node":
        return [dict(NODES[0])]
    if state == "adding":
        return [dict(row) for row in NODES] + [dict(ADDING_NODE)]
    if state == "balancer_less":
        # Nothing registered behind a balancer — the fallback egress shape.
        return []
    if state == "transitioning":
        # mojo-api-c is the node the balancer will not let go (target state
        # "unused"), mojo-api-b-clone-2 the one it never registered.
        return [dict(row) for row in NODES] + [dict(UNREGISTERED_NODE)]
    return [dict(row) for row in NODES]


def _egress(handler, state, nodes):
    """The stable-outbound picture, in the server's envelope shape.

    Mirrors mojo/apps/aws/services/capacity.py ``_egress_envelope``: what is
    ATTACHED is ground truth, ``pending_nodes`` is every fleet node without an
    address (policy off or on), and a failed read is reported as unreadable
    rather than as "off".
    """
    if state == "egress_unknown":
        return {
            "enabled": False, "available": True,
            "fleet_available": True, "addresses_available": True,
            "policy_available": False,
            "addresses": [], "attached": [], "pending_nodes": [],
            "reserved": [], "to_allocate": 0,
            "monthly_usd_per_address": EIP_MONTHLY_USD,
            "fallback_attached": [],
        }
    enabled = getattr(handler, "capacity_flags", {}).get("egress_enabled")
    if enabled is None:
        enabled = state not in ("stable_ips_off", "balancer_less")
    # Registered nodes only: the server derives the allowlist from the
    # balancer's targets, so a node nothing registered is neither attached nor
    # pending — it is not in the fleet the toggle manages.
    ids = [row["id"] for row in nodes if row.get("registered", True)]
    if not enabled:
        holders = []
    elif state == "stable_ips_partial":
        # Half-converged: the first node holds its address, the rest do not.
        holders = ids[:1]
    else:
        holders = ids
    attached = [{"instance": node_id,
                 "public_ip": EGRESS_IPS.get(node_id, "52.10.0.1"),
                 "allocation_id": "eipalloc-0" + node_id[-15:],
                 "managed": True}
                for node_id in holders]
    pending = [node_id for node_id in ids if node_id not in holders]
    if enabled:
        # One spare reservation while converging, none once converged.
        reserved = ([{"allocation_id": "eipalloc-0reserved00001",
                      "public_ip": "52.10.0.90"}] if pending else [])
    else:
        # Disable keeps the reservations, so re-enabling restores the exact
        # same allowlisted addresses.
        reserved = [{"allocation_id": "eipalloc-0" + node_id[-15:],
                     "public_ip": EGRESS_IPS.get(node_id, "52.10.0.1")}
                    for node_id in ids]
    return {
        "enabled": enabled,
        "available": True,
        "fleet_available": True,
        "addresses_available": True,
        "policy_available": True,
        "addresses": sorted({row["public_ip"] for row in attached}),
        "attached": attached,
        "pending_nodes": pending,
        "reserved": reserved,
        "to_allocate": max(0, len(pending) - len(reserved)),
        "monthly_usd_per_address": EIP_MONTHLY_USD,
        "fallback_attached": ([dict(row) for row in FALLBACK_ATTACHED]
                              if state == "balancer_less" else []),
    }


def _members(row, role, **changes):
    """One member row updated in place-by-copy, the rest carried through."""
    members = []
    for member in row.get("members") or []:
        member = dict(member)
        if member.get("role") == role:
            member.update(changes)
        members.append(member)
    return members


def _databases(state):
    if state == "denied":
        return []
    if state == "no_reader":
        return [dict(DATABASES[0], readers=[], reader_endpoint=None,
                     reader_instance_classes={}, reader_statuses={},
                     members=[dict(member) for member in DATABASES[0]["members"]
                              if member["role"] == "writer"])]
    if state == "transitioning":
        # A reader AWS is still modifying: the raw status stays the provider's
        # word, ``_lifecycle`` normalizes "modifying" to "creating", both
        # capability flags go False, and the ROW is stamped transitioning
        # because one member is not available.
        return [dict(DATABASES[0],
                     reader_statuses={"mojo-prod-aurora-2": "modifying"},
                     members=_members(DATABASES[0], "reader",
                                      status="modifying",
                                      lifecycle_state="creating",
                                      can_resize=False, can_remove=False),
                     blocked_reason="resource_transitioning")]
    return [dict(row) for row in DATABASES]


# A second, settled group, only in the transitioning report. A row that
# carries its own blocked_reason renders THAT sentence and nothing else, so a
# blocked cache ACTION has no visible slot on a transitioning group — this
# unblocked one is where the family's sentence lands.
SETTLED_CACHE = {
    "identifier": "mojo-prod-redis-jobs", "status": "available",
    "replica_count": 0, "cluster_enabled": False,
    "automatic_failover_on": False, "multi_az_on": False,
    "node_type": "cache.t4g.micro",
    # No replica to roll onto: the server pre-states the downtime case.
    "resize_impact": "downtime",
    "members": [{"id": "mojo-prod-redis-jobs-001", "role": "primary",
                 "status": "available", "lifecycle_state": "available"}],
    "min_replicas": 0, "blocked_reason": None,
}


def _caches(state):
    if state == "transitioning":
        # The group still reports "available" while a replica is replaced —
        # the member is where the change shows, and the row's blocked_reason
        # follows from it.
        return [dict(CACHES[0],
                     members=_members(CACHES[0], "replica",
                                      status="modifying",
                                      lifecycle_state="creating"),
                     blocked_reason="resource_transitioning"),
                dict(SETTLED_CACHE)]
    return [dict(row) for row in CACHES]


def _egress_blocks(external, egress, nodes):
    """``(enable_block, disable_block)`` — the server's gate, reproduced.

    Order matters: an unknown fleet is not an empty fleet, and no failed read
    may render its answer as canonical.
    """
    def block():
        if external:
            return "infrastructure_external"
        if not egress.get("fleet_available"):
            return "fleet_unavailable"
        if not egress.get("addresses_available"):
            return "addresses_unavailable"
        if not egress.get("policy_available"):
            return "policy_unavailable"
        return None

    enable_block = block()
    if enable_block is None:
        if not nodes:
            enable_block = "no_fleet_nodes"
        elif egress.get("enabled") and not egress.get("pending_nodes"):
            enable_block = "already_enabled"
    disable_block = block()
    if disable_block is None:
        managed_attached = any(row.get("managed")
                               for row in egress.get("attached") or [])
        if not egress.get("enabled") and not managed_attached:
            disable_block = "not_enabled"
    return enable_block, disable_block


# What the transitioning report offers. Families keep the server's mapping —
# EC2 answers for the node actions, RDS for the database ones, ElastiCache for
# the cache ones — and each *_unavailable reason sits on the action whose
# control actually renders its sentence: add_node's stepper floor, the reader
# stepper's floor, and the cache panel's blocked line on the settled group (a
# row's own blocked_reason wins over the action's everywhere else, which is
# why the transitioning cache group cannot carry it).
#
# One deliberate liberty: the server reaches "*_unavailable" from a failed
# provider read, which would also empty that section. This state pairs them
# with live rows so ONE report exercises both the transition branches and the
# sentences the blocked families render.
#
# ``drain_node`` stays offered on purpose: the per-node ``can_drain`` verdict
# is then the only thing deciding which row gets a Remove, which is the branch
# this state exists to show.
TRANSITIONING_OFFERS = {
    "add_node": "instances_unavailable",
    "add_reader": "databases_unavailable",
    "remove_reader": "no_reader",
    "resize_database": "resource_transitioning",
    "set_cache_replicas": "caches_unavailable",
    "resize_cache": "caches_unavailable",
}


def _actions(handler, state, nodes, databases, egress):
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
    enable_block, disable_block = _egress_blocks(external, egress, nodes)
    offers = {
        "enable_stable_ips": offer(enable_block),
        "disable_stable_ips": offer(disable_block),
        "add_node": offer(node_block),
        "drain_node": offer(remove_block),
        "terminate_node": offer("infrastructure_external" if external else None),
        "add_reader": offer("infrastructure_external" if external else (
            None if databases else "no_database")),
        "remove_reader": offer("infrastructure_external" if external else (
            None if any(row["readers"] for row in databases) else "no_reader")),
        "set_cache_replicas": offer(
            "infrastructure_external" if external else None),
        "resize_cache": offer(
            "infrastructure_external" if external else None),
        "resize_database": offer("infrastructure_external" if external else (
            None if databases else "no_database")),
    }
    if state == "transitioning" and not external:
        offers.update({name: offer(reason)
                       for name, reason in TRANSITIONING_OFFERS.items()})
    return offers


def _report(handler):
    state = getattr(handler, "capacity_state", "healthy")
    nodes = _nodes(state)
    databases = _databases(state)
    external = getattr(handler, "infrastructure_mode", "managed") == "external"
    egress = _egress(handler, state, nodes)
    held = {row["instance"] for row in egress["attached"]}
    for row in nodes:
        row["stable_ip"] = row["id"] in held
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
            "self": nodes[0]["id"] if (nodes and state != "denied") else None,
            # "denied" is the honest unavailable case: the portal could not
            # match itself to any instance, and says so rather than implying
            # the check passed.
            "self_check": ("unavailable" if state in ("denied", "balancer_less")
                           else "matched"),
        },
        "egress": egress,
        "databases": databases,
        "caches": _caches(state),
        "sizes": {kind: [dict(row) for row in rows]
                  for kind, rows in SIZES.items()},
        "warnings": [dict(DENIED_WARNING)] if state == "denied" else [],
        "actions": _actions(handler, state, nodes, databases, egress),
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


def _batch_status(handler, batch_id):
    """One batch poll advances one step to done — the _operation idiom."""
    record = getattr(handler, "capacity_batches", {}).get(batch_id)
    if record is None:
        return 404, {"status": False, "error": "That capacity batch is not "
                     "on record.", "error_code": "batch_not_found", "data": {}}
    steps = record["steps"]
    active = next((step for step in steps if step["state"] == "running"), None)
    if active is not None:
        active["state"] = "done"
        active["phase"] = "complete"
        active["message"] = "done."
        following = next((step for step in steps
                          if step["state"] == "pending"), None)
        if following is not None:
            following["state"] = "running"
            following["phase"] = "working"
            following["message"] = "working"
            record["current_index"] = following["index"]
        else:
            record["state"] = "done"
            record["message"] = f"All {len(steps)} step(s) completed."
    return 200, {key: record[key] for key in (
        "schema_version", "id", "plan_id", "state", "current_index",
        "message", "error_code", "steps", "stalled")}


def _status(handler, parsed):
    query = parse_qs(parsed.query)
    batch_id = (query.get("batch") or [""])[0]
    if batch_id:
        return _batch_status(handler, batch_id)
    return _operation(handler, parsed)


ROUTES = {
    "/api/aws/capacity": lambda handler, parsed: _report(handler),
    "/api/aws/capacity/status": _status,
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
    "resize_cache": ("the group now runs the new node type. Replicas were "
                     "replaced first, then a brief failover swapped the "
                     "primary — one short interruption, not an outage."),
    "resize_database": ("the instance now runs the new class. The instance "
                        "restarted to change class — that was the few minutes "
                        "of downtime this action warned about."),
    "enable_stable_ips": ("every registered node holds its stable outbound "
                          "address. Give providers the addresses listed on "
                          "the panel."),
    "disable_stable_ips": ("the stable addresses are detached and kept "
                           "reserved — re-enabling restores the same ones."),
}

# The phase ladders the stable-outbound runners walk, matching the server's
# ACTION_PHASES for these two actions.
STABLE_IP_PHASES = {
    "enable_stable_ips": ["planning", "associating", "verifying"],
    "disable_stable_ips": ["detaching", "verifying"],
}


# Fixture prices consistent with the report's instance types. The cache
# group's current cache.t4g.small is deliberately unpriced, so a plan touching
# it exercises the honest-null path (monthly_delta_usd null + a warning +
# estimate_complete false) instead of a silent $0.
PLAN_PRICES = {
    "m6i.large": 70.0,
    "db.t4g.medium": 50.0, "db.r6g.large": 175.0,
    "db.r6g.xlarge": 350.0, "db.r6g.2xlarge": 700.0,
    "cache.t4g.micro": 12.0, "cache.t4g.medium": 50.0,
    "cache.r7g.large": 120.0, "cache.r7g.xlarge": 240.0,
}

PLAN_ORDER_NOTE = ("Steps run in the server's order: additions first, then "
                   "resizes, then removals — and a terminate runs "
                   "immediately after its own drain.")

_PLAN_RANK = {"add_node": 0, "add_reader": 1, "resize_cache": 3,
              "resize_database": 4, "remove_reader": 6, "drain_node": 7,
              "terminate_node": 8}


def _node_name(resource):
    for row in NODES + [ADDING_NODE]:
        if row["id"] == resource:
            return row["name"]
    return resource


def _rung_type(family, size):
    for row in SIZES[family]:
        if row["size"] == size:
            return row["type"]
    return str(size or "")


def _plan_step(step):
    action = step.get("action") or ""
    resource = step.get("resource") or ""
    kind = ("add" if action in ("add_node", "add_reader")
            else "change" if action.startswith("resize_") else "remove")
    delta, warnings = None, []
    if action == "add_node":
        description = "Add an app node"
        warnings = ["builds, deploys and proves itself before serving · "
                    "20–40 min"]
        delta = PLAN_PRICES["m6i.large"]
    elif action == "add_reader":
        description = f"Add a read replica to {resource}"
        warnings = ["can take up to an hour to come online"]
        delta = PLAN_PRICES["db.r6g.large"]
    elif action == "set_cache_replicas":
        count = step.get("count")
        current = CACHES[0]["replica_count"]
        description = f"Change {resource} replicas {current} → {count}"
        kind = "add" if (count or 0) > current else "remove"
        warnings = ["applies immediately — ElastiCache has no "
                    "maintenance-window option",
                    "no listed price for cache.t4g.small"]
    elif action == "resize_cache":
        description = f"Resize {resource} to {_rung_type('cache', step.get('size'))}"
        warnings = ["rolls replicas first, then a brief failover — one short "
                    "interruption",
                    "no listed price for cache.t4g.small"]
    elif action == "resize_database":
        to_type = _rung_type("database", step.get("size"))
        writer = DATABASES[0]["writer"]
        role = "writer" if resource == writer else "reader"
        from_type = (DATABASES[0]["writer_instance_class"] if role == "writer"
                     else DATABASES[0]["reader_instance_classes"].get(
                         resource, "db.t4g.medium"))
        description = f"Resize {role} {resource} to {to_type}"
        warnings = (["~minutes offline while the writer changes class"]
                    if role == "writer" else
                    ["reads keep flowing; this reader pauses while it "
                     "changes class"])
        if from_type in PLAN_PRICES and to_type in PLAN_PRICES:
            delta = PLAN_PRICES[to_type] - PLAN_PRICES[from_type]
        else:
            warnings = warnings + [f"no listed price for {from_type}"]
    elif action == "remove_reader":
        description = f"Remove read replica {resource}"
        warnings = ["deleted with no final snapshot"]
        delta = -PLAN_PRICES["db.t4g.medium"]
    elif action == "drain_node":
        description = f"Drain {_node_name(resource)} — traffic moves off it first"
        delta = 0.0
    elif action == "terminate_node":
        description = f"Terminate {_node_name(resource)}"
        delta = -PLAN_PRICES["m6i.large"]
    else:
        description = action or "unknown step"
    return {"action": action, "resource": resource,
            "params": {key: value for key, value in step.items()
                       if key not in ("action", "resource")},
            "kind": kind, "description": description, "warnings": warnings,
            "monthly_delta_usd": delta}


def _order_plan_steps(steps):
    def rank(step):
        if step["action"] == "set_cache_replicas":
            return 2 if step["kind"] == "add" else 5
        return _PLAN_RANK.get(step["action"], 9)

    ranked = sorted(steps, key=rank)
    drains = {step["resource"] for step in ranked
              if step["action"] == "drain_node"}
    paired = {step["resource"]: step for step in ranked
              if step["action"] == "terminate_node"
              and step["resource"] in drains}
    ordered = []
    for step in ranked:
        if (step["action"] == "terminate_node"
                and paired.get(step["resource"]) is step):
            continue
        ordered.append(step)
        if step["action"] == "drain_node" and step["resource"] in paired:
            ordered.append(paired[step["resource"]])
    return ordered


def _plan(handler, payload):
    steps = [_plan_step(step) for step in (payload.get("steps") or [])]
    ordered = _order_plan_steps(steps)
    for index, step in enumerate(ordered):
        step["index"] = index
    priced = [step["monthly_delta_usd"] for step in ordered
              if step["monthly_delta_usd"] is not None]
    plan_id = "plan-1"
    record = {
        "schema_version": 1, "id": plan_id,
        "created": "2026-08-18T18:00:00Z", "expires_in": 300,
        "expires_at": "2026-08-18T18:05:00Z", "actor": 1,
        "steps": ordered,
        "total_monthly_delta_usd": round(sum(priced), 2),
        "estimate_complete": len(priced) == len(ordered),
        "order_note": PLAN_ORDER_NOTE,
    }
    handler.capacity_plans[plan_id] = record
    return 200, record


def _plan_apply(handler, payload):
    plan = getattr(handler, "capacity_plans", {}).get(payload.get("plan_id"))
    if plan is None:
        return 404, {"status": False,
                     "error": "That plan is not on record — plans expire "
                              "after 5 minutes. Request a new plan and review "
                              "it again.",
                     "error_code": "plan_not_found", "data": {}}
    steps = [{"index": step["index"], "action": step["action"],
              "resource": step["resource"], "params": step["params"],
              "description": step["description"], "kind": step["kind"],
              "state": "pending", "operation": None, "phase": None,
              "message": None, "error_code": None}
             for step in plan["steps"]]
    if steps:
        steps[0]["state"] = "running"
        steps[0]["phase"] = "working"
        steps[0]["message"] = "requested"
    record = {
        "schema_version": 1, "id": "batch-1", "plan_id": plan["id"],
        "state": "running", "current_index": 0, "message": "requested",
        "error_code": None, "steps": steps, "stalled": False,
    }
    handler.capacity_batches[record["id"]] = record
    return 200, record


def post(handler, path, payload):
    if path == "/api/aws/capacity/plan":
        return _plan(handler, payload)
    if path == "/api/aws/capacity/plan/apply":
        return _plan_apply(handler, payload)
    if path != "/api/aws/capacity/apply":
        return None
    action = payload.get("action") or "add_node"
    if action == "add_node":
        phases = list(ADD_PHASES)
    else:
        phases = list(STABLE_IP_PHASES.get(action, ["working"]))
    if action in STABLE_IP_PHASES:
        # The switch has flipped; the report re-read after the operation
        # finishes shows the new roster.
        getattr(handler, "capacity_flags", {})["egress_enabled"] = (
            action == "enable_stable_ips")
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
