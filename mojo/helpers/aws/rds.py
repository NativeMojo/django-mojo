"""
AWS RDS Helper Module

Two narrow jobs, no more:

* **Engine versions**, for the Admin Maintenance view — "what version is this
  database on, is it settled?" plus the one requested upgrade.
* **Read capacity**, for the Admin capacity actions — what shape a database is
  (Aurora cluster vs standalone instance), adding one reader, and deleting one.

The shape question is not cosmetic. Aurora does NOT support
``CreateDBInstanceReadReplica`` for a cluster: a reader is a plain
``create_db_instance`` carrying ``DBClusterIdentifier``, and the cluster is
what replicates. A standalone instance is the opposite — it has no cluster, and
its replica is made with ``create_db_instance_read_replica``. Sending either
call to the other kind of database is an API error, so the shape is resolved
LIVE at apply time (``instance_role`` / ``cluster_members``) rather than read
off a cached report.

Every provider call goes through ``ProviderCaller.call`` with the IAM action
named EXPLICITLY, and with ``mutation`` passed explicitly rather than inferred.
``ProviderClient`` derives mutation from the method-name prefix and ``modify_``
is not one of its prefixes, so a modify would be recorded as a read and its
failure would claim ``mutation_state="none"`` — a wrong answer on the one
question that matters after a failed upgrade.

Raw botocore text never leaves this module: a failure raises
``ProviderCallError``, whose ``detail()`` is the only shape safe for an API
response or a log line.
"""

from .client import get_client, get_session
from .provider_call import ProviderCaller
from mojo.helpers.settings import settings
from mojo.helpers import logit


logger = logit.get_logger("aws_rds", "aws.log")

DEFAULT_TIMEOUT = 5
MAX_ROWS = 200
# "available" is the only RDS status that means no change is in flight.
SETTLED = "available"

_caller = ProviderCaller(logger)


def _setting(name, default=None):
    try:
        return settings.get_static(name, default)
    except Exception:
        return default


def _rds(client=None, region=None, timeout=DEFAULT_TIMEOUT):
    """The injection seam. Tests and callers holding a session pass ``client``."""
    if client is not None:
        return client
    region = region or _setting("AWS_REGION", "us-east-1")
    session = get_session(_setting("AWS_KEY"), _setting("AWS_SECRET"), region)
    # One attempt: a denial or an unreachable endpoint must answer the operator
    # quickly, and a retried modify is the one thing this module must not do.
    return get_client("rds", session=session, region=region,
                      timeout=timeout, max_attempts=1)


def _facts(status, engine_version, pending):
    return {
        "status": str(status or "").lower(),
        "engine_version": engine_version,
        # The version RDS has accepted but not yet applied. Its presence is how
        # an operator knows a requested upgrade was actually queued.
        "pending_version": (pending or {}).get("EngineVersion"),
    }


def _tag_map(row):
    return {tag.get("Key"): tag.get("Value")
            for tag in row.get("TagList") or row.get("Tags") or []
            if tag.get("Key")}


def _tag_list(tags):
    return [{"Key": key, "Value": str(value)}
            for key, value in sorted((tags or {}).items())]


def _instance_facts(row):
    facts = _facts(row.get("DBInstanceStatus"), row.get("EngineVersion"),
                   row.get("PendingModifiedValues"))
    # Instances only — clusters have no class of their own. Riding on the one
    # describe `instance_statuses` already performs, this is what gives the
    # capacity report a per-instance class for every Aurora member at zero
    # extra API calls.
    facts["instance_class"] = row.get("DBInstanceClass")
    facts["tags"] = _tag_map(row)
    return facts


def _cluster_facts(row):
    facts = _facts(row.get("Status"), row.get("EngineVersion"),
                   row.get("PendingModifiedValues"))
    facts["tags"] = _tag_map(row)
    return facts


def instance_statuses(client=None, region=None):
    """``{identifier: facts}`` for every DB instance, in ONE describe."""
    rds = _rds(client, region)
    page = _caller.call(
        "rds.describe_db_instances", lambda: rds.describe_db_instances(),
        iam_action="rds:DescribeDBInstances", mutation=False)
    return {row.get("DBInstanceIdentifier"): _instance_facts(row)
            for row in (page.get("DBInstances") or [])[:MAX_ROWS]
            if row.get("DBInstanceIdentifier")}


def cluster_statuses(client=None, region=None):
    """``{identifier: facts}`` for every DB cluster, in ONE describe."""
    rds = _rds(client, region)
    page = _caller.call(
        "rds.describe_db_clusters", lambda: rds.describe_db_clusters(),
        iam_action="rds:DescribeDBClusters", mutation=False)
    return {row.get("DBClusterIdentifier"): _cluster_facts(row)
            for row in (page.get("DBClusters") or [])[:MAX_ROWS]
            if row.get("DBClusterIdentifier")}


def instance_status(identifier, client=None, region=None):
    """One instance's facts, or None. Filtered describe — this is the poll path."""
    rds = _rds(client, region)
    page = _caller.call(
        "rds.describe_db_instances",
        lambda: rds.describe_db_instances(DBInstanceIdentifier=identifier),
        iam_action="rds:DescribeDBInstances", mutation=False)
    rows = page.get("DBInstances") or []
    return _instance_facts(rows[0]) if rows else None


def cluster_status(identifier, client=None, region=None):
    """One cluster's facts, or None. Filtered describe — this is the poll path."""
    rds = _rds(client, region)
    page = _caller.call(
        "rds.describe_db_clusters",
        lambda: rds.describe_db_clusters(DBClusterIdentifier=identifier),
        iam_action="rds:DescribeDBClusters", mutation=False)
    rows = page.get("DBClusters") or []
    return _cluster_facts(rows[0]) if rows else None


def modify_instance_engine_version(identifier, target_version, apply_immediately,
                                   parameter_group=None, client=None, region=None):
    """Move one DB instance to ``target_version``.

    ``AllowMajorVersionUpgrade`` is always true: the Maintenance view only ever
    offers the MAJOR targets the drift scanner found, and RDS refuses a major
    engine change without it. ``ApplyImmediately`` is the caller's choice and is
    never defaulted — "now" and "next maintenance window" are different
    outage decisions and the operator makes them, not this module.
    """
    rds = _rds(client, region)
    params = {
        "DBInstanceIdentifier": identifier,
        "EngineVersion": target_version,
        "AllowMajorVersionUpgrade": True,
        "ApplyImmediately": bool(apply_immediately),
    }
    if parameter_group:
        params["DBParameterGroupName"] = parameter_group
    return _caller.call(
        "rds.modify_db_instance", lambda: rds.modify_db_instance(**params),
        iam_action="rds:ModifyDBInstance", mutation=True)


def modify_instance_class(identifier, instance_class, apply_immediately,
                          promotion_tier=None, client=None, region=None):
    """Move one DB instance to ``instance_class``.

    ``PromotionTier`` rides in the SAME call when given — Aurora members only,
    never sent for a standalone instance — so a successful resize can never
    leave the tier half-set. No ``AllowMajorVersionUpgrade``: a class change
    is not an engine change. ``ApplyImmediately`` is the caller's explicit
    choice and is never defaulted — "now" restarts the instance, "next
    maintenance window" parks the change, and the operator decides which.
    """
    rds = _rds(client, region)
    params = {
        "DBInstanceIdentifier": identifier,
        "DBInstanceClass": instance_class,
        "ApplyImmediately": bool(apply_immediately),
    }
    if promotion_tier is not None:
        params["PromotionTier"] = int(promotion_tier)
    return _caller.call(
        "rds.modify_db_instance", lambda: rds.modify_db_instance(**params),
        iam_action="rds:ModifyDBInstance", mutation=True)


def modify_cluster_engine_version(identifier, target_version, apply_immediately,
                                  parameter_group=None, client=None, region=None):
    """Move one DB cluster (Aurora) to ``target_version``. See the instance twin."""
    rds = _rds(client, region)
    params = {
        "DBClusterIdentifier": identifier,
        "EngineVersion": target_version,
        "AllowMajorVersionUpgrade": True,
        "ApplyImmediately": bool(apply_immediately),
    }
    if parameter_group:
        params["DBClusterParameterGroupName"] = parameter_group
    return _caller.call(
        "rds.modify_db_cluster", lambda: rds.modify_db_cluster(**params),
        iam_action="rds:ModifyDBCluster", mutation=True)


# ── read capacity ───────────────────────────────────────────────────────────

def _endpoint(row):
    endpoint = row.get("Endpoint") or {}
    address = endpoint.get("Address")
    port = endpoint.get("Port")
    return f"{address}:{port}" if address and port else address or None


def instance_role(identifier, client=None, region=None):
    """What ONE DB instance is, or None when AWS has no such instance.

    ``is_replica`` is true for a standalone read replica (it names a source)
    and for a non-writer Aurora member; ``cluster`` is the only reliable
    discriminator between the two shapes.
    """
    rds = _rds(client, region)
    page = _caller.call(
        "rds.describe_db_instances",
        lambda: rds.describe_db_instances(DBInstanceIdentifier=identifier),
        iam_action="rds:DescribeDBInstances", mutation=False)
    rows = page.get("DBInstances") or []
    if not rows:
        return None
    row = rows[0]
    source = row.get("ReadReplicaSourceDBInstanceIdentifier")
    return {
        "identifier": row.get("DBInstanceIdentifier"),
        "status": str(row.get("DBInstanceStatus") or "").lower(),
        "engine": row.get("Engine"),
        "engine_version": row.get("EngineVersion"),
        "instance_class": row.get("DBInstanceClass"),
        "availability_zone": row.get("AvailabilityZone"),
        "cluster": row.get("DBClusterIdentifier"),
        "replica_source": source,
        "is_replica": bool(source),
        "endpoint": _endpoint(row),
        "tags": _tag_map(row),
    }


def cluster_members(identifier, client=None, region=None):
    """One Aurora cluster's members and reader endpoint, or None.

    ``is_writer`` comes from ``IsClusterWriter`` — the ONLY place Aurora says
    which member is the primary. An identifier alone proves nothing.
    """
    rds = _rds(client, region)
    page = _caller.call(
        "rds.describe_db_clusters",
        lambda: rds.describe_db_clusters(DBClusterIdentifier=identifier),
        iam_action="rds:DescribeDBClusters", mutation=False)
    rows = page.get("DBClusters") or []
    if not rows:
        return None
    row = rows[0]
    members = []
    # DBClusterMembers carries no per-member lifecycle status — only the
    # parameter-group sync state, which is a different question. A member's
    # real status comes from describe_db_instances (instance_role).
    for member in (row.get("DBClusterMembers") or [])[:MAX_ROWS]:
        members.append({
            "id": member.get("DBInstanceIdentifier"),
            "is_writer": bool(member.get("IsClusterWriter")),
        })
    return {
        "identifier": row.get("DBClusterIdentifier"),
        "engine": row.get("Engine"),
        "engine_version": row.get("EngineVersion"),
        "status": str(row.get("Status") or "").lower(),
        "endpoint": row.get("Endpoint"),
        "reader_endpoint": row.get("ReaderEndpoint"),
        "members": members,
        "readers": [member["id"] for member in members if not member["is_writer"]],
        "writer": next((member["id"] for member in members if member["is_writer"]), None),
        "tags": _tag_map(row),
    }


def create_cluster_reader(cluster_id, instance_id, instance_class, engine,
                          availability_zone=None, tags=None, client=None,
                          region=None):
    """Add ONE reader to an Aurora cluster.

    ``create_db_instance`` with ``DBClusterIdentifier``, NOT
    ``create_db_instance_read_replica`` — Aurora does not support the latter
    for a cluster member. No storage or credentials are passed: an Aurora
    member takes both from the cluster.
    """
    rds = _rds(client, region)
    params = {
        "DBInstanceIdentifier": instance_id,
        "DBClusterIdentifier": cluster_id,
        "DBInstanceClass": instance_class,
        "Engine": engine,
    }
    if availability_zone:
        params["AvailabilityZone"] = availability_zone
    if tags:
        params["Tags"] = _tag_list(tags)
    return _caller.call(
        "rds.create_db_instance", lambda: rds.create_db_instance(**params),
        iam_action="rds:CreateDBInstance", mutation=True)


def create_read_replica(source_id, replica_id, instance_class=None,
                        availability_zone=None, tags=None, client=None,
                        region=None):
    """Add ONE read replica of a STANDALONE DB instance. See the Aurora twin."""
    rds = _rds(client, region)
    params = {
        "DBInstanceIdentifier": replica_id,
        "SourceDBInstanceIdentifier": source_id,
    }
    if instance_class:
        params["DBInstanceClass"] = instance_class
    if availability_zone:
        params["AvailabilityZone"] = availability_zone
    if tags:
        params["Tags"] = _tag_list(tags)
    return _caller.call(
        "rds.create_db_instance_read_replica",
        lambda: rds.create_db_instance_read_replica(**params),
        iam_action="rds:CreateDBInstanceReadReplica", mutation=True)


def delete_instance(identifier, client=None, region=None):
    """Delete ONE DB instance with no final snapshot.

    ``SkipFinalSnapshot=True`` is correct ONLY because the caller has already
    proved the target is a reader/replica — a copy of data that lives
    elsewhere. The proof is the caller's job; this function does not re-derive
    it, so never call it on something whose role you have not checked.
    """
    rds = _rds(client, region)
    return _caller.call(
        "rds.delete_db_instance",
        lambda: rds.delete_db_instance(
            DBInstanceIdentifier=identifier, SkipFinalSnapshot=True),
        iam_action="rds:DeleteDBInstance", mutation=True)
