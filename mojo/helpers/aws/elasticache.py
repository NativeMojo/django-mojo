"""
AWS ElastiCache Helper Module

Engine-version reads and the two engine-version mutations, for the Admin
Maintenance view. The twin of ``rds.py``, with one structural difference that
drives the whole module: ElastiCache has TWO resource shapes behind one
identifier space, and they take DIFFERENT modify operations.

* A replication group is modified with ``modify_replication_group``.
* A standalone cache cluster is modified with ``modify_cache_cluster``.

Calling the wrong one fails, so ``resolve_kind`` asks the provider which shape
an identifier is at APPLY time rather than trusting a cached report.

Neither modify operation defines ``AllowMajorVersionUpgrade`` — that member
exists only on the RDS operations. Sending it here is an API error, which is
why the tests stub these calls against the real service model instead of a bare
Mock that would have accepted it silently.

Every provider call goes through ``ProviderCaller.call`` with the IAM action and
the mutation flag named explicitly; see ``rds.py`` for why mutation is never
inferred.
"""

from .client import get_client, get_session
from .provider_call import ProviderCallError, ProviderCaller
from mojo.helpers.settings import settings
from mojo.helpers import logit


logger = logit.get_logger("aws_elasticache", "aws.log")

DEFAULT_TIMEOUT = 5
MAX_ROWS = 200
# "available" is the only ElastiCache status that means no change is in flight.
SETTLED = "available"
# botocore raises ReplicationGroupNotFoundFault; the alternate spellings are
# cheap insurance against an SDK rename turning "standalone" into a 500.
NOT_FOUND_CODES = frozenset({
    "ReplicationGroupNotFoundFault", "ReplicationGroupNotFoundException",
    "ReplicationGroupNotFound",
})

KIND_REPLICATION_GROUP = "replication_group"
KIND_STANDALONE = "standalone"

_caller = ProviderCaller(logger)


def _setting(name, default=None):
    try:
        return settings.get_static(name, default)
    except Exception:
        return default


def _cache(client=None, region=None, timeout=DEFAULT_TIMEOUT):
    """The injection seam. Tests and callers holding a session pass ``client``."""
    if client is not None:
        return client
    region = region or _setting("AWS_REGION", "us-east-1")
    session = get_session(_setting("AWS_KEY"), _setting("AWS_SECRET"), region)
    return get_client("elasticache", session=session, region=region,
                      timeout=timeout, max_attempts=1)


def _member(row):
    return {
        "id": row.get("CacheClusterId"),
        "status": str(row.get("CacheClusterStatus") or "").lower(),
        "engine_version": row.get("EngineVersion"),
    }


def _group_status(members):
    """A group is only as settled as its LEAST settled member.

    Reporting the first member's status would call a group "available" while a
    replica is still rebooting into the new engine — exactly the moment an
    operator must not be told the upgrade is done.
    """
    for member in members:
        if member["status"] != SETTLED:
            return member["status"]
    return SETTLED if members else ""


def _group_map(rows):
    groups = {}
    for row in rows[:MAX_ROWS]:
        replication_id = row.get("ReplicationGroupId")
        identifier = replication_id or row.get("CacheClusterId")
        if not identifier:
            continue
        entry = groups.get(identifier)
        if entry is None:
            entry = groups[identifier] = {
                "is_replication_group": bool(replication_id),
                "engine": row.get("Engine"),
                "members": [],
            }
        entry["members"].append(_member(row))
    for entry in groups.values():
        entry["status"] = _group_status(entry["members"])
        entry["engine_versions"] = sorted({
            member["engine_version"] for member in entry["members"]
            if member["engine_version"]})
    return groups


def group_statuses(client=None, region=None):
    """``{identifier: group}`` for every cache group, in ONE describe.

    Keyed by ``ReplicationGroupId or CacheClusterId`` — the same key the drift
    scanner reports findings under, so a finding and its live status join
    without a second lookup.
    """
    cache = _cache(client, region)
    page = _caller.call(
        "elasticache.describe_cache_clusters",
        lambda: cache.describe_cache_clusters(),
        iam_action="elasticache:DescribeCacheClusters", mutation=False)
    return _group_map(page.get("CacheClusters") or [])


def group_status(identifier, client=None, region=None):
    """One group's live status, or None. This is the poll path.

    Deliberately ONE unfiltered ``describe_cache_clusters`` narrowed in
    process, not a filtered describe: ``describe_cache_clusters`` has no
    replication-group filter, and the ReplicationGroup shape carries no engine
    version at all, so the only provider-side alternative is one describe per
    member — an N+1 against a rate-limited API for a poll that repeats every
    ten seconds.
    """
    return group_statuses(client=client, region=region).get(identifier)


def resolve_kind(identifier, client=None, region=None):
    """``replication_group`` or ``standalone`` — the apply-time discriminator.

    Resolved against the provider at apply time and never read from the cached
    report: the report can be ten minutes stale, and picking the wrong modify
    operation for a resource is a failed mutation against a live database.
    """
    cache = _cache(client, region)
    try:
        _caller.call(
            "elasticache.describe_replication_groups",
            lambda: cache.describe_replication_groups(ReplicationGroupId=identifier),
            iam_action="elasticache:DescribeReplicationGroups", mutation=False)
    except ProviderCallError as err:
        if err.provider_code in NOT_FOUND_CODES:
            return KIND_STANDALONE
        raise
    return KIND_REPLICATION_GROUP


def modify_replication_group_engine_version(identifier, target_version, apply_immediately,
                                            parameter_group=None, client=None, region=None):
    """Move one replication group to ``target_version``.

    No ``AllowMajorVersionUpgrade``: the member is not defined on this
    operation. ``ApplyImmediately`` is the caller's explicit choice — applying
    now fails the group over, and that is an operator decision.
    """
    cache = _cache(client, region)
    params = {
        "ReplicationGroupId": identifier,
        "EngineVersion": target_version,
        "ApplyImmediately": bool(apply_immediately),
    }
    if parameter_group:
        params["CacheParameterGroupName"] = parameter_group
    return _caller.call(
        "elasticache.modify_replication_group",
        lambda: cache.modify_replication_group(**params),
        iam_action="elasticache:ModifyReplicationGroup", mutation=True)


def modify_replication_group_node_type(identifier, node_type, apply_immediately,
                                       client=None, region=None):
    """Move one replication group to ``node_type`` — every member, rolling.

    ``CacheNodeType`` is group-wide: replicas always match their primary, so
    there is no per-instance form of this call. No other member rides along,
    and ``ApplyImmediately`` is the caller's explicit choice — replacing every
    node in the group is an interruption decision the operator makes.
    """
    cache = _cache(client, region)
    return _caller.call(
        "elasticache.modify_replication_group",
        lambda: cache.modify_replication_group(
            ReplicationGroupId=identifier, CacheNodeType=node_type,
            ApplyImmediately=bool(apply_immediately)),
        iam_action="elasticache:ModifyReplicationGroup", mutation=True)


def modify_cache_cluster_engine_version(identifier, target_version, apply_immediately,
                                        parameter_group=None, client=None, region=None):
    """Move one standalone cache cluster to ``target_version``. See the group twin."""
    cache = _cache(client, region)
    params = {
        "CacheClusterId": identifier,
        "EngineVersion": target_version,
        "ApplyImmediately": bool(apply_immediately),
    }
    if parameter_group:
        params["CacheParameterGroupName"] = parameter_group
    return _caller.call(
        "elasticache.modify_cache_cluster",
        lambda: cache.modify_cache_cluster(**params),
        iam_action="elasticache:ModifyCacheCluster", mutation=True)


# ── replica count ───────────────────────────────────────────────────────────
#
# A replica in a replication group is FAILOVER capacity, not read throughput:
# django-mojo talks to the primary endpoint only. Removing the last replica of
# a group with automatic failover enabled therefore does not "free a node" — it
# removes the thing that would take over when the primary dies, and AWS refuses
# the call anyway. That refusal is worth making before the API round trip, with
# a sentence saying what is lost.

CLUSTER_MODE_UNSUPPORTED = "cluster_mode_unsupported"
FAILOVER_REQUIRES_REPLICA = "automatic_failover_requires_replica"
# AutomaticFailover reports four values; two of them mean it is on or coming on.
FAILOVER_ON = frozenset({"enabled", "enabling"})
MULTI_AZ_ON = frozenset({"enabled", "enabling"})


class ReplicaCountError(ValueError):
    """A replica-count change this module refuses to send. Carries a reason."""

    def __init__(self, message, reason):
        self.reason = str(reason)
        super().__init__(message)


def _members(row):
    """Per-member role and status, flattened across every node group."""
    members = []
    for group in (row.get("NodeGroups") or [])[:MAX_ROWS]:
        for member in (group.get("NodeGroupMembers") or [])[:MAX_ROWS]:
            members.append({
                "id": member.get("CacheClusterId"),
                "node_group": group.get("NodeGroupId"),
                "role": str(member.get("CurrentRole") or "").lower(),
                "zone": member.get("PreferredAvailabilityZone"),
            })
    return members


def _group_facts(row):
    members = _members(row)
    replicas = [member for member in members if member["role"] == "replica"]
    failover = str(row.get("AutomaticFailover") or "").lower()
    multi_az = str(row.get("MultiAZ") or "").lower()
    return {
        "identifier": row.get("ReplicationGroupId"),
        "arn": row.get("ARN"),
        "status": str(row.get("Status") or "").lower(),
        "description": row.get("Description"),
        # Group-wide in ElastiCache: every member runs the same node type, so
        # there is no per-instance cache sizing to report.
        "node_type": row.get("CacheNodeType"),
        "cluster_enabled": bool(row.get("ClusterEnabled")),
        "automatic_failover": failover,
        "automatic_failover_on": failover in FAILOVER_ON,
        "multi_az": multi_az,
        "multi_az_on": multi_az in MULTI_AZ_ON,
        "node_groups": len(row.get("NodeGroups") or []),
        "members": members,
        "member_clusters": list(row.get("MemberClusters") or [])[:MAX_ROWS],
        "replica_count": len(replicas),
        "primary_count": sum(1 for member in members if member["role"] == "primary"),
    }


def replication_group_facts(identifier, client=None, region=None):
    """Everything the replica-count guards need about ONE group, or None.

    ``cluster_enabled`` is read straight from the group rather than inferred
    from node-group count: a cluster-mode-enabled group with a single shard
    looks exactly like a cluster-mode-disabled one from the outside, and the
    Increase/DecreaseReplicaCount operations behave differently on it.
    """
    cache = _cache(client, region)
    try:
        page = _caller.call(
            "elasticache.describe_replication_groups",
            lambda: cache.describe_replication_groups(ReplicationGroupId=identifier),
            iam_action="elasticache:DescribeReplicationGroups", mutation=False)
    except ProviderCallError as err:
        if err.provider_code in NOT_FOUND_CODES:
            return None
        raise
    rows = page.get("ReplicationGroups") or []
    return _group_facts(rows[0]) if rows else None


def replication_groups(client=None, region=None):
    """The same facts for EVERY replication group, in ONE describe."""
    cache = _cache(client, region)
    page = _caller.call(
        "elasticache.describe_replication_groups",
        lambda: cache.describe_replication_groups(),
        iam_action="elasticache:DescribeReplicationGroups", mutation=False)
    return [_group_facts(row)
            for row in (page.get("ReplicationGroups") or [])[:MAX_ROWS]
            if row.get("ReplicationGroupId")]


def replication_group_tags(arn, client=None, region=None):
    """The ownership tags for one replication group, keyed by tag name."""
    if not arn:
        return {}
    cache = _cache(client, region)
    page = _caller.call(
        "elasticache.list_tags_for_resource",
        lambda: cache.list_tags_for_resource(ResourceName=str(arn)),
        iam_action="elasticache:ListTagsForResource", mutation=False)
    return {tag.get("Key"): tag.get("Value")
            for tag in (page.get("TagList") or [])[:MAX_ROWS]
            if tag.get("Key")}


def set_replica_count(identifier, new_count, apply_immediately, facts=None,
                      client=None, region=None):
    """Move ONE replication group to ``new_count`` replicas per shard.

    Refuses, before any API call:

    * a cluster-mode-enabled group, by name — its replica count is per shard
      and changing it is a resharding decision, not a capacity button;
    * a decrease below one replica while automatic failover or Multi-AZ is on.
      AWS rejects it too, but its message is not the sentence an operator needs.

    ``ReplicasToRemove`` is deliberately never sent on a decrease: naming the
    node to kill means choosing which availability zone loses its standby, and
    ElastiCache picks better than a portal that cannot see the shard layout.

    ``ApplyImmediately`` is the caller's explicit choice and is passed through
    unchanged; ElastiCache currently supports only ``True`` on these two
    operations, and a caller sending ``False`` should hear that from AWS rather
    than have this module quietly rewrite the request.
    """
    facts = facts if facts is not None else replication_group_facts(
        identifier, client=client, region=region)
    if facts is None:
        raise ReplicaCountError(
            f"{identifier} is not a replication group", CLUSTER_MODE_UNSUPPORTED)
    if facts.get("cluster_enabled"):
        raise ReplicaCountError(
            f"{identifier} is cluster-mode enabled; its replica count is a "
            f"per-shard resharding decision, not a capacity change",
            CLUSTER_MODE_UNSUPPORTED)
    new_count = int(new_count)
    current = int(facts.get("replica_count") or 0)
    if new_count < 0:
        raise ReplicaCountError("a replica count cannot be negative", "invalid_request")
    if new_count == current:
        return {"operation": None, "changed": False, "replica_count": current}
    if new_count < 1 and (facts.get("automatic_failover_on")
                          or facts.get("multi_az_on")):
        raise ReplicaCountError(
            f"{identifier} has automatic failover enabled, which requires at "
            f"least one replica", FAILOVER_REQUIRES_REPLICA)

    cache = _cache(client, region)
    if new_count > current:
        _caller.call(
            "elasticache.increase_replica_count",
            lambda: cache.increase_replica_count(
                ReplicationGroupId=identifier, NewReplicaCount=new_count,
                ApplyImmediately=bool(apply_immediately)),
            iam_action="elasticache:IncreaseReplicaCount", mutation=True)
        return {"operation": "elasticache.increase_replica_count",
                "changed": True, "replica_count": new_count}
    _caller.call(
        "elasticache.decrease_replica_count",
        lambda: cache.decrease_replica_count(
            ReplicationGroupId=identifier, NewReplicaCount=new_count,
            ApplyImmediately=bool(apply_immediately)),
        iam_action="elasticache:DecreaseReplicaCount", mutation=True)
    return {"operation": "elasticache.decrease_replica_count",
            "changed": True, "replica_count": new_count}
