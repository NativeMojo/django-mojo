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
