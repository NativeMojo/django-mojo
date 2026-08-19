"""
AWS RDS Helper Module

Engine-version reads and the two engine-version mutations, for the Admin
Maintenance view. Deliberately narrow: this module answers "what version is
this database on, and is it settled?" and applies one requested upgrade. It
does not create, delete, resize, or snapshot anything.

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


def _instance_facts(row):
    return _facts(row.get("DBInstanceStatus"), row.get("EngineVersion"),
                  row.get("PendingModifiedValues"))


def _cluster_facts(row):
    return _facts(row.get("Status"), row.get("EngineVersion"),
                  row.get("PendingModifiedValues"))


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
