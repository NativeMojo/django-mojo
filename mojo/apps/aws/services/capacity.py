"""Admin capacity actions: add/remove an app node, an RDS reader, a cache
replica — and resize the cache group or a database instance to a curated size.

The alternative to this module is an operator in the AWS console at 2am, adding
a node by hand and hoping they picked the same instance type, the same subnet,
the same security groups and the same instance profile — with no record of who
did it and no proof the new box is running the code the rest of the fleet runs.

Three things make that safe enough to put behind a button:

**Every guard is re-derived from the provider, at apply time.** Not from the
dashboard cache, not from the request body. The last-healthy-target check
re-describes EVERY attached group of EVERY balancer; the reader delete
re-reads whether the target really is a replica; ElastiCache's failover posture
is read before its replica count is touched. A ten-minute-old report is a fine
thing to render and a terrible thing to mutate against.

**An added node is not "launched", it is PROVEN.** The clone boots, takes a
unique hostname derived from its own instance id, joins the job fleet as its
own runner, is told to deploy the fleet's last CONVERGED commit, and is
registered behind the balancer only after it reports back that it is running
that exact sha. A node that never proves is left unregistered — costing money
and serving nothing — because the failure mode on the other side is serving
production traffic from a box running unknown code.

**Nothing here is reversible by accident.** Draining and terminating are
SEPARATE actions, and terminate re-proves the drain server-side; a node cannot
be removed if it is the last healthy target anywhere, or if it is the node
answering the request.

The hostname is the identity, everywhere. The jobs runner id is
``<hostname>-engine``; the readiness node id is the hostname; the certbot
primary is elected by comparing the hostname to ``PRIMARY_BALANCER_HOST``. That
is why the clone's user-data sets a hostname derived from its instance id
before anything else — a clone that kept the source's hostname would be a
second engine claiming the source's runner id, and the fleet would have two
nodes answering as one.

Progress lives in the cache, not in a table: an operation is a bounded
observation of AWS state, and losing the observation loses nothing that
``report()`` cannot re-derive from the provider. Losing a MUTATION is what
would matter, and every mutation is AWS-side before the record is written.
"""

import hashlib
import json
import time
import uuid

from django.core.cache import cache

from mojo.deploy.provision import spec as provision_spec
from mojo.helpers import infrastructure
from mojo.helpers import logit
from mojo.helpers.aws import ec2 as ec2_helper
from mojo.helpers.aws import elasticache as elasticache_helper
from mojo.helpers.aws import elbv2 as elbv2_helper
from mojo.helpers.aws import rds as rds_helper
from mojo.helpers.aws.provider_call import ProviderCallError
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_capacity", "aws.log")

SCHEMA_VERSION = 1
CACHE_PREFIX = "mojo:aws:capacity"
REPORT_TTL = 120
# 90 minutes: an add that captures a fresh AMI, boots, converges and proves can
# legitimately take the better part of an hour, and a claim that expires under
# a running operation would let a second add start on top of it.
CLAIM_TTL = 5400

ACTION_ADD_NODE = "add_node"
ACTION_DRAIN_NODE = "drain_node"
ACTION_TERMINATE_NODE = "terminate_node"
ACTION_ADD_READER = "add_reader"
ACTION_REMOVE_READER = "remove_reader"
ACTION_SET_CACHE_REPLICAS = "set_cache_replicas"
ACTION_ENABLE_STABLE_IPS = "enable_stable_ips"
ACTION_DISABLE_STABLE_IPS = "disable_stable_ips"
ACTION_RESIZE_CACHE = "resize_cache"
ACTION_RESIZE_DATABASE = "resize_database"
ACTIONS = (ACTION_ADD_NODE, ACTION_DRAIN_NODE, ACTION_TERMINATE_NODE,
           ACTION_ADD_READER, ACTION_REMOVE_READER, ACTION_SET_CACHE_REPLICAS,
           ACTION_ENABLE_STABLE_IPS, ACTION_DISABLE_STABLE_IPS,
           ACTION_RESIZE_CACHE, ACTION_RESIZE_DATABASE)

# Adds serialize on ONE fixed key, never on the resource: two concurrent adds
# name different resources (or none at all), so a per-resource key would let
# them both through and race on the same image, the same topology write, and
# the same convergence. The stable-ips pair shares a second fixed key for the
# same reason — an enable interleaved with a disable is two hands on one
# switch. Everything else is genuinely per-resource.
ADD_NODE_CLAIM = "add_node:fleet"
STABLE_IPS_CLAIM = "stable_ips:fleet"

# The durable policy: one protected system setting (superuser-only writer,
# stored in the DB, readable from API and job processes on any node), shape
# {"enabled": bool}. Registered with its validator in mojo.apps.aws.apps.
STABLE_EGRESS_SETTING = "AWS_STABLE_OUTBOUND_IPS"

# The ownership tag for addresses this feature manages. Mutation is gated on
# tags, reporting on associations: an EIP is created, renamed, or detached here
# ONLY if it wears this tag — an untagged address is reported, never touched.
STABLE_EIP_TAG = "mojo:eip"
STABLE_EIP_TAG_VALUE = "stable-egress"

# What one public IPv4 costs a month. Keep in agreement with
# mojo/deploy/provision/spec.py COST_TABLE["eip"]. AWS bills every public IPv4
# identically, attached or not — so an attached EIP REPLACES the node's
# auto-assigned-address charge (enable is net ~zero), and the additive cost
# appears at disable, when a kept reservation bills beside the node's new
# auto-assigned address.
EIP_MONTHLY_USD = 3.6

PHASES = {
    ACTION_ADD_NODE: ("capturing", "launching", "booting", "converging",
                      "proving", "addressing", "registering", "settling"),
    ACTION_DRAIN_NODE: ("draining",),
    ACTION_TERMINATE_NODE: ("terminating",),
    ACTION_ADD_READER: ("creating", "settling"),
    ACTION_REMOVE_READER: ("deleting",),
    ACTION_SET_CACHE_REPLICAS: ("scaling", "settling"),
    ACTION_ENABLE_STABLE_IPS: ("planning", "associating", "verifying"),
    ACTION_DISABLE_STABLE_IPS: ("detaching", "verifying"),
    ACTION_RESIZE_CACHE: ("resizing", "settling"),
    ACTION_RESIZE_DATABASE: ("resizing", "settling"),
}

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

# Bounds. Each is a deadline, never a sleep: the operation job polls on
# POLL_INTERVAL and gives up at the deadline with a named error code.
POLL_INTERVAL = 10
IMAGE_TIMEOUT = 1800        # AMI pending -> nothing was launched
LAUNCH_TIMEOUT = 600        # instance pending -> it exists; say so
RUNNER_TIMEOUT = 1200       # no runner on `edge` -> left unregistered, safe
PROOF_MARGIN = 300          # on top of canary_timeout()
HEALTH_TIMEOUT = 900        # registered but never healthy -> offer Drain
DRAIN_MARGIN = 300          # on top of the group's deregistration_delay
RDS_TIMEOUT = 3600          # a reader can genuinely take this long
CACHE_TIMEOUT = 1800
# A node-type change replaces EVERY node in the group sequentially (rolling),
# so a 1+2 group is three replacements. 3600 would sit exactly at the portal's
# old one-hour follow cap and double-fail slow-but-succeeding resizes.
CACHE_RESIZE_TIMEOUT = 5400

DEFAULT_IMAGE_MAX_AGE_DAYS = 14
DEFAULT_NODE_ROOT = "/opt/api"
IMAGE_TAG_VALUE = "admin-capacity"
RDS_SETTLED = "available"


class CapacityError(Exception):
    """A wire-safe refusal or provider failure, with the status it should get."""

    def __init__(self, message, error_code, status=400, data=None):
        self.message = str(message)
        self.error_code = str(error_code)
        self.status = int(status)
        self.data = dict(data or {})
        super().__init__(self.message)


def _provider_error(err, message):
    """The single translation from a provider failure to a wire answer."""
    if err.denied:
        status, code = 403, "provider_denied"
    elif err.retryable:
        status, code = 503, "provider_unavailable"
    else:
        status, code = 502, "provider_error"
    return CapacityError(message, code, status, {"failure": err.detail()})


def _setting(name, default=None):
    try:
        return settings.get_static(name, default)
    except Exception:
        return default


def _region():
    return _setting("AWS_REGION", "us-east-1")


def _image_max_age_days():
    try:
        return max(0, int(_setting("ADMIN_CAPACITY_IMAGE_MAX_AGE_DAYS",
                                   DEFAULT_IMAGE_MAX_AGE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_MAX_AGE_DAYS


def _node_root():
    return str(_setting("ADMIN_CAPACITY_NODE_ROOT", DEFAULT_NODE_ROOT) or
               DEFAULT_NODE_ROOT)


def _now():
    from mojo.helpers import dates
    return dates.utcnow().isoformat()


# ── cache keys ──────────────────────────────────────────────────────────────

def _report_key():
    return f"{CACHE_PREFIX}:report:{_region()}"


def _claim_key(action, resource):
    if action == ACTION_ADD_NODE:
        return f"{CACHE_PREFIX}:claim:{ADD_NODE_CLAIM}"
    if action in (ACTION_ENABLE_STABLE_IPS, ACTION_DISABLE_STABLE_IPS):
        return f"{CACHE_PREFIX}:claim:{STABLE_IPS_CLAIM}"
    if action == ACTION_RESIZE_CACHE:
        # ONE claim per replication group, shared with set_cache_replicas: a
        # resize and a replica-count change mutate the same AWS object and must
        # serialize. The deployed key literal is reused (rather than renaming
        # both) so a claim held across a deploy stays honored.
        return f"{CACHE_PREFIX}:claim:{ACTION_SET_CACHE_REPLICAS}:{resource}"
    return f"{CACHE_PREFIX}:claim:{action}:{resource}"


def _operation_key(operation_id):
    return f"{CACHE_PREFIX}:op:{operation_id}"


def invalidate():
    """Drop every cached view a mutation just made wrong.

    The dashboard's balancer cache is invalidated too: after a register or a
    deregister the Dashboard's EC2 and load-balancer rows are stale in the one
    direction that matters, and a 60-second lie about serving-tier membership
    is exactly what an operator is watching for.
    """
    from mojo.apps.account.services.admin_platform import LOAD_BALANCER_CACHE_KEY
    for key in (_report_key(), LOAD_BALANCER_CACHE_KEY):
        try:
            cache.delete(key)
        except Exception:
            logger.warning("capacity: cache key %s could not be invalidated", key)


# ── single flight ───────────────────────────────────────────────────────────

def _claim(action, resource, actor_pk):
    """Single-flight. A cache that cannot answer is a refusal, never a go-ahead."""
    key = _claim_key(action, resource)
    try:
        acquired = cache.add(key, {"actor": actor_pk, "at": _now()}, CLAIM_TTL)
    except Exception:
        raise CapacityError(
            "The coordination cache is unavailable, so a second concurrent "
            "capacity change cannot be ruled out. Try again shortly.",
            "cache_unavailable", 503) from None
    if acquired:
        return key
    # `add` returning False means either "somebody holds it" or "the backend
    # silently did nothing". Reading the key back tells those apart, and they
    # get different answers.
    try:
        holder = cache.get(key)
    except Exception:
        holder = None
    if holder:
        raise CapacityError(
            "A capacity change for this resource is already in progress.",
            "capacity_in_progress", 409)
    raise CapacityError(
        "The coordination cache is unavailable, so a second concurrent "
        "capacity change cannot be ruled out. Try again shortly.",
        "cache_unavailable", 503)


def _release(key):
    try:
        cache.delete(key)
    except Exception:
        logger.warning("capacity: claim %s could not be released", key)


# ── operation record ────────────────────────────────────────────────────────

def _write_operation(record):
    record["updated"] = _now()
    try:
        cache.set(_operation_key(record["id"]), record, CLAIM_TTL)
    except Exception:
        logger.warning("capacity: operation %s could not be recorded",
                       record.get("id"))
    return record


def _read_operation(operation_id):
    try:
        record = cache.get(_operation_key(operation_id))
    except Exception:
        record = None
    return record if isinstance(record, dict) else None


def _new_operation(action, resource, actor, claim, detail=None):
    phases = PHASES.get(action) or ()
    return _write_operation({
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "action": action,
        "resource": resource,
        "actor": getattr(actor, "pk", None),
        "started": _now(),
        "state": STATE_RUNNING,
        "phase": phases[0] if phases else "working",
        "phases": list(phases),
        "message": "requested",
        "error_code": None,
        "warnings": [],
        "detail": dict(detail or {}),
        "claim": claim,
    })


def _advance(record, phase, message, **detail):
    record["phase"] = phase
    record["message"] = message
    if detail:
        record["detail"].update(detail)
    return _write_operation(record)


def _finish(record, message, **detail):
    record["state"] = STATE_DONE
    record["phase"] = "complete"
    record["message"] = message
    if detail:
        record["detail"].update(detail)
    _release(record.get("claim"))
    invalidate()
    return _write_operation(record)


def _fail(record, error_code, message, hold_claim=False, **detail):
    record["state"] = STATE_FAILED
    record["error_code"] = str(error_code)
    record["message"] = message
    if detail:
        record["detail"].update(detail)
    if not hold_claim:
        _release(record.get("claim"))
    invalidate()
    logger.warning("capacity operation %s failed action=%s code=%s",
                   record.get("id"), record.get("action"), error_code)
    return _write_operation(record)


def _warn(record, code, message):
    warnings = list(record.get("warnings") or [])
    warnings.append({"code": str(code), "message": str(message)})
    record["warnings"] = warnings[-8:]
    return _write_operation(record)


# ── guards ──────────────────────────────────────────────────────────────────

def _primary_host():
    """The certbot primary's hostname, lowercased, or ''.

    Only a preference: an add clones a NON-primary node when one is available,
    because capturing an image of the primary is one more thing happening to
    the node that renews the fleet's certificates. It is never a blocker — the
    clone gets its own hostname, so it is never elected primary either way.
    """
    return str(_setting("PRIMARY_BALANCER_HOST", "") or "").strip().lower()


def _local_hostname():
    import socket
    return socket.gethostname().split(".", 1)[0].strip().lower()


def _is_primary(facts, primary):
    if not primary:
        return False
    candidates = {str(facts.get("private_hostname") or "").lower(),
                  str(facts.get("name") or "").lower()}
    return primary in candidates


def _source_node(healthy_ids, client=None, region=None):
    """The instance to clone: a healthy, running fleet member, non-primary first.

    Only healthy node is the primary? Clone it anyway — a NoReboot image does
    not interrupt it, and the clone takes its own hostname, so the primary
    election is unaffected.
    """
    ids = [value for value in (healthy_ids or []) if str(value).startswith("i-")]
    if not ids:
        return None
    primary = _primary_host()
    facts = ec2_helper.instance_map(ids, client=client, region=region)
    running = [value for value in ids
               if (facts.get(value) or {}).get("state") == "running"]
    ordered = [value for value in running if not _is_primary(facts[value], primary)]
    ordered += [value for value in running if value not in ordered]
    for value in ordered:
        return facts[value]
    return None


def _serving(client=None, region=None):
    """Every attached target group of every balancer, freshly described."""
    return elbv2_helper.serving_map(client=client, region=region)


def _groups_holding(serving, instance_id):
    return [group for group in (serving.get("groups") or [])
            if any(target.get("id") == instance_id
                   for target in group.get("targets") or [])]


def _would_strand(instance_id, serving):
    """Would removing ``instance_id`` leave a group with no healthy target?

    Checked across EVERY attached group of EVERY balancer, not just the one
    the dashboard happens to render. A node that is the last healthy target of
    an internal group nobody looks at is still the thing keeping that group up.
    """
    for group in serving.get("groups") or []:
        healthy = [target.get("id") for target in group.get("targets") or []
                   if target.get("state") == "healthy"]
        if instance_id in healthy and len(healthy) <= 1:
            return group
    return None


def _self_id(facts_map):
    """``(instance_id_or_None, status)`` for "which of these is me?".

    This box's short hostname against each instance's PrivateDnsName first
    label AND its Name tag. A fleet that sets its own hostnames matches on the
    tag; a fleet on AWS-assigned names matches on the DNS label. When neither
    matches, the answer is ``"unavailable"`` — NOT "this is not me". Absent
    evidence has never been proof of safety, and the confirmation copy says so
    out loud rather than implying the check passed.
    """
    local = _local_hostname()
    if not local:
        return None, "unavailable"
    for identifier, row in (facts_map or {}).items():
        candidates = {str(row.get("private_hostname") or "").lower(),
                      str(row.get("name") or "").lower()}
        if local in candidates:
            return identifier, "matched"
    return None, "unavailable"


def _self_check(serving_instance_ids, client=None, region=None):
    """``_self_id`` over ONE describe_instances. The apply-path form."""
    return _self_id(ec2_helper.instance_map(
        serving_instance_ids, client=client, region=region))


def _node_id_pinned():
    """True when this fleet pins EDGE_NODE_ID in its settings file.

    A pinned node id means every node reports the SAME readiness identity, so
    an added node is indistinguishable from the one it was cloned from. There
    is no safe way to prove a new node under that configuration, so the add is
    refused rather than half-done.
    """
    return bool(str(_setting("EDGE_NODE_ID", "") or "").strip())


# ── stable outbound addresses ───────────────────────────────────────────────

def _egress_enabled(strict=False):
    """Is the stable-outbound-ips policy on? Absent or malformed reads False.

    ``strict=True`` lets a broken read RAISE instead. The report may treat
    "unreadable" as "off" — it is rendering, not acting — but add_node's
    addressing leg is an admission gate: skipping it because the database
    blinked would register a node whose egress no provider has allowlisted.
    (``Setting.get_from_db`` swallows every exception into not-found, which is
    why the strict path queries the row itself.)
    """
    if strict:
        from mojo.apps.account.models import Setting
        row = Setting.objects.filter(
            key=STABLE_EGRESS_SETTING, group=None).first()
        if row is None:
            return False
        value = row.get_value()
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else None
        return isinstance(value, dict) and value.get("enabled") is True
    from mojo.apps.account.services import system_settings
    try:
        value = system_settings.get_value(STABLE_EGRESS_SETTING, None)
    except Exception:
        return False
    return isinstance(value, dict) and value.get("enabled") is True


def _egress_policy():
    """``(enabled, available)`` — the report's form of the policy read.

    The AWS legs publish ``fleet_available``/``addresses_available`` so a
    failed read renders as unknown; the policy read gets the same honesty. A
    DB blip must not paint "off" beside attached addresses as if it were a
    canonical answer.
    """
    try:
        return _egress_enabled(strict=True), True
    except Exception:
        logger.warning("capacity: stable-ips policy could not be read")
        return False, False


def _stable_tagged(tags):
    """Does this feature manage the address? Mutation is gated on this tag."""
    return (tags or {}).get(STABLE_EIP_TAG) == STABLE_EIP_TAG_VALUE


def _django_mojo_tagged(tags):
    """django-mojo ownership by tag — the adoption and assign-eligibility gate.

    Matches what provision writes on every resource it creates
    (spec.MANAGED_BY / TAGS). An address carrying neither tag is foreign:
    reported, never consumed, never renamed, never detached.
    """
    tags = tags or {}
    return tags.get("managed-by") == "django-mojo" or bool(tags.get("mojo:project"))


def _fleet_ids(serving):
    """Every registered instance across every group, deduped, first-seen order."""
    seen = []
    for group in serving.get("groups") or []:
        for target in group.get("targets") or []:
            identifier = target.get("id")
            if (identifier and str(identifier).startswith("i-")
                    and identifier not in seen):
                seen.append(identifier)
    return seen


def _address_view(fleet_ids, addresses):
    """One describe_addresses read projected onto one fleet.

    ``unassociated`` requires no association, no instance AND no network
    interface — an address held by an NLB or a NAT gateway has no instance id
    but is very much in use, and treating it as reusable would rip an address
    out of the serving path.
    """
    by_instance = {}
    attached = []
    unassociated = []
    for row in addresses or []:
        instance = row.get("instance_id")
        if instance and instance in fleet_ids:
            by_instance[instance] = row
            attached.append({
                "instance": instance,
                "public_ip": row.get("public_ip"),
                "allocation_id": row.get("allocation_id"),
                "managed": _stable_tagged(row.get("tags")),
            })
        elif (not row.get("association_id") and not instance
                and not row.get("network_interface_id")):
            unassociated.append(row)
    reserved = [{"allocation_id": row.get("allocation_id"),
                 "public_ip": row.get("public_ip")}
                for row in unassociated if _stable_tagged(row.get("tags"))]
    pending = [identifier for identifier in fleet_ids
               if identifier not in by_instance]
    return {"by_instance": by_instance, "attached": attached,
            "unassociated": unassociated, "reserved": reserved,
            "pending": pending}


# ── report ──────────────────────────────────────────────────────────────────

def _node_rows(serving, facts_map, self_id, primary):
    """One row per registered instance, with everything an operator decides on.

    ``primary`` is shown, never acted on: the certbot primary is elected by
    hostname, so it is worth seeing which node renews the fleet's certificates
    before choosing one to drain — but it is not a refusal, and an added clone
    never becomes primary by accident because it takes its own hostname.
    """
    rows = []
    seen = set()
    for group in serving.get("groups") or []:
        for target in group.get("targets") or []:
            identifier = target.get("id")
            if not identifier or not str(identifier).startswith("i-"):
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            facts = (facts_map or {}).get(identifier) or {}
            rows.append({
                "id": identifier,
                "name": facts.get("name") or identifier,
                "state": target.get("state"),
                "instance_state": facts.get("state"),
                "instance_type": facts.get("instance_type"),
                "zone": facts.get("availability_zone"),
                "public_ip": facts.get("public_ip"),
                "healthy": target.get("state") == "healthy",
                "self": identifier == self_id,
                "primary": _is_primary(facts, primary),
                "added_by_capacity": (facts.get("tags") or {}).get(
                    ec2_helper.CREATED_BY_TAG) == "admin-capacity",
                "groups": [],
            })
    by_id = {row["id"]: row for row in rows}
    for group in serving.get("groups") or []:
        for target in group.get("targets") or []:
            row = by_id.get(target.get("id"))
            if row is not None and group.get("arn") not in row["groups"]:
                row["groups"].append(group.get("arn"))
    return rows


def _database_rows(rds_client=None, region=None):
    """Every Aurora cluster and standalone instance, with its reader picture.

    django-mojo does NOT consume a reader endpoint today: DATABASES points at
    one host and every query goes there. A reader added here is standby read
    capacity plus an endpoint string for the project to wire in, and the UI is
    required to say exactly that rather than imply the app got faster.
    """
    rows = []
    clusters = rds_helper.cluster_statuses(client=rds_client, region=region)
    instances = rds_helper.instance_statuses(client=rds_client, region=region)
    for identifier in sorted(clusters):
        detail = rds_helper.cluster_members(
            identifier, client=rds_client, region=region) or {}
        rows.append({
            "identifier": identifier,
            "kind": "aurora",
            "engine": detail.get("engine"),
            "status": clusters[identifier].get("status"),
            "writer": detail.get("writer"),
            "readers": list(detail.get("readers") or []),
            # Per-instance classes out of the ONE describe `instances` already
            # holds — the writer and each reader carry independent sizes, and
            # that asymmetry (big writer, smaller readers) is the point.
            "writer_instance_class": (
                instances.get(detail.get("writer")) or {}).get("instance_class"),
            "reader_instance_classes": {
                reader: (instances.get(reader) or {}).get("instance_class")
                for reader in detail.get("readers") or []},
            "reader_endpoint": detail.get("reader_endpoint"),
            "endpoint": detail.get("endpoint"),
        })
    members = {row["identifier"] for row in rows}
    for identifier in sorted(instances):
        detail = rds_helper.instance_role(
            identifier, client=rds_client, region=region) or {}
        if detail.get("cluster"):
            continue
        if identifier in members:
            continue
        rows.append({
            "identifier": identifier,
            "kind": "standalone",
            "engine": detail.get("engine"),
            "status": instances[identifier].get("status"),
            "writer": None if detail.get("is_replica") else identifier,
            "replica_of": detail.get("replica_source"),
            "is_replica": bool(detail.get("is_replica")),
            "instance_class": detail.get("instance_class"),
            "readers": [],
            "reader_instance_classes": {},
            "endpoint": detail.get("endpoint"),
        })
    # A standalone replica belongs to its source's row, not its own.
    sources = {row["identifier"]: row for row in rows if row["kind"] == "standalone"}
    for row in list(rows):
        source = sources.get(row.get("replica_of"))
        if source is not None:
            source["readers"].append(row["identifier"])
            source["reader_instance_classes"][row["identifier"]] = \
                row.get("instance_class")
    return [row for row in rows if not row.get("is_replica")]


def _cache_rows(cache_client=None, region=None):
    """Every replication group, with the facts the replica floor depends on."""
    rows = []
    for facts in elasticache_helper.replication_groups(
            client=cache_client, region=region):
        blocked = None
        if facts.get("cluster_enabled"):
            blocked = elasticache_helper.CLUSTER_MODE_UNSUPPORTED
        rows.append({
            "identifier": facts.get("identifier"),
            "status": facts.get("status"),
            "replica_count": facts.get("replica_count"),
            "cluster_enabled": facts.get("cluster_enabled"),
            "automatic_failover_on": facts.get("automatic_failover_on"),
            "multi_az_on": facts.get("multi_az_on"),
            "node_type": facts.get("node_type"),
            # Which interruption a node-type change means for THIS group,
            # stated before apply: failover with a replica rolls (replicas
            # first, then a brief failover); without one the primary is down
            # for the duration.
            "resize_impact": ("rolling"
                              if (facts.get("automatic_failover_on")
                                  and int(facts.get("replica_count") or 0) >= 1)
                              else "downtime"),
            "members": facts.get("members"),
            # The lowest count this group may be moved to. One, not zero, when
            # anything would fail over onto a replica that would no longer be
            # there.
            "min_replicas": 1 if (facts.get("automatic_failover_on")
                                  or facts.get("multi_az_on")) else 0,
            "blocked_reason": blocked,
        })
    return rows


def _collect(envelope, code, iam_action, loader):
    """Run one bounded read, degrading to a named warning instead of a 500."""
    try:
        return loader()
    except ProviderCallError as err:
        detail = err.detail()
        envelope["warnings"].append({
            "code": code,
            "iam_action": detail.get("iam_action") or iam_action,
            "aws_code": detail.get("provider_code"),
            "message": (f"{iam_action} did not answer, so this section could "
                        f"not be read"),
        })
        logger.warning("capacity report read failed %s %s", code, detail)
        return None
    except Exception:
        envelope["warnings"].append({
            "code": code, "iam_action": iam_action, "aws_code": None,
            "message": f"{iam_action} did not answer, so this section could "
                       f"not be read"})
        logger.exception("capacity report read failed %s", code)
        return None


# ── curated sizes ───────────────────────────────────────────────────────────

def _size_catalog():
    """The resize allowlist, with the price beside each rung.

    The ladders live in provision's ``spec.py`` beside COST_TABLE so ONE file
    answers "what sizes exist and what do they cost". The panel renders
    exactly this and hardcodes nothing.
    """
    def rows(ladder):
        return [{"size": key, "label": label, "type": itype,
                 "monthly_usd": provision_spec.COST_TABLE.get(itype)}
                for key, label, itype in ladder]
    return {"cache": rows(provision_spec.CACHE_SIZES),
            "database": rows(provision_spec.DB_SIZES)}


def _resolve_size(ladder, size):
    """A curated size KEY to its instance type — anything else is refused.

    Refused before any provider call, and a raw type string is therefore
    never accepted: the ladder can be re-pointed at new instance families
    without breaking callers, and a client cannot smuggle a type the
    allowlist does not carry.
    """
    wanted = str(size or "").strip().lower()
    for key, _label, itype in ladder:
        if key == wanted:
            return itype
    raise CapacityError(
        "size must be one of: small, medium, large, xlarge", "invalid_request")


def _ladder_index(ladder, itype):
    """Position of ``itype`` on the ladder, or None when it is not curated.

    None means the direction of a resize is unknown — the completion note
    softens its memory caution instead of pretending to know.
    """
    for index, (_key, _label, rung_type) in enumerate(ladder):
        if rung_type == itype:
            return index
    return None


def _offers(envelope):
    """Per-action ``offered``/``blocked_reason``, computed once, server-side.

    The panel renders these; it never derives them. A control the server would
    refuse must not be a button the operator can press.
    """
    external = envelope.get("mode") == infrastructure.EXTERNAL
    nodes = envelope.get("nodes") or {}
    instances = nodes.get("instances") or []
    healthy = [row for row in instances if row.get("healthy")]
    offers = {}

    def offer(name, blocked):
        offers[name] = {"offered": blocked is None, "blocked_reason": blocked}

    node_block = None
    if external:
        node_block = infrastructure.ERROR_CODE
    elif envelope.get("node_id_pinned"):
        node_block = "node_id_pinned"
    elif not healthy:
        node_block = "no_source_node"
    offer(ACTION_ADD_NODE, node_block)

    remove_block = infrastructure.ERROR_CODE if external else (
        "last_healthy_target" if len(healthy) <= 1 else None)
    offer(ACTION_DRAIN_NODE, remove_block)
    offer(ACTION_TERMINATE_NODE, infrastructure.ERROR_CODE if external else None)

    database_block = infrastructure.ERROR_CODE if external else (
        None if envelope.get("databases") else "no_database")
    offer(ACTION_ADD_READER, database_block)
    offer(ACTION_REMOVE_READER, infrastructure.ERROR_CODE if external else (
        None if any(row.get("readers") for row in envelope.get("databases") or [])
        else "no_reader"))

    cache_block = infrastructure.ERROR_CODE if external else (
        None if envelope.get("caches") else "no_cache_group")
    offer(ACTION_SET_CACHE_REPLICAS, cache_block)

    # The resizes share their family's availability: a region with a cache
    # group can resize it, a region with a database can resize an instance.
    # Per-group cluster-mode blocking rides on each cache row's blocked_reason.
    offer(ACTION_RESIZE_CACHE, cache_block)
    offer(ACTION_RESIZE_DATABASE, database_block)

    egress = envelope.get("egress") or {}

    def egress_block():
        # Order matters: unknown fleet is not an empty fleet, and no failed
        # read — AWS or policy — may render its answer as canonical.
        if external:
            return infrastructure.ERROR_CODE
        if not egress.get("fleet_available"):
            return "fleet_unavailable"
        if not egress.get("addresses_available"):
            return "addresses_unavailable"
        if not egress.get("policy_available"):
            return "policy_unavailable"
        return None

    enable_block = egress_block()
    if enable_block is None:
        if not instances:
            enable_block = "no_fleet_nodes"
        elif egress.get("enabled") and not egress.get("pending_nodes"):
            # Converged. Re-running enable is only offered while something is
            # left to converge — that re-run IS the retry path after a partial
            # failure.
            enable_block = "already_enabled"
    offer(ACTION_ENABLE_STABLE_IPS, enable_block)

    disable_block = egress_block()
    if disable_block is None:
        managed_attached = any(row.get("managed")
                               for row in egress.get("attached") or [])
        # Disable stays offered while policy is off but a managed address is
        # still attached — that is a half-done detach, and re-running disable
        # is how it finishes.
        if not egress.get("enabled") and not managed_attached:
            disable_block = "not_enabled"
    offer(ACTION_DISABLE_STABLE_IPS, disable_block)
    return offers


def _build(elbv2_client=None, ec2_client=None, rds_client=None, cache_client=None):
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "region": _region(),
        "mode": infrastructure.infrastructure_mode(),
        "generated_at": _now(),
        "node_id_pinned": _node_id_pinned(),
        "nodes": {"balancers": [], "groups": [], "instances": [],
                  "self": None, "self_check": "unavailable"},
        "databases": [],
        "caches": [],
        "warnings": [],
    }
    serving = _collect(envelope, "serving", "elasticloadbalancing:DescribeTargetHealth",
                       lambda: _serving(client=elbv2_client))
    if serving is not None:
        ids = sorted({target.get("id")
                      for group in serving.get("groups") or []
                      for target in group.get("targets") or []
                      if str(target.get("id") or "").startswith("i-")})
        # ONE describe_instances for the whole panel: the self check, the
        # primary flag, the instance types and the capacity-added tag all come
        # out of the same read.
        facts = {}
        if ids:
            facts = _collect(envelope, "instances", "ec2:DescribeInstances",
                             lambda: ec2_helper.instance_map(
                                 ids, client=ec2_client)) or {}
        self_id, self_status = _self_id(facts) if facts else (None, "unavailable")
        envelope["nodes"] = {
            "balancers": serving.get("balancers") or [],
            "groups": [{key: group[key] for key in
                        ("arn", "name", "target_type", "port", "protocol")}
                       for group in serving.get("groups") or []],
            "instances": _node_rows(serving, facts, self_id, _primary_host()),
            "self": self_id,
            "self_check": self_status,
        }
    addresses = _collect(envelope, "addresses", "ec2:DescribeAddresses",
                         lambda: ec2_helper.address_map(client=ec2_client))
    envelope["egress"] = _egress_envelope(serving, addresses)
    if serving is not None and addresses is not None and not _fleet_ids(serving):
        envelope["egress"]["fallback_attached"] = _fallback_attached(
            addresses, envelope, ec2_client=ec2_client)
    held = {row.get("instance") for row in envelope["egress"]["attached"]}
    for row in envelope["nodes"]["instances"]:
        row["stable_ip"] = row["id"] in held
    databases = _collect(envelope, "databases", "rds:DescribeDBClusters",
                         lambda: _database_rows(rds_client=rds_client))
    envelope["databases"] = databases or []
    caches = _collect(envelope, "caches", "elasticache:DescribeReplicationGroups",
                      lambda: _cache_rows(cache_client=cache_client))
    envelope["caches"] = caches or []
    envelope["reader_routing"] = _reader_routing(envelope)
    # Static data, no provider read — no _collect wrapper needed.
    envelope["sizes"] = _size_catalog()
    envelope["actions"] = _offers(envelope)
    return envelope


def _reader_routing(envelope, django_databases=None, django_routers=None,
                    skip_reason=None, redis_reader_on=None):
    """What THIS process actually does with readers, self-reported.

    The reader settings are file-only and read at boot, so no admin surface
    can see them by querying anything — except the serving process itself,
    which IS the thing the question is about. The answer is per-node: a node
    that has not restarted since the config line was added still runs without
    routing, so the panel labels this "this node", and fleet-wide convergence
    stays a deployments question.

    The keyword arguments exist for tests (no process-global settings
    mutation); production callers pass none of them.
    """
    from django.conf import settings as django_settings
    from mojo.db import config as db_reader_config
    from mojo.helpers.redis import client as redis_client

    if django_databases is None:
        django_databases = getattr(django_settings, "DATABASES", None)
    if django_routers is None:
        django_routers = getattr(django_settings, "DATABASE_ROUTERS", None)
    if skip_reason is None:
        skip_reason = db_reader_config.LAST_SKIP_REASON
    if redis_reader_on is None:
        redis_reader_on = redis_client.reader_configured()

    databases = django_databases if isinstance(django_databases, dict) else {}
    reader = databases.get("reader")
    host = reader.get("HOST") if isinstance(reader, dict) else None
    active = bool(reader) and db_reader_config._ROUTER in list(django_routers or [])

    # The one check that catches a paste error: the configured host against
    # the cluster reader endpoints AWS itself reports. None when there is
    # nothing to compare — unknown, never a false alarm.
    endpoints = [row.get("reader_endpoint")
                 for row in envelope.get("databases") or []]
    endpoints = [value for value in endpoints if value]
    matches = None
    if active and host and endpoints:
        matches = host in endpoints

    return {
        "database": {
            "active": active,
            # Host only — the alias carries credentials this report must not.
            "host": host,
            "skip_reason": skip_reason,
            "matches_reader_endpoint": matches,
        },
        "redis": {"active": bool(redis_reader_on)},
    }


def _egress_envelope(serving, addresses):
    """The stable-outbound picture: policy, allowlist, and what is missing.

    ``addresses`` (the canonical vendor allowlist) is what IS attached —
    association is ground truth for reporting, tags only gate mutation — with
    unmanaged rows labelled rather than hidden. ``available`` requires BOTH
    reads: a fleet that could not be read is unknown, not empty, and an empty
    allowlist rendered as canonical would be a lie in the one place an
    operator copies from.
    """
    fleet_available = serving is not None
    addresses_available = addresses is not None
    fleet = _fleet_ids(serving) if fleet_available else []
    view = _address_view(fleet, addresses) if (
        fleet_available and addresses_available) else {
        "attached": [], "reserved": [], "pending": []}
    enabled, policy_available = _egress_policy()
    return {
        "enabled": enabled,
        "available": fleet_available and addresses_available,
        "fleet_available": fleet_available,
        "addresses_available": addresses_available,
        "policy_available": policy_available,
        "addresses": sorted({row.get("public_ip")
                             for row in view["attached"]
                             if row.get("public_ip")}),
        "attached": view["attached"],
        "pending_nodes": list(view["pending"]),
        "reserved": view["reserved"],
        "to_allocate": max(0, len(view["pending"]) - len(view["reserved"])),
        "monthly_usd_per_address": EIP_MONTHLY_USD,
        # Balancer-less installs: filled by _build from _fallback_attached.
        # Deliberately a separate key — the offers and both runners read only
        # the fleet-scoped facts above, so a read-only fallback row can never
        # make a mutation look available.
        "fallback_attached": [],
    }


def _fallback_attached(addresses, envelope, ec2_client=None):
    """Balancer-less installs: what holds a stable address anyway. Read-only.

    With no registered fleet there is nothing for the toggle to manage — but
    an operator on a single-node install still needs the address vendors must
    allow, and "no load balancer" must not read as "no stable address".
    Association to an EC2 INSTANCE is the filter: an address held by a load
    balancer or a NAT gateway is inbound plumbing, not node egress.
    """
    held = [row for row in addresses or [] if row.get("instance_id")]
    if not held:
        return []
    ids = sorted({row["instance_id"] for row in held})
    facts = _collect(envelope, "instances", "ec2:DescribeInstances",
                     lambda: ec2_helper.instance_map(
                         ids, client=ec2_client)) or {}
    rows = []
    for row in held:
        instance = row["instance_id"]
        rows.append({
            "instance": instance,
            "instance_name": (facts.get(instance) or {}).get("name") or instance,
            "public_ip": row.get("public_ip"),
            "allocation_id": row.get("allocation_id"),
            "managed": _stable_tagged(row.get("tags")),
        })
    rows.sort(key=lambda row: (str(row.get("instance_name") or ""),
                               str(row.get("instance") or "")))
    return rows


def report(refresh=False, elbv2_client=None, ec2_client=None, rds_client=None,
           cache_client=None):
    """The capacity panel's whole read. Cached briefly per region.

    Short TTL on purpose: this is a page an operator opens BECAUSE something is
    changing, so it must not show them a five-minute-old fleet. Every apply
    invalidates it, and the panel's refresh control bypasses it entirely.
    """
    injected = any(value is not None for value in
                   (elbv2_client, ec2_client, rds_client, cache_client))
    key = _report_key()
    if not refresh and not injected:
        try:
            cached = cache.get(key)
        except Exception:
            cached = None
        if isinstance(cached, dict) and cached.get("schema_version") == SCHEMA_VERSION:
            return cached
    envelope = _build(elbv2_client, ec2_client, rds_client, cache_client)
    if not injected:
        try:
            cache.set(key, envelope, REPORT_TTL)
        except Exception:
            logger.warning("capacity report could not be cached")
    return envelope


# ── user data ───────────────────────────────────────────────────────────────

def node_user_data(base_name, root=None):
    """The clone's first-boot script.

    Order is the whole point:

    1. IMDSv2 (token first — the launch forces ``HttpTokens=required``, so an
       unauthenticated metadata read would simply fail) for this instance's own
       id, and a hostname derived from it. Unique by construction, and derived
       from something only this box knows.
    2. config-sync, so the node pulls ITS OWN ``django.conf`` from S3 rather
       than running on whatever the AMI happened to bake. The baked config is
       deliberately NOT deleted: config-sync failing on a node with no config
       at all is an unbootable box, and the sync overwrites in place anyway.
    3. ``node_setup``, which is idempotent and converges var/ ownership,
       systemd units and the jobs cron.
    4. A restart of the app service and the job engine. Cloud-init runs LATE:
       both were already started from the AMI, under the SOURCE's hostname, so
       without this the clone's engine registers as the source's runner id and
       two boxes answer as one node. Best-effort — a fleet that names its units
       differently loses the restart, not the boot.
    """
    root = root or _node_root()
    safe_base = "".join(char if (char.isalnum() or char == "-") else "-"
                        for char in str(base_name or "mojo-node").lower()).strip("-")
    safe_base = safe_base or "mojo-node"
    safe_root = str(root).replace('"', "")
    return "\n".join([
        "#!/bin/bash",
        "set -u",
        "TOKEN=$(curl -sS -X PUT "
        '"http://169.254.169.254/latest/api/token" '
        '-H "X-aws-ec2-metadata-token-ttl-seconds: 300" || true)',
        'IID=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" '
        '"http://169.254.169.254/latest/meta-data/instance-id" || true)',
        'if [ -n "$IID" ]; then',
        f'  hostnamectl set-hostname "{safe_base}-${{IID##*-}}"',
        "fi",
        "systemctl start config-sync.service || true",
        f'python3 -m mojo.deploy.node_setup --root "{safe_root}" || true',
        "systemctl restart mojo-asgi.service || true",
        f'"{safe_root}/bin/jobman" restart || true',
        "",
    ])


def expected_node_id(base_name, instance_id):
    """The hostname the user-data above will set, computed the same way.

    ``${IID##*-}`` in the script is exactly ``instance_id.split("-")[-1]`` here,
    and this value IS the readiness node id — the runner id is it plus
    ``-engine``. Deriving it server-side is what lets the join leg wait for one
    named runner instead of diffing the roster and guessing.
    """
    safe_base = "".join(char if (char.isalnum() or char == "-") else "-"
                        for char in str(base_name or "mojo-node").lower()).strip("-")
    return f"{safe_base or 'mojo-node'}-{str(instance_id).rsplit('-', 1)[-1]}"


def expected_runner_id(node_id):
    from mojo.apps.jobs import ENGINE_CHANNEL_SUFFIX
    return f"{node_id}{ENGINE_CHANNEL_SUFFIX}"


# ── apply ───────────────────────────────────────────────────────────────────

def _refuse_external(action_label):
    """Backstop for NON-REST callers (a shell, a job, a future importer).

    The REST gate already answered HTTP for every ordinary caller, so reaching
    this line means something bypassed it — which is exactly when a deliberate,
    logged refusal is worth the duplication.
    """
    if not infrastructure.is_external():
        return
    logger.error("capacity apply refused: %s is %s on this installation",
                 infrastructure.SETTING, infrastructure.EXTERNAL)
    raise CapacityError(
        infrastructure.refusal_message(action_label),
        infrastructure.ERROR_CODE, 403)


# The tags that name WHICH fleet a resource belongs to. Provision stamps both
# (spec.TAGS), and spec.owns() refuses anything whose values differ — the
# generic django-mojo tags alone cannot tell staging from prod in one account.
FLEET_IDENTITY_TAGS = ("mojo:project", "mojo:env")


def _identity_matches(candidate_tags, reference_tags):
    """Exact fleet identity: project AND env equal, both present on both sides.

    A missing value on EITHER side is a mismatch, never a wildcard — an
    anchor that cannot state its own identity proves nothing about anyone.
    """
    for key in FLEET_IDENTITY_TAGS:
        wanted = (reference_tags or {}).get(key)
        held = (candidate_tags or {}).get(key)
        if not wanted or not held or wanted != held:
            return False
    return True


def _prove_fleet_member(resource, serving, ec2_client=None):
    """Fresh EC2 facts must prove an UNREGISTERED node is ours to terminate.

    A COMPLETED drain removes the target from its group entirely, so the
    drained node terminate exists to destroy is invisible to the serving map.
    Membership is then proven from the provider — never assumed from the
    request — and a generic django-mojo tag is NOT enough: a staging and a
    prod fleet in one account+region are both django-mojo-tagged, and this
    predicate gates TerminateInstances (the exact reason ``spec.owns()``
    matches identity, never ownership alone). Two proofs are accepted:

    - the ``mojo:created-by=admin-capacity`` stamp this feature puts on every
      clone it launches, or
    - a django-mojo ownership tag PLUS an exact identity match — the
      candidate's ``mojo:project`` and ``mojo:env`` equal to those of a
      CURRENTLY REGISTERED fleet member (the anchor), with a missing value on
      either side counting as a mismatch.

    No registered member to anchor against means identity cannot be verified,
    and the terminate is refused outright. Fail closed.
    """
    try:
        facts = ec2_helper.instance_facts(resource, client=ec2_client)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report this instance, so no terminate is safe "
                 "to make.") from None
    state = str((facts or {}).get("state") or "")
    if facts is not None and state in ("terminated", "shutting-down"):
        raise CapacityError(
            f"{resource} is already {state}.", "already_terminated", 409)
    tags = (facts or {}).get("tags") or {}
    if (facts is not None
            and tags.get(ec2_helper.CREATED_BY_TAG) == "admin-capacity"):
        return
    if facts is None or not _django_mojo_tagged(tags):
        raise CapacityError(
            f"{resource} is not registered behind any load balancer, and "
            f"fresh EC2 facts do not prove it a django-mojo fleet member — "
            f"this control will not terminate it.",
            "not_fleet_member", 409)
    anchor_id = next(iter(_fleet_ids(serving)), None)
    if anchor_id is None:
        raise CapacityError(
            f"The fleet has no registered member to prove identity against, "
            f"so this control cannot verify that {resource} belongs to THIS "
            f"installation. Terminate it from the AWS console if you are "
            f"certain.",
            "not_fleet_member", 409)
    try:
        anchor = ec2_helper.instance_facts(anchor_id, client=ec2_client)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report the fleet's registered member, so this "
                 "instance's identity cannot be verified.") from None
    if not _identity_matches(tags, (anchor or {}).get("tags")):
        raise CapacityError(
            f"{resource} carries django-mojo tags, but its mojo:project/"
            f"mojo:env do not match this fleet's registered members — it may "
            f"belong to another environment in this account. This control "
            f"will not terminate it.",
            "not_fleet_member", 409)


def _prepare_add_node(serving, ec2_client=None):
    """Everything an add decides BEFORE anything is created."""
    if _node_id_pinned():
        raise CapacityError(
            "This fleet pins EDGE_NODE_ID, so every node reports the same "
            "readiness identity and a new node could never be proven. Remove "
            "the pin before adding capacity.",
            "node_id_pinned", 409)
    healthy = [target.get("id") for group in serving.get("groups") or []
               for target in group.get("targets") or []
               if target.get("state") == "healthy"
               and str(target.get("id") or "").startswith("i-")]
    source = _source_node(sorted(set(healthy)), client=ec2_client)
    if source is None:
        raise CapacityError(
            "No healthy, running fleet member is available to clone. A node "
            "can only be added from a node that is currently serving.",
            "no_source_node", 409)
    groups = [{"arn": group.get("arn"), "name": group.get("name"),
               "port": next((target.get("port")
                             for target in group.get("targets") or []
                             if target.get("id") == source["instance_id"]), None)}
              for group in _groups_holding(serving, source["instance_id"])]
    return source, groups


def apply(actor, action, resource="", **params):
    """Start ONE capacity change. Returns the operation record.

    Every guard that can be evaluated before a mutation IS evaluated before a
    mutation, against a fresh provider read. What survives that is claimed
    single-flight, recorded, and handed to a job — the long legs (an AMI, a
    boot, a convergence, a proof) are minutes of polling and belong nowhere
    near a request thread.
    """
    _refuse_external("Changing capacity")
    if action not in ACTIONS:
        raise CapacityError(f"Unknown capacity action '{action}'", "invalid_request")
    resource = str(resource or "").strip()

    if action in (ACTION_ADD_NODE, ACTION_DRAIN_NODE, ACTION_TERMINATE_NODE):
        return _apply_node(actor, action, resource, **params)
    if action in (ACTION_ADD_READER, ACTION_REMOVE_READER):
        return _apply_reader(actor, action, resource, **params)
    if action in (ACTION_ENABLE_STABLE_IPS, ACTION_DISABLE_STABLE_IPS):
        return _apply_stable_ips(actor, action, **params)
    if action == ACTION_RESIZE_CACHE:
        return _apply_resize_cache(actor, resource, **params)
    if action == ACTION_RESIZE_DATABASE:
        return _apply_resize_database(actor, resource, **params)
    return _apply_cache(actor, resource, **params)


def _apply_node(actor, action, resource, elbv2_client=None, ec2_client=None, **_ignored):
    try:
        serving = _serving(client=elbv2_client)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report the serving tier, so no node change is "
                 "safe to make.") from None

    if action == ACTION_ADD_NODE:
        source, groups = _prepare_add_node(serving, ec2_client=ec2_client)
        claim = _claim(action, "fleet", getattr(actor, "pk", None))
        record = _new_operation(action, source["instance_id"], actor, claim, {
            "source_instance": source["instance_id"],
            "source_name": source.get("name"),
            "instance_type": source.get("instance_type"),
            "subnet_id": source.get("subnet_id"),
            "target_groups": groups,
        })
        _dispatch(record)
        return record

    if not str(resource or "").startswith("i-"):
        raise CapacityError("A node identifier is required", "invalid_request")
    holding = _groups_holding(serving, resource)
    if not holding and action == ACTION_DRAIN_NODE:
        raise CapacityError(
            f"{resource} is not registered behind any load balancer.",
            "not_registered", 409)
    serving_ids = [row.get("id") for group in serving.get("groups") or []
                   for row in group.get("targets") or []]
    # The bare resource id rides along: a fully-drained node is absent from
    # every group, and the self check must still be able to match it — a
    # drained self node must not terminate itself.
    self_id, self_status = _self_check(
        serving_ids + ([resource] if resource not in serving_ids else []),
        client=ec2_client)
    if self_id and self_id == resource:
        raise CapacityError(
            f"{resource} is the node answering this request. Removing it "
            f"would cut the connection carrying the removal.",
            "cannot_remove_self", 409, {"self_check": self_status})

    if action == ACTION_DRAIN_NODE:
        stranded = _would_strand(resource, serving)
        if stranded is not None:
            raise CapacityError(
                f"{resource} is the only healthy target in "
                f"{stranded.get('name') or stranded.get('arn')}. Draining it "
                f"would take that target group out of service.",
                "last_healthy_target", 409,
                {"target_group": stranded.get("name") or stranded.get("arn")})
        claim = _claim(action, resource, getattr(actor, "pk", None))
        record = _new_operation(action, resource, actor, claim, {
            "target_groups": [{"arn": group.get("arn"), "name": group.get("name"),
                               "port": next((target.get("port")
                                             for target in group.get("targets") or []
                                             if target.get("id") == resource), None)}
                              for group in holding],
            "self_check": self_status,
        })
        _dispatch(record)
        return record

    # terminate: the drain is re-proved HERE, server-side, from a fresh read.
    # A client that says "already drained" is a client, not evidence.
    for group in holding:
        rows = [target for target in group.get("targets") or []
                if target.get("id") == resource]
        if not elbv2_helper.drained(rows):
            raise CapacityError(
                f"{resource} is still registered in "
                f"{group.get('name') or group.get('arn')} and has not finished "
                f"draining. Drain it first, and wait for the drain to complete.",
                "not_drained", 409,
                {"target_group": group.get("name") or group.get("arn"),
                 "states": sorted({row.get("state") for row in rows})})
    if not holding:
        _prove_fleet_member(resource, serving, ec2_client=ec2_client)
    claim = _claim(action, resource, getattr(actor, "pk", None))
    record = _new_operation(action, resource, actor, claim,
                            {"self_check": self_status})
    _dispatch(record)
    return record


def _apply_reader(actor, action, resource, rds_client=None, **params):
    if not resource:
        raise CapacityError("A database identifier is required", "invalid_request")
    try:
        if action == ACTION_ADD_READER:
            cluster = rds_helper.cluster_members(resource, client=rds_client)
            if cluster is not None:
                detail = {"kind": "aurora", "cluster": resource,
                          "instance_class": params.get("instance_class")
                          or _default_class(cluster, rds_client),
                          "engine": cluster.get("engine")}
            else:
                source = rds_helper.instance_role(resource, client=rds_client)
                if source is None:
                    raise CapacityError(
                        f"AWS reports no database called {resource}.",
                        "resource_not_found", 404)
                if source.get("is_replica"):
                    raise CapacityError(
                        f"{resource} is itself a read replica. Add the reader "
                        f"to its source database instead.",
                        "not_a_source", 409)
                detail = {"kind": "standalone", "source": resource,
                          "instance_class": source.get("instance_class"),
                          "engine": source.get("engine")}
            detail["reader_id"] = _reader_id(resource)
            claim = _claim(action, resource, getattr(actor, "pk", None))
            record = _new_operation(action, resource, actor, claim, detail)
            _dispatch(record)
            return record

        # remove: the target must PROVABLY be a reader/replica. Nothing else is
        # deletable here, and SkipFinalSnapshot makes being wrong permanent.
        target = rds_helper.instance_role(resource, client=rds_client)
        if target is None:
            raise CapacityError(
                f"AWS reports no database instance called {resource}.",
                "resource_not_found", 404)
        cluster_id = target.get("cluster")
        if cluster_id:
            cluster = rds_helper.cluster_members(cluster_id, client=rds_client) or {}
            if resource not in (cluster.get("readers") or []):
                raise CapacityError(
                    f"{resource} is the writer of {cluster_id}, not a reader.",
                    "not_a_reader", 409)
        elif not target.get("is_replica"):
            raise CapacityError(
                f"{resource} is not a read replica — it is a primary database. "
                f"This control only removes read capacity.",
                "not_a_reader", 409)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report this database, so no change was made."
        ) from None
    claim = _claim(action, resource, getattr(actor, "pk", None))
    record = _new_operation(action, resource, actor, claim, {
        "cluster": target.get("cluster"), "replica_of": target.get("replica_source")})
    _dispatch(record)
    return record


def _default_class(cluster, rds_client=None):
    """Match a new Aurora reader to the writer's instance class."""
    writer = cluster.get("writer")
    if not writer:
        return None
    row = rds_helper.instance_role(writer, client=rds_client) or {}
    return row.get("instance_class")


def _reader_id(resource):
    stamp = uuid.uuid4().hex[:8]
    base = str(resource)[:40].rstrip("-")
    return f"{base}-reader-{stamp}"


def _apply_cache(actor, resource, count=None, apply_immediately=None,
                 cache_client=None, **_ignored):
    if not resource:
        raise CapacityError("A cache group identifier is required", "invalid_request")
    if count is None:
        raise CapacityError(
            "count is required: state the number of replicas the group should "
            "have when this finishes", "invalid_request")
    try:
        wanted = int(count)
    except (TypeError, ValueError):
        raise CapacityError("count must be a whole number", "invalid_request") from None
    if apply_immediately is not True:
        # ElastiCache supports only an immediate replica-count change. Rather
        # than silently rewrite a False into a True, say what the operator is
        # actually agreeing to.
        raise CapacityError(
            "apply_immediately must be true: ElastiCache applies a replica-count "
            "change immediately and offers no maintenance-window option.",
            "invalid_request")
    try:
        facts = elasticache_helper.replication_group_facts(resource, client=cache_client)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report this cache group, so no change was made."
        ) from None
    if facts is None:
        raise CapacityError(
            f"AWS reports no replication group called {resource}.",
            "resource_not_found", 404)
    if facts.get("cluster_enabled"):
        raise CapacityError(
            f"{resource} is cluster-mode enabled. Its replica count is a "
            f"per-shard resharding decision, not a capacity change, and this "
            f"control does not make it.",
            elasticache_helper.CLUSTER_MODE_UNSUPPORTED, 409)
    current = int(facts.get("replica_count") or 0)
    if wanted < 0:
        raise CapacityError("count cannot be negative", "invalid_request")
    if wanted == current:
        raise CapacityError(
            f"{resource} already has {current} replica(s).", "no_change", 409)
    if wanted < 1 and (facts.get("automatic_failover_on") or facts.get("multi_az_on")):
        raise CapacityError(
            f"{resource} has automatic failover enabled, which requires at "
            f"least one replica. Removing the last replica would leave nothing "
            f"to fail over to.",
            elasticache_helper.FAILOVER_REQUIRES_REPLICA, 409)
    claim = _claim(ACTION_SET_CACHE_REPLICAS, resource, getattr(actor, "pk", None))
    record = _new_operation(ACTION_SET_CACHE_REPLICAS, resource, actor, claim, {
        "from_count": current, "to_count": wanted,
        "automatic_failover_on": facts.get("automatic_failover_on"),
        "multi_az_on": facts.get("multi_az_on"),
        "apply_immediately": True,
    })
    _dispatch(record)
    return record


def _require_immediate(apply_immediately, deferral):
    """Both resizes demand a literal True — no default, no silent rewrite.

    These are observed operations with a settle check; a change deferred to
    the maintenance window has nothing to observe and would report a timeout
    for a change that is merely queued.
    """
    if apply_immediately is not True:
        raise CapacityError(
            f"apply_immediately must be true: this control makes observed, "
            f"immediate changes with a settle check. {deferral} Schedule a "
            f"deferred change in the AWS console instead.",
            "invalid_request")


def _apply_resize_cache(actor, resource, size=None, apply_immediately=None,
                        cache_client=None, **_ignored):
    if not resource:
        raise CapacityError("A cache group identifier is required", "invalid_request")
    size_key = str(size or "").strip().lower()
    to_type = _resolve_size(provision_spec.CACHE_SIZES, size_key)
    _require_immediate(apply_immediately,
                       "A deferred node-type change has nothing to observe.")
    try:
        facts = elasticache_helper.replication_group_facts(resource, client=cache_client)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report this cache group, so no change was made."
        ) from None
    if facts is None:
        raise CapacityError(
            f"AWS reports no replication group called {resource}.",
            "resource_not_found", 404)
    if facts.get("cluster_enabled"):
        raise CapacityError(
            f"{resource} is cluster-mode enabled. Sizing a sharded group is a "
            f"resharding decision, not a capacity change, and this control "
            f"does not make it.",
            elasticache_helper.CLUSTER_MODE_UNSUPPORTED, 409)
    status = str(facts.get("status") or "").lower()
    # "snapshotting" is the nightly backup — routine background work, never a
    # conflict. Anything else short of available may equally be AWS's own
    # background activity, so the refusal quotes the state verbatim and never
    # claims another change is in flight.
    if status not in (elasticache_helper.SETTLED, "snapshotting"):
        raise CapacityError(
            f"{resource} is {status}; a resize needs it settled — try again "
            f"when it reports available.",
            "not_settled", 409)
    from_type = facts.get("node_type")
    if from_type == to_type:
        raise CapacityError(f"{resource} already runs {to_type}.", "no_change", 409)
    replica_count = int(facts.get("replica_count") or 0)
    failover_on = bool(facts.get("automatic_failover_on"))
    from_index = _ladder_index(provision_spec.CACHE_SIZES, from_type)
    to_index = _ladder_index(provision_spec.CACHE_SIZES, to_type)
    claim = _claim(ACTION_RESIZE_CACHE, resource, getattr(actor, "pk", None))
    record = _new_operation(ACTION_RESIZE_CACHE, resource, actor, claim, {
        "from_type": from_type, "to_type": to_type, "size": size_key,
        # The same rule the report's resize_impact states, re-derived from the
        # fresh facts this apply is acting on.
        "impact": ("rolling" if (failover_on and replica_count >= 1)
                   else "downtime"),
        "replica_count": replica_count,
        "automatic_failover_on": failover_on,
        "apply_immediately": True,
        "monthly_usd": provision_spec.COST_TABLE.get(to_type),
        # True/False by ladder position; None when the current type is not
        # curated and the direction is unknown.
        "downsize": None if from_index is None else to_index < from_index,
    })
    _dispatch(record)
    return record


def _apply_resize_database(actor, resource, size=None, apply_immediately=None,
                           rds_client=None, **_ignored):
    if not resource:
        raise CapacityError("A database instance identifier is required",
                            "invalid_request")
    size_key = str(size or "").strip().lower()
    to_class = _resolve_size(provision_spec.DB_SIZES, size_key)
    _require_immediate(apply_immediately,
                       "A deferred class change parks silently until the "
                       "maintenance window.")
    try:
        role = rds_helper.instance_role(resource, client=rds_client)
        if role is None:
            raise CapacityError(
                f"AWS reports no database instance called {resource}.",
                "resource_not_found", 404)
        status = str(role.get("status") or "").lower()
        # backing-up / storage-optimization / maintenance are routine RDS
        # background windows in which a class change is permitted. Anything
        # else — modifying, rebooting, creating, and above all deleting (the
        # remove_reader race) — is refused with the provider state quoted
        # verbatim, attributing nothing.
        if status not in (RDS_SETTLED, "backing-up", "storage-optimization",
                          "maintenance"):
            raise CapacityError(
                f"{resource} is {status}; a resize needs it settled — try "
                f"again when it reports available.",
                "not_settled", 409)
        from_class = role.get("instance_class")
        if from_class == to_class:
            raise CapacityError(
                f"{resource} already runs {to_class}.", "no_change", 409)
        cluster_id = role.get("cluster")
        if cluster_id:
            # Aurora member: the writer is known ONLY through IsClusterWriter.
            # The tier rides in the same ModifyDBInstance call — writer at 0,
            # readers at 1, so a failover prefers the big box — and this item
            # never moves the writer role itself.
            cluster = rds_helper.cluster_members(cluster_id, client=rds_client) or {}
            role_name = "writer" if resource == cluster.get("writer") else "reader"
            promotion_tier = 0 if role_name == "writer" else 1
        else:
            role_name = "reader" if role.get("is_replica") else "writer"
            # PromotionTier is an Aurora concept; never sent off-Aurora.
            promotion_tier = None
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report this database, so no change was made."
        ) from None
    claim = _claim(ACTION_RESIZE_DATABASE, resource, getattr(actor, "pk", None))
    record = _new_operation(ACTION_RESIZE_DATABASE, resource, actor, claim, {
        "kind": "aurora" if cluster_id else "standalone",
        "cluster": cluster_id, "role": role_name,
        "from_class": from_class, "to_class": to_class, "size": size_key,
        "promotion_tier": promotion_tier,
        "apply_immediately": True,
        "monthly_usd": provision_spec.COST_TABLE.get(to_class),
    })
    _dispatch(record)
    return record


def _write_policy(actor, enabled):
    """Persist the desired policy through the only allowed writer.

    Written in the REQUEST thread, before dispatch: the durable intent is
    recorded by the human-authenticated request (``set_value`` re-proves the
    live superuser), and the job merely converges it. A failed job then leaves
    the report showing enabled-but-pending (or disabled-but-attached) with the
    action still offered as the retry.
    """
    from mojo.apps.account.services import system_settings
    system_settings.set_value(actor, STABLE_EGRESS_SETTING,
                              {"enabled": bool(enabled)})


def _validate_assign(assign, view, fleet):
    """The operator's explicit address choices, refused unless provably safe."""
    if not assign:
        return {}
    if not isinstance(assign, dict):
        raise CapacityError(
            "assign must map instance ids to Elastic IP allocation ids",
            "invalid_request")
    unassociated = {str(row.get("allocation_id")): row
                    for row in view["unassociated"]}
    cleaned = {}
    for instance, allocation in assign.items():
        instance = str(instance or "").strip()
        allocation = str(allocation or "").strip()
        if instance not in fleet:
            raise CapacityError(
                f"{instance} is not a registered fleet node.",
                "invalid_request", 409)
        if instance in view["by_instance"]:
            raise CapacityError(
                f"{instance} already holds an Elastic IP — there is nothing "
                f"to assign to it.", "invalid_request", 409)
        row = unassociated.get(allocation)
        if row is None:
            raise CapacityError(
                f"{allocation} is not an unassociated Elastic IP in this "
                f"region.", "address_not_eligible", 409)
        tags = row.get("tags") or {}
        if not (_stable_tagged(tags) or _django_mojo_tagged(tags)):
            raise CapacityError(
                f"{allocation} carries no django-mojo ownership tag, so this "
                f"control will not consume it — it may be someone else's "
                f"reservation. If it is really yours, tag it in the AWS "
                f"console first (managed-by=django-mojo).",
                "address_not_eligible", 409)
        if allocation in cleaned.values():
            raise CapacityError(
                f"{allocation} is assigned to more than one node.",
                "invalid_request")
        cleaned[instance] = allocation
    return cleaned


def _apply_stable_ips(actor, action, assign=None, elbv2_client=None,
                      ec2_client=None, **_ignored):
    """Guards, plan, claim, policy write, dispatch — for the fleet switch."""
    try:
        serving = _serving(client=elbv2_client)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report the serving tier, so the fleet's "
                 "addresses cannot be changed safely.") from None
    fleet = _fleet_ids(serving)
    try:
        addresses = ec2_helper.address_map(client=ec2_client)
    except ProviderCallError as err:
        raise _provider_error(
            err, "AWS did not report the region's Elastic IPs, so no address "
                 "change was planned.") from None
    view = _address_view(fleet, addresses)

    if action == ACTION_ENABLE_STABLE_IPS:
        if not fleet:
            raise CapacityError(
                "No node is registered behind a load balancer, so there is "
                "nothing to give a stable address to.",
                "no_fleet_nodes", 409)
        assignments = _validate_assign(assign, view, fleet)
        detail = {
            "fleet": fleet,
            "pending": list(view["pending"]),
            "reserved": [row.get("allocation_id") for row in view["reserved"]],
            "assign": assignments,
            "to_allocate": max(0, len(view["pending"]) - len(view["reserved"])),
        }
    else:
        managed = [row for row in view["attached"] if row.get("managed")]
        if not _egress_enabled() and not managed:
            raise CapacityError(
                "Stable outbound IPs are not enabled and no managed address "
                "is attached — there is nothing to disable.",
                "no_change", 409)
        detail = {"fleet": fleet,
                  "attached": [row.get("allocation_id") for row in managed]}

    claim = _claim(action, "fleet", getattr(actor, "pk", None))
    try:
        _write_policy(actor, action == ACTION_ENABLE_STABLE_IPS)
    except Exception:
        _release(claim)
        raise
    record = _new_operation(action, "fleet", actor, claim, detail)
    try:
        _dispatch(record)
    except CapacityError as err:
        if err.error_code != "dispatch_failed":
            raise
        # The generic dispatch refusal says "nothing was changed" — false
        # here, because the durable policy flipped a line ago and the add_node
        # admission gate already honors it. Say what actually happened.
        state = ("enabled" if action == ACTION_ENABLE_STABLE_IPS
                 else "disabled")
        raise CapacityError(
            f"The stable-outbound-IPs policy IS recorded ({state}), but no "
            f"job runner accepted the convergence, so no address was "
            f"attached or detached. Run the action again to converge.",
            "dispatch_failed", 503) from None
    return record


def _dispatch(record):
    """Hand one recorded operation to a job. A publish failure fails the op."""
    from mojo.apps import jobs
    try:
        jobs.publish(
            func="mojo.apps.aws.asyncjobs.capacity_operation",
            payload={"operation": record["id"]},
            channel="cleanup", max_retries=0,
            max_exec_seconds=_deadline_for(record["action"]) + 120)
    except Exception:
        logger.exception("capacity: operation %s could not be dispatched",
                         record.get("id"))
        _fail(record, "dispatch_failed",
              "The operation could not be handed to a job runner, so nothing "
              "was started.")
        raise CapacityError(
            "This installation's job runners did not accept the operation, so "
            "nothing was changed.", "dispatch_failed", 503) from None
    logger.info("capacity operation %s dispatched action=%s resource=%s actor=%s",
                record["id"], record["action"], record["resource"], record["actor"])


def _deadline_for(action):
    if action == ACTION_ADD_NODE:
        return (IMAGE_TIMEOUT + LAUNCH_TIMEOUT + RUNNER_TIMEOUT
                + PROOF_MARGIN + HEALTH_TIMEOUT)
    if action == ACTION_DRAIN_NODE:
        return 3600
    if action in (ACTION_ADD_READER, ACTION_REMOVE_READER,
                  ACTION_RESIZE_DATABASE):
        return RDS_TIMEOUT
    if action == ACTION_SET_CACHE_REPLICAS:
        return CACHE_TIMEOUT
    if action == ACTION_RESIZE_CACHE:
        return CACHE_RESIZE_TIMEOUT
    return 900


# ── status ──────────────────────────────────────────────────────────────────

def operation_status(operation_id):
    """One operation's recorded progress, or a 404.

    Deliberately a pure READ. The phases below are proven by the operation job,
    not by whoever happens to be polling: a status endpoint that advanced the
    work would let a caller with read-only grants drive a registration.
    """
    record = _read_operation(str(operation_id or "").strip())
    if record is None:
        raise CapacityError(
            "That capacity operation is not on record. It finished more than "
            "90 minutes ago, or the coordination cache was cleared.",
            "operation_not_found", 404)
    return record


# ── batch plans ─────────────────────────────────────────────────────────────
#
# Two-phase: plan_batch validates an ordered set of EXISTING actions against a
# fresh report and stores a short-lived, server-worded plan; apply_batch
# confirms by plan id and hands the steps to ONE job that calls the unchanged
# apply() per step. Guards are never re-implemented against a hypothetical
# future state — each step re-derives them from AWS the moment it runs,
# exactly as a manual click would, and holds exactly the claims a manual
# sequence would.

PLAN_TTL = 300
MAX_BATCH_STEPS = 20
# How long a running batch may go silent before status flags it: the runner
# ticks every POLL_INTERVAL, so minutes of silence mean the runner thread is
# gone (engine death, deploy) — nothing auto-resumes, and the flag is the
# honest surface for that.
BATCH_STALL_AFTER = 180

# The stable-ips pair is deliberately absent: fleet-wide, its own fixed claim,
# and interleaving it in a batch buys nothing. It runs alone.
BATCH_ACTIONS = (ACTION_ADD_NODE, ACTION_ADD_READER, ACTION_SET_CACHE_REPLICAS,
                 ACTION_RESIZE_CACHE, ACTION_RESIZE_DATABASE,
                 ACTION_REMOVE_READER, ACTION_DRAIN_NODE, ACTION_TERMINATE_NODE)

# Which report sections each action validates against. A degraded read of a
# touched section must surface as "AWS did not answer — retry", never as
# "unknown resource".
_BATCH_SECTIONS = {
    ACTION_ADD_NODE: ("serving", "instances"),
    ACTION_DRAIN_NODE: ("serving", "instances"),
    ACTION_TERMINATE_NODE: ("serving", "instances"),
    ACTION_ADD_READER: ("databases",),
    ACTION_REMOVE_READER: ("databases",),
    ACTION_RESIZE_DATABASE: ("databases",),
    ACTION_SET_CACHE_REPLICAS: ("caches",),
    ACTION_RESIZE_CACHE: ("caches",),
}

# Execution order: capacity never drops before a disruptive change. Ranks 2/5
# split set_cache_replicas by direction (grow with the adds, shrink with the
# removes); a terminate is pinned immediately behind its own drain afterwards.
_BATCH_RANK = {
    ACTION_ADD_NODE: 0,
    ACTION_ADD_READER: 1,
    ACTION_RESIZE_CACHE: 3,
    ACTION_RESIZE_DATABASE: 4,
    ACTION_REMOVE_READER: 6,
    ACTION_DRAIN_NODE: 7,
    ACTION_TERMINATE_NODE: 8,
}

ORDER_NOTE = ("Steps run in the server's order: additions first, then "
              "resizes, then removals — and a terminate runs immediately "
              "after its own drain.")


def _plan_key(plan_id):
    return f"{CACHE_PREFIX}:plan:{plan_id}"


def _plan_lock_key(plan_id):
    return f"{CACHE_PREFIX}:planlock:{plan_id}"


def _batch_key(batch_id):
    return f"{CACHE_PREFIX}:batch:{batch_id}"


def _write_batch(record):
    record["updated"] = _now()
    try:
        cache.set(_batch_key(record["id"]), record,
                  int(record.get("ttl") or CLAIM_TTL))
    except Exception:
        logger.warning("capacity: batch %s could not be recorded",
                       record.get("id"))
    return record


def _read_batch(batch_id):
    try:
        record = cache.get(_batch_key(batch_id))
    except Exception:
        record = None
    return record if isinstance(record, dict) else None


def _instances(envelope):
    return (envelope.get("nodes") or {}).get("instances") or []


def _node_row(envelope, resource):
    for row in _instances(envelope):
        if row.get("id") == resource:
            return row
    return None


def _cache_row(envelope, resource):
    for row in envelope.get("caches") or []:
        if row.get("identifier") == resource:
            return row
    return None


def _db_role(envelope, resource):
    """``(role, instance_class)`` for one database instance, or (None, None).

    A standalone primary is its row's writer (challenge #6: the fallback to
    ``instance_class`` matters — standalone rows carry no
    writer_instance_class).
    """
    for row in envelope.get("databases") or []:
        if resource == row.get("writer") or (
                row.get("kind") == "standalone"
                and resource == row.get("identifier")):
            return "writer", (row.get("writer_instance_class")
                              or row.get("instance_class"))
        if resource in (row.get("readers") or []):
            return "reader", (row.get("reader_instance_classes") or {}).get(
                resource)
    return None, None


def _fleet_fingerprint(envelope):
    """sha256 of the STRUCTURAL facts a plan's safety depends on.

    Transient status strings are deliberately excluded so a `backing-up` flap
    does not 409 a valid plan; instance classes are included so a resize —
    Aurora or standalone — changes the hash. Server-side only: never returned
    to a client, so there is no forgeable surface.
    """
    nodes = sorted(
        [str(row.get("id") or ""), bool(row.get("healthy")),
         str(row.get("instance_type") or "")]
        for row in _instances(envelope))
    databases = sorted(
        [str(row.get("identifier") or ""), str(row.get("kind") or ""),
         str(row.get("writer") or ""),
         sorted(str(reader) for reader in row.get("readers") or []),
         str(row.get("instance_class") or ""),
         str(row.get("writer_instance_class") or ""),
         sorted([str(key), str(value)] for key, value in
                (row.get("reader_instance_classes") or {}).items())]
        for row in envelope.get("databases") or [])
    caches = sorted(
        [str(row.get("identifier") or ""), int(row.get("replica_count") or 0),
         str(row.get("node_type") or ""), bool(row.get("cluster_enabled"))]
        for row in envelope.get("caches") or [])
    projection = {"mode": envelope.get("mode"), "nodes": nodes,
                  "databases": databases, "caches": caches}
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True).encode()).hexdigest()


def _refuse_degraded(envelope, actions):
    """A throttled describe must never validate — or fingerprint — a plan."""
    touched = set()
    for action in actions:
        touched.update(_BATCH_SECTIONS.get(action) or ())
    for warning in envelope.get("warnings") or []:
        if warning.get("code") in touched:
            raise CapacityError(
                f"AWS did not answer completely "
                f"({warning.get('iam_action') or warning.get('code')}), so "
                f"this plan cannot be checked against the fleet. Retry "
                f"shortly.",
                "report_degraded", 503)


def _refuse_step(index, message, error_code="invalid_request", status=400):
    raise CapacityError(f"Step {index + 1}: {message}", error_code, status,
                        {"step": index})


def _step_kind(step, envelope):
    action = step["action"]
    if action in (ACTION_ADD_NODE, ACTION_ADD_READER):
        return "add"
    if action in (ACTION_RESIZE_CACHE, ACTION_RESIZE_DATABASE):
        return "change"
    if action == ACTION_SET_CACHE_REPLICAS:
        row = _cache_row(envelope, step["resource"]) or {}
        current = int(row.get("replica_count") or 0)
        return "add" if step["params"]["count"] > current else "remove"
    return "remove"


def _step_rank(step):
    if step["action"] == ACTION_SET_CACHE_REPLICAS:
        return 2 if step["kind"] == "add" else 5
    return _BATCH_RANK[step["action"]]


def _order_steps(steps):
    """Stable-sort by rank, then pin each terminate behind its own drain."""
    ranked = sorted(steps, key=_step_rank)
    drains = {step["resource"] for step in ranked
              if step["action"] == ACTION_DRAIN_NODE}
    paired = {step["resource"]: step for step in ranked
              if step["action"] == ACTION_TERMINATE_NODE
              and step["resource"] in drains}
    ordered = []
    for step in ranked:
        if (step["action"] == ACTION_TERMINATE_NODE
                and paired.get(step["resource"]) is step):
            continue
        ordered.append(step)
        if step["action"] == ACTION_DRAIN_NODE and step["resource"] in paired:
            ordered.append(paired[step["resource"]])
    return ordered


def _validate_step(index, raw, envelope, planned):
    """One submitted step against the report, refusing what apply would.

    Plan-time validation is the offers gate plus report-level resource, param
    and cross-batch checks — full provider re-derivation stays at execution
    (the settled "not a reconciler" stance). ``planned`` is the normalized
    steps accepted so far, in submission order.
    """
    if not isinstance(raw, dict):
        _refuse_step(index, "each step must be an object naming an action")
    action = str(raw.get("action") or "").strip()
    if action in (ACTION_ENABLE_STABLE_IPS, ACTION_DISABLE_STABLE_IPS):
        _refuse_step(index,
                     "the stable-outbound-IPs switch is fleet-wide and holds "
                     "its own claim — run it alone through the single-action "
                     "apply, never inside a batch.")
    if action not in BATCH_ACTIONS:
        _refuse_step(index, f"unknown batch action '{action}'.")
    offer = (envelope.get("actions") or {}).get(action) or {}
    if not offer.get("offered"):
        _refuse_step(index,
                     f"{action} is not currently offered "
                     f"({offer.get('blocked_reason') or 'unavailable'}).",
                     offer.get("blocked_reason") or "not_offered", 409)
    resource = str(raw.get("resource") or "").strip()
    step = {"action": action, "resource": resource, "params": {}}

    if action == ACTION_ADD_NODE:
        step["resource"] = resource = ""
    elif action == ACTION_ADD_READER:
        if not any(row.get("identifier") == resource
                   for row in envelope.get("databases") or []):
            _refuse_step(index,
                         f"the report lists no database called "
                         f"{resource or '(missing)'}.",
                         "resource_not_found", 404)
    elif action == ACTION_REMOVE_READER:
        role, _class = _db_role(envelope, resource)
        if role != "reader":
            _refuse_step(index,
                         f"{resource or '(missing)'} is not a read replica in "
                         f"the report.", "not_a_reader", 409)
    elif action in (ACTION_SET_CACHE_REPLICAS, ACTION_RESIZE_CACHE):
        row = _cache_row(envelope, resource)
        if row is None:
            _refuse_step(index,
                         f"the report lists no cache group called "
                         f"{resource or '(missing)'}.",
                         "resource_not_found", 404)
        if row.get("blocked_reason"):
            _refuse_step(index, f"{resource} is blocked: "
                                f"{row['blocked_reason']}.",
                         row["blocked_reason"], 409)
    elif action == ACTION_RESIZE_DATABASE:
        role, _class = _db_role(envelope, resource)
        if role is None:
            _refuse_step(index,
                         f"the report lists no database instance called "
                         f"{resource or '(missing)'}.",
                         "resource_not_found", 404)
    else:
        row = _node_row(envelope, resource)
        if row is None:
            _refuse_step(index,
                         f"the report lists no registered node called "
                         f"{resource or '(missing)'}.",
                         "resource_not_found", 404)
        if resource == (envelope.get("nodes") or {}).get("self"):
            _refuse_step(index,
                         f"{resource} is the node answering this request; a "
                         f"batch cannot remove it.",
                         "cannot_remove_self", 409)

    if action not in (ACTION_ADD_NODE, ACTION_ADD_READER):
        if any(prior["action"] == action and prior["resource"] == resource
               for prior in planned):
            _refuse_step(index,
                         f"duplicate step — {action} on "
                         f"{resource or 'the fleet'} appears twice.")

    if action == ACTION_SET_CACHE_REPLICAS:
        row = _cache_row(envelope, resource)
        count = raw.get("count")
        if type(count) is not int:
            _refuse_step(index, "count must be a whole number.")
        if raw.get("apply_immediately") is not True:
            _refuse_step(index,
                         "apply_immediately must be true: ElastiCache applies "
                         "a replica-count change immediately and offers no "
                         "maintenance-window option.")
        if count < 0:
            _refuse_step(index, "count cannot be negative.")
        current = int(row.get("replica_count") or 0)
        if count == current:
            _refuse_step(index,
                         f"{resource} already has {current} replica(s).",
                         "no_change", 409)
        floor = int(row.get("min_replicas") or 0)
        if count < floor:
            _refuse_step(index,
                         f"{resource} has automatic failover enabled, which "
                         f"requires at least {floor} replica(s).",
                         elasticache_helper.FAILOVER_REQUIRES_REPLICA, 409)
        step["params"] = {"count": count, "apply_immediately": True}
    elif action in (ACTION_RESIZE_CACHE, ACTION_RESIZE_DATABASE):
        if raw.get("apply_immediately") is not True:
            _refuse_step(index,
                         "apply_immediately must be true: a resize is an "
                         "observed, immediate change with a settle check.")
        ladder = (provision_spec.CACHE_SIZES if action == ACTION_RESIZE_CACHE
                  else provision_spec.DB_SIZES)
        try:
            to_type = _resolve_size(ladder, raw.get("size"))
        except CapacityError as err:
            _refuse_step(index, err.message, err.error_code)
        if action == ACTION_RESIZE_CACHE:
            current_type = (_cache_row(envelope, resource) or {}).get(
                "node_type")
        else:
            _role, current_type = _db_role(envelope, resource)
        if current_type == to_type:
            _refuse_step(index, f"{resource} already runs {to_type}.",
                         "no_change", 409)
        step["params"] = {"size": str(raw.get("size")).strip().lower(),
                          "apply_immediately": True}

    _check_cross_batch(index, step, envelope, planned)
    step["kind"] = _step_kind(step, envelope)
    return step


def _check_cross_batch(index, step, envelope, planned):
    action, resource = step["action"], step["resource"]
    removes = (ACTION_REMOVE_READER, ACTION_DRAIN_NODE, ACTION_TERMINATE_NODE)
    resizes = (ACTION_RESIZE_CACHE, ACTION_RESIZE_DATABASE)

    if action == ACTION_DRAIN_NODE:
        # Only drains of currently-HEALTHY nodes consume the healthy budget —
        # exact parity with the page's removable() rule: an unhealthy node
        # drains freely, and at least one healthy node must remain.
        healthy = [row for row in _instances(envelope) if row.get("healthy")]
        if (_node_row(envelope, resource) or {}).get("healthy"):
            already = sum(
                1 for prior in planned
                if prior["action"] == ACTION_DRAIN_NODE
                and (_node_row(envelope, prior["resource"]) or {}).get(
                    "healthy"))
            if already + 1 >= len(healthy):
                _refuse_step(index,
                             "draining these nodes would leave no healthy "
                             "node serving — keep at least one.",
                             "last_healthy_target", 409)

    if action == ACTION_TERMINATE_NODE:
        drained_in_batch = any(
            prior["action"] == ACTION_DRAIN_NODE
            and prior["resource"] == resource for prior in planned)
        if not drained_in_batch:
            row = _node_row(envelope, resource) or {}
            state = str(row.get("state") or "")
            if row.get("healthy") or state == "initial" or "draining" in state:
                _refuse_step(index,
                             f"terminate_node needs a drain_node for "
                             f"{resource} earlier in this batch, or a node "
                             f"whose drain has already completed.",
                             "not_drained", 409)

    # Resizing a resource another step in this batch removes is never what
    # the operator meant — refused whichever order the two were submitted in.
    if action in resizes and any(
            prior["action"] in removes and prior["resource"] == resource
            for prior in planned):
        _refuse_step(index,
                     f"this batch both resizes and removes {resource}.",
                     "conflicting_steps", 409)
    if action in removes and any(
            prior["action"] in resizes and prior["resource"] == resource
            for prior in planned):
        _refuse_step(index,
                     f"this batch both resizes and removes {resource}.",
                     "conflicting_steps", 409)


def _describe_step(step, envelope):
    """Plain-English ``(description, warnings)`` — the server's own words."""
    action, resource = step["action"], step["resource"]
    if action == ACTION_ADD_NODE:
        return ("Add an app node",
                ["builds, deploys and proves itself before serving · "
                 "20–40 min"])
    if action == ACTION_ADD_READER:
        return (f"Add a read replica to {resource}",
                ["can take up to an hour to come online"])
    if action == ACTION_SET_CACHE_REPLICAS:
        row = _cache_row(envelope, resource) or {}
        current = int(row.get("replica_count") or 0)
        return (f"Change {resource} replicas {current} → "
                f"{step['params']['count']}",
                ["applies immediately — ElastiCache has no maintenance-window "
                 "option"])
    if action == ACTION_RESIZE_CACHE:
        row = _cache_row(envelope, resource) or {}
        to_type = _resolve_size(provision_spec.CACHE_SIZES,
                                step["params"]["size"])
        note = ("rolls replicas first, then a brief failover — one short "
                "interruption"
                if row.get("resize_impact") == "rolling" else
                "no replica: the cache is down while its node is replaced")
        return f"Resize {resource} to {to_type}", [note]
    if action == ACTION_RESIZE_DATABASE:
        role, _class = _db_role(envelope, resource)
        to_class = _resolve_size(provision_spec.DB_SIZES,
                                 step["params"]["size"])
        if role == "writer":
            return (f"Resize writer {resource} to {to_class}",
                    ["~minutes offline while the writer changes class"])
        return (f"Resize reader {resource} to {to_class}",
                ["reads keep flowing; this reader pauses while it changes "
                 "class"])
    if action == ACTION_REMOVE_READER:
        return (f"Remove read replica {resource}",
                ["deleted with no final snapshot"])
    row = _node_row(envelope, resource) or {}
    name = row.get("name") or resource
    if action == ACTION_DRAIN_NODE:
        return f"Drain {name} — traffic moves off it first", []
    return f"Terminate {name}", []


def _price(instance_type):
    return provision_spec.COST_TABLE.get(instance_type) if instance_type \
        else None


def _step_cost(step, envelope):
    """``(monthly_delta_usd, warnings)``. An unpriced type is an honest None
    plus a warning — never a silent $0 folded into the total."""
    action, resource = step["action"], step["resource"]
    if action == ACTION_DRAIN_NODE:
        return 0.0, []
    if action == ACTION_ADD_NODE:
        healthy = [row for row in _instances(envelope) if row.get("healthy")]
        itype = healthy[0].get("instance_type") if healthy else None
        price = _price(itype)
        if price is None:
            return None, [f"no listed price for {itype or 'this node type'}"]
        return price, []
    if action == ACTION_TERMINATE_NODE:
        itype = (_node_row(envelope, resource) or {}).get("instance_type")
        price = _price(itype)
        if price is None:
            return None, [f"no listed price for {itype or 'this node type'}"]
        return -price, []
    if action == ACTION_ADD_READER:
        row = next((r for r in envelope.get("databases") or []
                    if r.get("identifier") == resource), {})
        cls = row.get("writer_instance_class") or row.get("instance_class")
        price = _price(cls)
        if price is None:
            return None, [f"no listed price for {cls or 'this class'}"]
        return price, []
    if action == ACTION_REMOVE_READER:
        _role, cls = _db_role(envelope, resource)
        price = _price(cls)
        if price is None:
            return None, [f"no listed price for {cls or 'this class'}"]
        return -price, []
    if action == ACTION_SET_CACHE_REPLICAS:
        row = _cache_row(envelope, resource) or {}
        node_type = row.get("node_type")
        price = _price(node_type)
        if price is None:
            return None, [f"no listed price for {node_type or 'this node type'}"]
        delta = step["params"]["count"] - int(row.get("replica_count") or 0)
        return delta * price, []
    if action == ACTION_RESIZE_CACHE:
        row = _cache_row(envelope, resource) or {}
        from_type = row.get("node_type")
        to_type = _resolve_size(provision_spec.CACHE_SIZES,
                                step["params"]["size"])
        # Node type is group-wide, so the delta applies to every member.
        members = len(row.get("members") or []) or (
            int(row.get("replica_count") or 0) + 1)
        from_price, to_price = _price(from_type), _price(to_type)
        if from_price is None or to_price is None:
            missing = from_type if from_price is None else to_type
            return None, [f"no listed price for {missing or 'this node type'}"]
        return (to_price - from_price) * members, []
    _role, from_class = _db_role(envelope, resource)
    to_class = _resolve_size(provision_spec.DB_SIZES, step["params"]["size"])
    from_price, to_price = _price(from_class), _price(to_class)
    if from_price is None or to_price is None:
        missing = from_class if from_price is None else to_class
        return None, [f"no listed price for {missing or 'this class'}"]
    return to_price - from_price, []


def _iso_in(seconds):
    import datetime
    from mojo.helpers import dates
    return (dates.utcnow() + datetime.timedelta(seconds=seconds)).isoformat()


def plan_batch(actor, steps):
    """Validate, order, word and price an ordered set of capacity steps.

    Reads the CACHED report on purpose: the page re-plans on every debounced
    stepper tweak, and a full AWS describe sweep per keystroke would
    rate-limit the account for an answer at most REPORT_TTL old. Correctness
    is unaffected — execution re-derives every guard, and the apply-time
    fingerprint is fresh regardless.
    """
    _refuse_external("Planning capacity changes")
    if not isinstance(steps, (list, tuple)) or not steps:
        raise CapacityError("steps must be a non-empty list of capacity steps",
                            "invalid_request")
    if len(steps) > MAX_BATCH_STEPS:
        raise CapacityError(
            f"A batch holds at most {MAX_BATCH_STEPS} steps; this one has "
            f"{len(steps)}.", "invalid_request")
    envelope = report()
    _refuse_degraded(envelope, [str((raw or {}).get("action") or "")
                                for raw in steps
                                if isinstance(raw, dict)])
    normalized = []
    for index, raw in enumerate(steps):
        normalized.append(_validate_step(index, raw, envelope, normalized))
    ordered = _order_steps(normalized)
    total = 0.0
    complete = True
    for index, step in enumerate(ordered):
        step["index"] = index
        description, notes = _describe_step(step, envelope)
        delta, cost_notes = _step_cost(step, envelope)
        step["description"] = description
        step["warnings"] = notes + cost_notes
        step["monthly_delta_usd"] = None if delta is None else round(delta, 2)
        if delta is None:
            complete = False
        else:
            total += delta
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "created": _now(),
        "expires_in": PLAN_TTL,
        "expires_at": _iso_in(PLAN_TTL),
        "actor": getattr(actor, "pk", None),
        "fingerprint": _fleet_fingerprint(envelope),
        "steps": ordered,
        "total_monthly_delta_usd": round(total, 2),
        "estimate_complete": complete,
        "order_note": ORDER_NOTE,
    }
    try:
        cache.set(_plan_key(record["id"]), record, PLAN_TTL)
    except Exception:
        # A plan that cannot be stored cannot be confirmed — refusal, never
        # go-ahead, same stance as _claim.
        raise CapacityError(
            "The coordination cache is unavailable, so this plan could not "
            "be stored for confirmation. Try again shortly.",
            "cache_unavailable", 503) from None
    return {key: value for key, value in record.items()
            if key != "fingerprint"}


def apply_batch(actor, plan_id):
    """Confirm one stored plan by id and hand its steps to the batch job."""
    _refuse_external("Applying capacity changes")
    plan_id = str(plan_id or "").strip()
    record = None
    if plan_id:
        try:
            record = cache.get(_plan_key(plan_id))
        except Exception:
            record = None
    if not isinstance(record, dict):
        raise CapacityError(
            "That plan is not on record — plans expire after 5 minutes. "
            "Request a new plan and review it again.",
            "plan_not_found", 404)
    fresh = report(refresh=True)
    # Never fingerprint an incomplete fleet: a degraded fresh envelope would
    # hash differently for the wrong reason and 409 an honest plan.
    _refuse_degraded(fresh, [step["action"] for step in record["steps"]])
    if _fleet_fingerprint(fresh) != record.get("fingerprint"):
        raise CapacityError(
            "The fleet changed since this plan was written. Request a new "
            "plan and review it again.",
            "plan_stale", 409)
    batch_id = str(uuid.uuid4())
    # Atomic single-use: `add` either claims the plan for THIS batch or names
    # the batch that already claimed it, so a double-click converges on
    # polling, never on a second batch.
    try:
        acquired = cache.add(_plan_lock_key(plan_id), batch_id, CLAIM_TTL)
    except Exception:
        raise CapacityError(
            "The coordination cache is unavailable, so a second apply of "
            "this plan cannot be ruled out. Try again shortly.",
            "cache_unavailable", 503) from None
    if not acquired:
        try:
            existing = cache.get(_plan_lock_key(plan_id))
        except Exception:
            existing = None
        if existing:
            raise CapacityError(
                "This plan was already applied. Poll the running batch "
                "instead of starting a second one.",
                "plan_already_applied", 409, {"batch": existing})
        raise CapacityError(
            "The coordination cache is unavailable, so a second apply of "
            "this plan cannot be ruled out. Try again shortly.",
            "cache_unavailable", 503)
    steps = [{
        "index": step["index"], "action": step["action"],
        "resource": step["resource"],
        "params": dict(step.get("params") or {}),
        "description": step.get("description"), "kind": step.get("kind"),
        "state": "pending", "operation": None, "phase": None,
        "message": None, "error_code": None,
    } for step in record["steps"]]
    budget = sum(_deadline_for(step["action"]) for step in steps) \
        + 120 * len(steps)
    batch = {
        "schema_version": SCHEMA_VERSION,
        "id": batch_id,
        "plan_id": plan_id,
        "actor": getattr(actor, "pk", None),
        "state": STATE_RUNNING,
        "started": _now(),
        "current_index": 0,
        "message": "requested",
        "error_code": None,
        "steps": steps,
        "ttl": budget + CLAIM_TTL,
    }
    _write_batch(batch)
    from mojo.apps import jobs
    try:
        jobs.publish(
            func="mojo.apps.aws.asyncjobs.capacity_batch",
            payload={"batch": batch_id},
            channel="cleanup", max_retries=0,
            # Stored but unenforced by the job engine — operator metadata,
            # not a kill switch. The runner's per-step ceilings are the clock.
            max_exec_seconds=budget)
    except Exception:
        logger.exception("capacity: batch %s could not be dispatched",
                         batch_id)
        batch["state"] = STATE_FAILED
        batch["error_code"] = "batch_dispatch_failed"
        batch["message"] = ("The batch could not be handed to a job runner, "
                            "so nothing was started.")
        for step in batch["steps"]:
            step["state"] = "not_attempted"
        _write_batch(batch)
        raise CapacityError(
            "This installation's job runners did not accept the batch, so "
            "nothing was changed.", "batch_dispatch_failed", 503) from None
    logger.info("capacity batch %s dispatched plan=%s steps=%d actor=%s",
                batch_id, plan_id, len(steps), batch["actor"])
    # The stored record keeps its TTL; the wire answer, like batch_status,
    # does not carry bookkeeping.
    return {key: value for key, value in batch.items() if key != "ttl"}


def batch_status(batch_id):
    """One batch's recorded progress, or a 404. A pure READ, like
    operation_status — polling must never advance the work."""
    record = _read_batch(str(batch_id or "").strip())
    if record is None:
        raise CapacityError(
            "That capacity batch is not on record. It finished long ago, or "
            "the coordination cache was cleared.",
            "batch_not_found", 404)
    result = {key: value for key, value in record.items() if key != "ttl"}
    result["stalled"] = _batch_stalled(record)
    return result


def _batch_stalled(record):
    if record.get("state") != STATE_RUNNING:
        return False
    from mojo.helpers import dates
    try:
        updated = dates.parse_datetime(record.get("updated"))
        age = (dates.utcnow() - updated).total_seconds()
    except Exception:
        return False
    return age > BATCH_STALL_AFTER


def _step_ceiling(action):
    """A generous BACKSTOP above the child's own deadline — never the clock.

    A ceiling that undercuts a legitimately slow child converts a succeeding
    mutation into a reported failure, the worst lie this API could tell. The
    add_node extra: _deadline_for budgets only PROOF_MARGIN for the proving
    leg, but the child actually waits canary_timeout() + PROOF_MARGIN.
    """
    ceiling = _deadline_for(action) + 900
    if action == ACTION_ADD_NODE:
        try:
            from mojo.apps.edge.services import deploy
            ceiling += int(deploy.canary_timeout())
        except Exception:
            ceiling += 900
    return ceiling


def _audit_batch_step(actor, step, operation_id):
    """The per-step audit row the REST layer would have written.

    Single-action applies audit in the REST handler; the batch runner calls
    the service directly, so it writes the same row itself, with the same
    untainted action names. Function-level imports on purpose: rest.capacity
    imports this module at module level.
    """
    try:
        from mojo.apps.account.services import admin_platform
        from mojo.apps.aws.rest.capacity import AUDIT_ACTIONS
        admin_platform.audit_after_commit(
            actor, AUDIT_ACTIONS.get(step["action"], "aws_capacity"),
            f"{step.get('resource') or 'fleet'}:{operation_id}")
    except Exception:
        logger.exception("capacity: batch step audit could not be written")


def _follow_operation(record, step, operation_id):
    """Poll one child operation to a terminal state, copying its progress.

    Copies phase/message onto the step each tick so ONE batch poll answers
    everything and `updated` stays fresh. Returns the terminal record, the
    still-running record at the backstop ceiling, or None when the child's
    record vanished from the cache.
    """
    deadline = time.time() + _step_ceiling(step["action"])
    misses = 0
    live = None
    while time.time() < deadline:
        _sleep(POLL_INTERVAL)
        live = _read_operation(operation_id)
        if live is None:
            # Tolerate a transient cache blip; sustained absence means the
            # record is genuinely gone.
            misses += 1
            if misses >= 3:
                return None
            continue
        misses = 0
        step["phase"] = live.get("phase")
        step["message"] = live.get("message")
        _write_batch(record)
        if live.get("state") != STATE_RUNNING:
            return live
    return live


def _fail_batch(record, position, error_code, message):
    steps = record.get("steps") or []
    step = steps[position]
    step["state"] = STATE_FAILED
    step["error_code"] = str(error_code)
    step["message"] = str(message)
    remaining = len(steps) - position - 1
    for later in steps[position + 1:]:
        later["state"] = "not_attempted"
        later["message"] = "not attempted — an earlier step failed"
    record["state"] = STATE_FAILED
    record["error_code"] = str(error_code)
    tail = (f"; the remaining {remaining} step(s) were not attempted."
            if remaining else "")
    record["message"] = (f"Step {position + 1} of {len(steps)} failed: "
                         f"{message}{tail}")
    logger.warning("capacity batch %s failed at step %s code=%s",
                   record.get("id"), position, error_code)
    _write_batch(record)
    return STATE_FAILED


def run_batch(batch_id):
    """The batch job body: each step is an UNCHANGED single-action apply.

    apply() claims, re-derives its guards from fresh AWS reads, records a
    child operation and dispatches its own job — identically to a manual
    click. This runner only sequences, mirrors progress, audits, and stops at
    the first failure (no rollback; later steps are not attempted). No claims
    of its own: each child releases its own via _finish/_fail.
    """
    from objict import objict

    record = _read_batch(batch_id)
    if record is None:
        logger.warning("capacity: batch %s vanished before it ran", batch_id)
        return "missing"
    if record.get("state") != STATE_RUNNING:
        return str(record.get("state"))
    actor = objict(pk=record.get("actor"))
    steps = record.get("steps") or []
    for position, step in enumerate(steps):
        record["current_index"] = position
        step["state"] = STATE_RUNNING
        step["message"] = "requested"
        record["message"] = (f"step {position + 1} of {len(steps)}: "
                             f"{step.get('description') or step['action']}")
        _write_batch(record)
        try:
            child = apply(actor, step["action"], step.get("resource") or "",
                          **(step.get("params") or {}))
        except CapacityError as err:
            return _fail_batch(record, position, err.error_code, err.message)
        except Exception:
            logger.exception("capacity: batch %s step %s raised",
                             batch_id, position)
            return _fail_batch(record, position, "operation_failed",
                               "This step stopped with an unexpected error.")
        operation_id = child.get("id")
        step["operation"] = operation_id
        _write_batch(record)
        _audit_batch_step(actor, step, operation_id)
        final = _follow_operation(record, step, operation_id)
        if final is None:
            return _fail_batch(
                record, position, "operation_vanished",
                f"The step's operation record vanished from the coordination "
                f"cache before it finished. Check the AWS console; the "
                f"operation id was {operation_id}.")
        if final.get("state") == STATE_RUNNING:
            return _fail_batch(
                record, position, "operation_timeout",
                f"The step gave no terminal answer within the batch's "
                f"backstop ceiling. It may still be working — poll "
                f"?operation={operation_id} and check the AWS console.")
        if final.get("state") != STATE_DONE:
            return _fail_batch(
                record, position,
                final.get("error_code") or "operation_failed",
                final.get("message") or "the operation failed")
        step["state"] = STATE_DONE
        step["phase"] = "complete"
        step["message"] = final.get("message")
        _write_batch(record)
    record["state"] = STATE_DONE
    record["message"] = f"All {len(steps)} step(s) completed."
    _write_batch(record)
    return STATE_DONE


# ── the operation job ───────────────────────────────────────────────────────

def run_operation(operation_id):
    """Drive one recorded operation to a proven steady state, or a named failure."""
    record = _read_operation(operation_id)
    if record is None:
        logger.warning("capacity: operation %s vanished before it ran", operation_id)
        return "missing"
    if record.get("state") != STATE_RUNNING:
        return str(record.get("state"))
    runners = {
        ACTION_ADD_NODE: _run_add_node,
        ACTION_DRAIN_NODE: _run_drain_node,
        ACTION_TERMINATE_NODE: _run_terminate_node,
        ACTION_ADD_READER: _run_add_reader,
        ACTION_REMOVE_READER: _run_remove_reader,
        ACTION_SET_CACHE_REPLICAS: _run_set_cache_replicas,
        ACTION_ENABLE_STABLE_IPS: _run_enable_stable_ips,
        ACTION_DISABLE_STABLE_IPS: _run_disable_stable_ips,
        ACTION_RESIZE_CACHE: _run_resize_cache,
        ACTION_RESIZE_DATABASE: _run_resize_database,
    }
    runner = runners.get(record.get("action"))
    if runner is None:
        _fail(record, "invalid_request", "Unknown capacity action.")
        return "invalid"
    try:
        runner(record)
    except ProviderCallError as err:
        detail = err.detail()
        _fail(record,
              "provider_denied" if err.denied else "provider_error",
              "AWS refused or could not complete this change.",
              # Mutation state unknown means a retry could double-apply, so the
              # claim is deliberately held until its TTL.
              hold_claim=err.mutation_state != "none",
              failure=detail)
    except Exception:
        logger.exception("capacity: operation %s raised", operation_id)
        _fail(record, "operation_failed",
              "This operation stopped with an unexpected error. Check the "
              "capacity report before retrying.", hold_claim=True)
    return str((_read_operation(operation_id) or {}).get("state") or STATE_FAILED)


def _sleep(seconds=POLL_INTERVAL):
    time.sleep(seconds)


def _run_add_node(record):
    from mojo.apps.edge.services import deploy, platform_deploy

    detail = record["detail"]
    source_id = detail["source_instance"]
    source = ec2_helper.instance_facts(source_id)
    if source is None:
        return _fail(record, "no_source_node",
                     "The node chosen as the clone source no longer exists.")

    # ── capturing ──────────────────────────────────────────────────────────
    reusable = ec2_helper.find_reusable_image(IMAGE_TAG_VALUE, _image_max_age_days())
    if reusable:
        image_id = reusable["image_id"]
        _advance(record, "capturing", "reusing a recent fleet image",
                 image_id=image_id, image_reused=True,
                 image_age_days=reusable.get("age_days"))
    else:
        stamp = _now().replace(":", "").replace("-", "")[:15]
        image_id = ec2_helper.capture_image(
            source_id, f"mojo-fleet-{stamp}", IMAGE_TAG_VALUE)
        _advance(record, "capturing",
                 "capturing an image of the source node (no reboot)",
                 image_id=image_id, image_reused=False)
        deadline = time.time() + IMAGE_TIMEOUT
        while time.time() < deadline:
            _sleep()
            status = ec2_helper.image_status(image_id) or {}
            if status.get("state") == "available":
                break
            if status.get("state") in ("failed", "error", "invalid"):
                return _fail(record, "image_failed",
                             "AWS could not build an image of the source node. "
                             "Nothing was launched.")
        else:
            return _fail(record, "image_timeout",
                         f"The image was still building after "
                         f"{IMAGE_TIMEOUT // 60} minutes. Nothing was launched.")

    # ── launching ──────────────────────────────────────────────────────────
    base = detail.get("source_name") or source.get("name") or "mojo-node"
    _advance(record, "launching", "launching the new node")
    # Clones carry the fleet's identity from birth: the source's
    # mojo:project/mojo:env (and managed-by, when present) ride onto the
    # launch tags beside the created-by stamp, so discovery (spec.owns) and
    # the terminate guard's identity anchor both recognize the clone even if
    # the stamp is ever lost.
    identity = {key: value
                for key, value in (source.get("tags") or {}).items()
                if key in FLEET_IDENTITY_TAGS + ("managed-by",) and value}
    instance_id = ec2_helper.launch_clone(
        source, image_id, source.get("subnet_id"),
        name=f"{base}-clone",
        user_data=node_user_data(base),
        tags=identity)
    if not instance_id:
        return _fail(record, "launch_failed",
                     "AWS accepted the launch but named no instance.")
    node_id = expected_node_id(base, instance_id)
    runner_id = expected_runner_id(node_id)
    _advance(record, "launching", f"launched {instance_id}",
             instance_id=instance_id, node_id=node_id, runner_id=runner_id)
    invalidate()

    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        _sleep()
        facts = ec2_helper.instance_facts(instance_id) or {}
        if facts.get("state") == "running":
            break
        if facts.get("state") in ("terminated", "shutting-down"):
            return _fail(record, "launch_failed",
                         f"{instance_id} stopped before it finished booting.")
    else:
        return _fail(record, "launch_timeout",
                     f"{instance_id} did not reach the running state. It exists "
                     f"and is NOT registered behind the balancer.")

    # ── booting ────────────────────────────────────────────────────────────
    _advance(record, "booting",
             f"waiting for {runner_id} to join the edge channel")
    if not _await_runner(runner_id, RUNNER_TIMEOUT):
        return _fail(record, "runner_missing",
                     f"{instance_id} is running but never joined the job fleet "
                     f"as {runner_id}. It is NOT registered behind the "
                     f"balancer, so it is serving nothing.")

    # ── converging ─────────────────────────────────────────────────────────
    row = platform_deploy.last_converged_deployment()
    if row is None:
        return _fail(record, "no_converged_deployment",
                     f"{instance_id} joined the fleet, but no deployment has "
                     f"ever converged here, so there is no proven commit to "
                     f"put on it. It is NOT registered.")
    _advance(record, "converging",
             f"deploying the fleet's last converged commit {row.sha[:12]}",
             sha=row.sha, deployment=str(row.pk))
    from mojo.apps.edge.asyncjobs import _publish_deploy_node
    # ONE targeted publish, to the NEW runner's own box-direct channel. Never
    # DEPLOY_CHANNEL: that is a fleet-wide broadcast, and redeploying the whole
    # fleet because one node was added is precisely the blast radius this
    # feature exists to avoid.
    _publish_deploy_node(runner_id, row.sha, row.framework_version,
                         migrate=False, deployment_id=row.pk)

    # ── proving ────────────────────────────────────────────────────────────
    _advance(record, "proving",
             "waiting for the new node to prove it is running that commit")
    if not _await_proof(runner_id, row, deploy.canary_timeout() + PROOF_MARGIN):
        return _fail(record, "proof_timeout",
                     f"{instance_id} did not prove it is running {row.sha[:12]}. "
                     f"It has deliberately NOT been registered behind the "
                     f"balancer — an unproven node must not serve traffic.")

    # ── addressing ─────────────────────────────────────────────────────────
    # Read the policy through the RAISING path: this is an admission gate, and
    # a database blip that silently skipped it would register a node whose
    # egress no provider has allowlisted. Unreadable fails the add instead.
    try:
        egress_on = _egress_enabled(strict=True)
    except Exception:
        logger.exception("capacity: stable-ips policy could not be read")
        return _fail(record, "policy_unreadable",
                     f"The stable-outbound-ips policy could not be read, so "
                     f"{instance_id} was NOT registered — a node whose egress "
                     f"nobody allowlisted must not serve provider traffic. It "
                     f"is running and unregistered; retry, or Terminate it.")
    if egress_on:
        _advance(record, "addressing",
                 "attaching the fleet's stable outbound address")
        try:
            view = _address_view([instance_id], ec2_helper.address_map())
            if view["pending"]:
                _, stable_ip = _attach_stable_ip(instance_id, node_id, view)
            else:
                stable_ip = (view["attached"][0] or {}).get("public_ip")
        except ProviderCallError as err:
            code = ("address_quota"
                    if err.provider_code == "AddressLimitExceeded"
                    else "address_failed")
            return _fail(record, code,
                         f"{instance_id} could not be given a stable outbound "
                         f"address, so it was NOT registered — a node whose "
                         f"egress no provider has allowlisted must not serve. "
                         f"It is running and unregistered; fix what the "
                         f"failure names, then retry the add or Terminate it.",
                         failure=err.detail())
        _advance(record, "addressing", f"stable address {stable_ip} attached",
                 stable_ip=stable_ip)
        invalidate()

    # ── registering ────────────────────────────────────────────────────────
    groups = detail.get("target_groups") or []
    _advance(record, "registering", "registering the proven node behind the balancer")
    for group in groups:
        elbv2_helper.register_target(group["arn"], instance_id, group.get("port"))
    invalidate()
    _extend_topology(record, node_id)

    # ── settling ───────────────────────────────────────────────────────────
    _advance(record, "settling", "waiting for the balancer to report it healthy")
    if not _await_healthy(instance_id, [group["arn"] for group in groups],
                          HEALTH_TIMEOUT):
        return _fail(record, "never_healthy",
                     f"{instance_id} was registered but the balancer never "
                     f"reported it healthy. Drain it and investigate before "
                     f"leaving it in the serving path.")
    _converge_pools(record)
    return _finish(record,
                   f"{instance_id} is serving as {node_id}",
                   healthy=True)


def _await_runner(runner_id, timeout):
    from mojo.apps import jobs
    deadline = time.time() + timeout
    while time.time() < deadline:
        _sleep()
        try:
            rows = jobs.get_runners_bounded(channel="edge", timeout=1.0) or []
        except Exception:
            continue
        for row in rows:
            if row.get("runner_id") == runner_id and row.get("alive"):
                return True
    return False


def _await_proof(runner_id, row, timeout):
    from mojo.apps.edge.services import platform_deploy
    from mojo.apps.jobs.manager import get_manager

    deadline = time.time() + timeout
    manager = get_manager()
    while time.time() < deadline:
        _sleep()
        try:
            response = manager.execute_on_runner(
                runner_id, "mojo.apps.edge.services.readiness.local_node_proof",
                {"deployment": str(row.pk), "sha": row.sha}, timeout=2.0)
        except Exception:
            continue
        if not isinstance(response, dict) or response.get("status") != "success":
            continue
        if platform_deploy.proof_matches(row, response.get("result")):
            return True
    return False


def _await_healthy(instance_id, group_arns, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _sleep()
        try:
            states = []
            for arn in group_arns:
                states += [target.get("state") for target in
                           elbv2_helper.target_health(arn, instance_id)]
        except ProviderCallError:
            continue
        if states and all(state == "healthy" for state in states):
            return True
    return False


def _extend_topology(record, node_id):
    """Add the new node to EDGE_EXPECTED_TOPOLOGY. Extend-only, never fatal.

    A node that is serving traffic is serving traffic whether or not a settings
    row lists it. Failing the add here would leave a proven, registered,
    healthy node reported as a failure — so this degrades to a warning the
    panel shows, with the one manual step spelled out.

    Only an EXISTING topology is extended. Writing one where none was
    configured would newly constrain a fleet that had deliberately left
    readiness to derive its own per-node view.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings

    try:
        current = system_settings.get_value(
            system_settings.EXPECTED_EDGE_TOPOLOGY, None)
        if not isinstance(current, dict) or not current.get("nodes"):
            return _warn(record, "topology_not_updated",
                         "EDGE_EXPECTED_TOPOLOGY is not configured, so nothing "
                         "was extended. Fleet readiness derives its own view.")
        nodes = sorted(set(current.get("nodes") or []) | {node_id})
        if nodes == sorted(set(current.get("nodes") or [])):
            return record
        actor_pk = record.get("actor")
        actor = User.objects.filter(pk=actor_pk).first() if actor_pk else None
        system_settings.set_value(
            actor, system_settings.EXPECTED_EDGE_TOPOLOGY,
            {"nodes": nodes, "pools": list(current.get("pools") or [])})
        record["detail"]["topology_nodes"] = nodes
        return _write_operation(record)
    except Exception:
        logger.exception("capacity: topology extend failed for %s", node_id)
        return _warn(record, "topology_not_updated",
                     f"The node is serving, but EDGE_EXPECTED_TOPOLOGY could "
                     f"not be extended. Add {node_id} to it by hand so fleet "
                     f"readiness expects it.")


def _converge_pools(record):
    """Kick the existing combined pool convergence so readiness settles green.

    Not a new mechanism — the same sweep the ten-minute cron runs. Right after
    an add, the fleet summary reads `pending` until it completes, which is
    expected and which the panel says out loud.
    """
    try:
        from mojo.apps.edge import cronjobs
        cronjobs.converge_edge()
    except Exception:
        logger.exception("capacity: pool convergence could not be triggered")
        _warn(record, "convergence_not_triggered",
              "The node is serving, but the vhost convergence sweep could not "
              "be queued. The scheduled sweep will pick it up.")


def _stable_ip_tags(name):
    return {"Name": str(name or "mojo-node"),
            STABLE_EIP_TAG: STABLE_EIP_TAG_VALUE,
            ec2_helper.CREATED_BY_TAG: "admin-capacity"}


def _attach_stable_ip(instance_id, name, view, assignments=None):
    """Give ONE node an Elastic IP: assigned, else reserved, else allocated.

    Mutates ``view['reserved']`` as reservations are consumed so one planning
    read serves a whole fleet loop. ``Resource.AlreadyAssociated`` (a raced
    reservation — a concurrent add_node leg, a second admin) is retried ONCE
    with a fresh allocation; ``AllowReassociation=False`` means the loser of
    that race errors instead of stealing, which is the point.
    """
    allocation = (assignments or {}).get(instance_id)
    public_ip = None
    if allocation:
        row = next((r for r in view["unassociated"]
                    if r.get("allocation_id") == allocation), None)
        if row is None:
            # The named reservation stopped being free between the
            # request-time check and this fresh read. Never tag what may now
            # be somebody else's — drop the assignment and fall through.
            allocation = None
        else:
            public_ip = row.get("public_ip")
            ec2_helper.tag_resources([allocation], _stable_ip_tags(name))
    if allocation is None:
        if view["reserved"]:
            chosen = view["reserved"].pop(0)
            allocation = chosen.get("allocation_id")
            public_ip = chosen.get("public_ip")
            ec2_helper.tag_resources([allocation], _stable_ip_tags(name))
        else:
            allocated = ec2_helper.allocate_address(_stable_ip_tags(name))
            allocation = allocated.get("allocation_id")
            public_ip = allocated.get("public_ip")
    try:
        ec2_helper.associate_address(allocation, instance_id)
    except ProviderCallError as err:
        if err.provider_code != "Resource.AlreadyAssociated":
            raise
        allocated = ec2_helper.allocate_address(_stable_ip_tags(name))
        allocation = allocated.get("allocation_id")
        public_ip = allocated.get("public_ip")
        ec2_helper.associate_address(allocation, instance_id)
    return allocation, public_ip


def _fail_released(record, err, message):
    """A provider failure on a stable-ips runner: fail WITHOUT holding the claim.

    The generic handler in ``run_operation`` holds the claim whenever a
    mutation was attempted, which is right for add_node — a retried add is a
    second live instance — and wrong here: both stable-ips runners are
    idempotent by construction (satisfied nodes are skipped, decisions
    re-derive from fresh reads, and even an allocate whose response was lost
    left a tagged unassociated address the next planning pass reuses first).
    Holding would 409 the exact re-run the panel offers as the retry, for the
    rest of the 90-minute claim TTL.
    """
    return _fail(record,
                 "provider_denied" if err.denied else "provider_error",
                 message, failure=err.detail())


def _run_enable_stable_ips(record):
    try:
        return _enable_stable_ips(record)
    except ProviderCallError as err:
        return _fail_released(
            record, err,
            "AWS refused or could not complete the address change. Fix what "
            "the failure names and run the enable again — it converges only "
            "what is still missing.")


def _enable_stable_ips(record):
    serving = _serving()
    fleet = _fleet_ids(serving)
    if not fleet:
        return _fail(record, "no_fleet_nodes",
                     "No node is registered behind a load balancer any more, "
                     "so there is nothing to give a stable address to.")
    facts = ec2_helper.instance_map(fleet)
    running = [identifier for identifier in fleet
               if (facts.get(identifier) or {}).get("state") == "running"]
    for identifier in fleet:
        if identifier not in running:
            _warn(record, "node_not_running",
                  f"{identifier} is registered but not running; it was left "
                  f"without a stable address.")
    if not running:
        return _fail(record, "no_fleet_nodes",
                     "No registered node is running, so no address was "
                     "attached.")
    _advance(record, "planning",
             f"planning stable addresses for {len(running)} node(s)")
    view = _address_view(running, ec2_helper.address_map())

    # Adoption is TAG-SCOPED: a node-attached address is claimed for this
    # feature only when it already carries django-mojo ownership tags (the
    # pre-balancer provision case). An untagged attached address satisfies the
    # node — it IS a stable address — but stays foreign: never tagged, never
    # renamed, never detached by disable.
    for row in view["attached"]:
        if row.get("managed"):
            continue
        tags = (view["by_instance"].get(row["instance"]) or {}).get("tags")
        if _django_mojo_tagged(tags):
            name = (facts.get(row["instance"]) or {}).get("name") or row["instance"]
            ec2_helper.tag_resources([row["allocation_id"]],
                                     _stable_ip_tags(name))
            row["managed"] = True

    assignments = record["detail"].get("assign") or {}
    view["reserved"] = [row for row in view["reserved"]
                        if row.get("allocation_id") not in assignments.values()]
    view["reserved"].sort(key=lambda row: str(row.get("allocation_id")))
    done = []
    for instance in list(view["pending"]):
        name = (facts.get(instance) or {}).get("name") or instance
        _advance(record, "associating",
                 f"attaching a stable address to {name}")
        try:
            allocation, public_ip = _attach_stable_ip(
                instance, name, view, assignments)
        except ProviderCallError as err:
            if err.provider_code == "AddressLimitExceeded":
                remaining = [row for row in view["pending"]
                             if row not in done and row != instance] + [instance]
                return _fail(
                    record, "address_quota",
                    f"AWS refused a new Elastic IP: the account's public IPv4 "
                    f"quota is exhausted. {len(done)} node(s) were addressed; "
                    f"{len(remaining)} still need one. Raise the EC2 Elastic "
                    f"IP quota (default 5 per region) and run the enable "
                    f"again.", failure=err.detail())
            raise
        done.append(instance)
        _advance(record, "associating",
                 f"{name} now holds {public_ip}", last_attached=public_ip)

    _advance(record, "verifying",
             "re-reading AWS to prove every node holds its address")
    final = _address_view(running, ec2_helper.address_map())
    if final["pending"]:
        return _fail(record, "address_unverified",
                     f"AWS still reports no Elastic IP on: "
                     f"{', '.join(final['pending'])}. Run the enable again to "
                     f"converge them.")
    allowlist = sorted({row.get("public_ip") for row in final["attached"]
                        if row.get("public_ip")})
    return _finish(record,
                   f"Stable outbound IPs are on. Give providers these "
                   f"addresses: {', '.join(allowlist)}",
                   addresses=allowlist)


def _run_disable_stable_ips(record):
    try:
        return _disable_stable_ips(record)
    except ProviderCallError as err:
        return _fail_released(
            record, err,
            "AWS refused or could not complete the detach. Run the disable "
            "again — it detaches only what is still attached.")


def _disable_stable_ips(record):
    serving = _serving()
    fleet = _fleet_ids(serving)
    expected = record["detail"].get("attached") or []
    if not fleet and expected:
        # A successful-but-empty serving read must not launder a detach:
        # addresses were attached at request time, nothing was touched, and
        # "off" would be a verified-sounding lie.
        return _fail(record, "address_unverified",
                     f"The serving tier now reports no registered node, but "
                     f"{len(expected)} managed address(es) were attached when "
                     f"this was requested. Nothing was detached — re-read the "
                     f"capacity report and run the disable again.")
    view = _address_view(fleet, ec2_helper.address_map())
    managed = [row for row in view["attached"] if row.get("managed")]
    _advance(record, "detaching",
             f"detaching {len(managed)} stable address(es)")
    for row in managed:
        association = (view["by_instance"].get(row["instance"]) or {}).get(
            "association_id")
        if not association:
            continue
        ec2_helper.disassociate_address(association)
        _advance(record, "detaching",
                 f"detached {row.get('public_ip')} from {row['instance']}")

    _advance(record, "verifying", "re-reading AWS to confirm the detach")
    final = _address_view(fleet, ec2_helper.address_map())
    still = [row for row in final["attached"] if row.get("managed")]
    if still:
        return _fail(record, "address_unverified",
                     f"AWS still reports managed addresses attached to: "
                     f"{', '.join(sorted(row['instance'] for row in still))}. "
                     f"Run the disable again.")
    facts = ec2_helper.instance_map(fleet)
    post = "; ".join(
        f"{(facts.get(identifier) or {}).get('name') or identifier}: "
        f"{(facts.get(identifier) or {}).get('public_ip') or 'no public address yet'}"
        for identifier in fleet)
    outsiders = [row for row in final["attached"] if not row.get("managed")]
    kept = [row for row in view["reserved"]] + [
        {"allocation_id": row.get("allocation_id"),
         "public_ip": row.get("public_ip")} for row in managed]
    kept_ips = sorted({row.get("public_ip") for row in kept
                       if row.get("public_ip")})
    message = (
        f"Stable outbound IPs are off. Nodes now report: {post or 'none'}. "
        f"{len(kept_ips)} address(es) stay reserved for re-enable "
        f"({', '.join(kept_ips)}) and bill ~${EIP_MONTHLY_USD:.2f}/month each "
        f"on top of the nodes' auto-assigned addresses — release them in the "
        f"AWS console if you mean to let them go.")
    if outsiders:
        message += (
            f" Still attached outside this control: "
            f"{', '.join(sorted(str(row.get('public_ip')) for row in outsiders))}.")
    return _finish(record, message,
                   reserved=[row.get("allocation_id") for row in kept])


def _run_drain_node(record):
    resource = record["resource"]
    groups = record["detail"].get("target_groups") or []
    _advance(record, "draining", "taking the node out of the serving path")
    longest = 0
    for group in groups:
        elbv2_helper.deregister_target(group["arn"], resource, group.get("port"))
        longest = max(longest, elbv2_helper.deregistration_delay(group["arn"]))
    invalidate()
    deadline = time.time() + longest + DRAIN_MARGIN
    while time.time() < deadline:
        _sleep()
        try:
            done = True
            states = []
            for group in groups:
                rows = elbv2_helper.target_health(group["arn"], resource)
                states += [row.get("state") for row in rows]
                done = done and elbv2_helper.drained(rows)
        except ProviderCallError:
            continue
        _advance(record, "draining",
                 f"draining ({', '.join(sorted(set(states))) or 'gone'})")
        if done:
            invalidate()
            return _finish(record,
                           f"{resource} is drained and no longer serving traffic.")
    return _fail(record, "drain_timeout",
                 f"{resource} was still draining after the target group's "
                 f"deregistration delay. It is NOT terminated.")


def _run_terminate_node(record):
    resource = record["resource"]
    _advance(record, "terminating", "terminating the node")
    state = ec2_helper.terminate(resource)
    invalidate()
    _advance(record, "terminating", f"AWS reports {state or 'shutting-down'}")
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        _sleep()
        facts = ec2_helper.instance_facts(resource)
        if facts is None or facts.get("state") == "terminated":
            return _finish(record, f"{resource} is terminated.")
    return _fail(record, "terminate_timeout",
                 f"AWS accepted the termination of {resource} but it has not "
                 f"reached the terminated state yet.")


def _run_add_reader(record):
    detail = record["detail"]
    reader_id = detail["reader_id"]
    _advance(record, "creating", f"creating {reader_id}")
    if detail.get("kind") == "aurora":
        rds_helper.create_cluster_reader(
            detail["cluster"], reader_id, detail.get("instance_class"),
            detail.get("engine"))
    else:
        rds_helper.create_read_replica(
            detail["source"], reader_id, detail.get("instance_class"))
    invalidate()
    _advance(record, "settling", "waiting for AWS to bring the reader up")
    deadline = time.time() + RDS_TIMEOUT
    while time.time() < deadline:
        _sleep(30)
        row = rds_helper.instance_role(reader_id)
        if row is None:
            continue
        _advance(record, "settling", f"{reader_id} is {row.get('status')}")
        if row.get("status") == RDS_SETTLED:
            endpoint = row.get("endpoint")
            invalidate()
            return _finish(
                record,
                f"{reader_id} is available. django-mojo does not read from a "
                f"reader endpoint today — wire {endpoint} into the project's "
                f"DATABASES to use it.",
                reader_id=reader_id, endpoint=endpoint)
    return _fail(record, "reader_timeout",
                 f"{reader_id} was created but has not become available. It is "
                 f"billable; check the RDS console.")


def _run_remove_reader(record):
    resource = record["resource"]
    _advance(record, "deleting", f"deleting {resource}")
    rds_helper.delete_instance(resource)
    invalidate()
    deadline = time.time() + RDS_TIMEOUT
    while time.time() < deadline:
        _sleep(30)
        row = rds_helper.instance_role(resource)
        if row is None:
            invalidate()
            return _finish(record, f"{resource} is deleted.")
        _advance(record, "deleting", f"{resource} is {row.get('status')}")
    return _fail(record, "delete_timeout",
                 f"AWS accepted the deletion of {resource} but it is still "
                 f"listed. Check the RDS console.")


def _run_set_cache_replicas(record):
    resource = record["resource"]
    detail = record["detail"]
    wanted = int(detail["to_count"])
    _advance(record, "scaling",
             f"moving {resource} from {detail['from_count']} to {wanted} replica(s)")
    try:
        result = elasticache_helper.set_replica_count(resource, wanted, True)
    except elasticache_helper.ReplicaCountError as err:
        return _fail(record, err.reason, str(err))
    if not result.get("changed"):
        return _finish(record, f"{resource} already has {wanted} replica(s).")
    invalidate()
    _advance(record, "settling", "waiting for the group to settle")
    deadline = time.time() + CACHE_TIMEOUT
    while time.time() < deadline:
        _sleep(30)
        facts = elasticache_helper.replication_group_facts(resource)
        if facts is None:
            continue
        _advance(record, "settling",
                 f"{resource} is {facts.get('status')} with "
                 f"{facts.get('replica_count')} replica(s)")
        if (facts.get("status") == elasticache_helper.SETTLED
                and int(facts.get("replica_count") or 0) == wanted):
            invalidate()
            note = ("Lag-tolerant reads can use the group's reader endpoint "
                    "when REDIS_READER_SERVER points at it."
                    if wanted > int(detail["from_count"]) else
                    "Automatic failover is off on this group, so it now has no "
                    "standby to fail over to. Unset REDIS_READER_SERVER when "
                    "removing the last replica; AWS does not document how the "
                    "reader endpoint resolves at zero replicas." if wanted == 0 else
                    "The group is smaller; the primary endpoint is unchanged.")
            return _finish(record,
                           f"{resource} now has {wanted} replica(s). {note}")
    return _fail(record, "cache_timeout",
                 f"{resource} did not settle at {wanted} replica(s). Check the "
                 f"ElastiCache console.")


# Provider failures in both resize runners deliberately ride the generic
# handler in run_operation: the claim is held while the mutation state is
# unknown, which is right here — a retried modify is a second mutation.

def _memory_caution(downsize):
    """The honest downsize note. Stated, never measured: the report carries no
    bytes-used figure, so the note names the risk without pretending to."""
    if downsize is True:
        return (" The node is smaller — if the working set no longer fits, "
                "Redis evicts under its maxmemory policy; check cache memory "
                "metrics.")
    if downsize is None:
        return (" If the new node is smaller than the old one and the working "
                "set no longer fits, Redis evicts under its maxmemory policy; "
                "check cache memory metrics.")
    return ""


def _run_resize_cache(record):
    resource = record["resource"]
    detail = record["detail"]
    to_type = detail["to_type"]
    _advance(record, "resizing",
             f"moving {resource} from {detail['from_type']} to {to_type}")
    elasticache_helper.modify_replication_group_node_type(resource, to_type, True)
    invalidate()
    _advance(record, "settling", "waiting for every node to run the new type")
    deadline = time.time() + CACHE_RESIZE_TIMEOUT
    while time.time() < deadline:
        _sleep(30)
        facts = elasticache_helper.replication_group_facts(resource)
        if facts is None:
            continue
        _advance(record, "settling",
                 f"{resource} is {facts.get('status')} on "
                 f"{facts.get('node_type')}")
        if (facts.get("status") == elasticache_helper.SETTLED
                and facts.get("node_type") == to_type):
            invalidate()
            note = ("Replicas were replaced first, then a brief failover "
                    "swapped the primary — one short interruption, not an "
                    "outage." if detail.get("impact") == "rolling" else
                    "With no replica, the cache was down while its node was "
                    "replaced.")
            note += _memory_caution(detail.get("downsize"))
            return _finish(record, f"{resource} now runs {to_type}. {note}")
    return _fail(record, "resize_timeout",
                 f"{resource} did not settle on {to_type}. Check the "
                 f"ElastiCache console.")


def _run_resize_database(record):
    resource = record["resource"]
    detail = record["detail"]
    to_class = detail["to_class"]
    _advance(record, "resizing",
             f"moving {resource} from {detail['from_class']} to {to_class}")
    rds_helper.modify_instance_class(
        resource, to_class, True, promotion_tier=detail.get("promotion_tier"))
    invalidate()
    _advance(record, "settling", "waiting for the instance to settle")
    deadline = time.time() + RDS_TIMEOUT
    while time.time() < deadline:
        _sleep(30)
        row = rds_helper.instance_role(resource)
        if row is None:
            continue
        _advance(record, "settling", f"{resource} is {row.get('status')}")
        if (row.get("status") == RDS_SETTLED
                and row.get("instance_class") == to_class):
            invalidate()
            note = ("The instance restarted to change class — that was the "
                    "few minutes of downtime this action warned about."
                    if detail.get("role") == "writer" else
                    "The writer kept serving; this reader paused while it "
                    "changed class.")
            # Keyed on the tier VALUE, never on role wording: a standalone
            # primary or replica (tier None) has no tier to claim.
            tier = detail.get("promotion_tier")
            if tier == 0:
                note += " Failover preference: tier 0."
            elif tier is not None:
                note += (f" It sits at failover tier {tier}, so the "
                         f"writer-class box stays preferred.")
            return _finish(record, f"{resource} now runs {to_class}. {note}")
    return _fail(record, "resize_timeout",
                 f"{resource} did not settle on {to_class}. Check the RDS "
                 f"console.")
