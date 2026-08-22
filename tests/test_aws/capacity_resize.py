"""Capacity resize: the modify request shapes and the report's resize facts.

Same testing stance as capacity.py's own module: ``botocore.stub.Stubber``
proves every request is legal against the real service model (a Mock would
accept ``PromotionTier`` spelled wrong, or silently accept it on a standalone
modify where it must not be sent), and injected fake clients feed the report.
The guard, runner, and in-process REST tests, which patch shared module
attributes (``_dispatch`` and friends), live in
tests/test_aws_extended_serial/capacity_resize.py (maestro #2558).
"""

from unittest import mock

from testit import helpers as th


REGION = "us-east-1"
CLUSTER = "mojo-resize-aurora"
WRITER = f"{CLUSTER}-1"
READER_A = f"{CLUSTER}-2"
READER_B = f"{CLUSTER}-3"
STANDALONE = "mojo-resize-postgres"
STANDALONE_REPLICA = f"{STANDALONE}-replica"
CACHE_GROUP = "mojo-resize-redis"
SECOND_GROUP = "mojo-resize-redis-solo"


# ── fixtures ────────────────────────────────────────────────────────────────

def _stub(service):
    """A real bounded client plus its Stubber. Credentials are never used."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name=REGION,
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def _cache_group(identifier=CACHE_GROUP, replicas=1, failover="enabled",
                 multi_az="enabled", cluster_enabled=False,
                 status="available", node_type="cache.t4g.micro"):
    members = [{"CacheClusterId": f"{identifier}-001", "CurrentRole": "primary"}]
    for index in range(replicas):
        members.append({"CacheClusterId": f"{identifier}-{index + 2:03d}",
                        "CurrentRole": "replica"})
    return {
        "ReplicationGroupId": identifier, "Status": status,
        "CacheNodeType": node_type, "ClusterEnabled": cluster_enabled,
        "AutomaticFailover": failover, "MultiAZ": multi_az,
        "NodeGroups": [{"NodeGroupId": "0001", "NodeGroupMembers": members}],
        "MemberClusters": [member["CacheClusterId"] for member in members],
    }


def _cache_client(groups=()):
    client = mock.Mock()
    client.describe_replication_groups.return_value = {
        "ReplicationGroups": list(groups)}
    return client


def _db_instance(identifier, cls="db.r6g.large", status="available",
                 cluster=None, replica_of=None):
    row = {
        "DBInstanceIdentifier": identifier, "DBInstanceStatus": status,
        "Engine": "aurora-postgresql" if cluster else "postgres",
        "EngineVersion": "16.4", "DBInstanceClass": cls,
        "AvailabilityZone": "us-east-1a",
        "Endpoint": {"Address": f"{identifier}.example.com", "Port": 5432},
    }
    if cluster:
        row["DBClusterIdentifier"] = cluster
    if replica_of:
        row["ReadReplicaSourceDBInstanceIdentifier"] = replica_of
    return row


def _rds_client(instances=(), clusters=()):
    """Filter-aware, unlike a plain Mock: ``instance_role`` describes ONE
    instance by identifier, and answering every filter with the same first row
    would make every per-instance test pass vacuously."""
    by_id = {row["DBInstanceIdentifier"]: row for row in instances}
    client = mock.Mock()

    def describe_instances(**kwargs):
        wanted = kwargs.get("DBInstanceIdentifier")
        if wanted:
            row = by_id.get(wanted)
            return {"DBInstances": [row] if row else []}
        return {"DBInstances": list(instances)}

    client.describe_db_instances.side_effect = describe_instances
    client.describe_db_clusters.return_value = {"DBClusters": list(clusters)}
    return client


def _aurora_cluster(writer=WRITER, readers=(READER_A, READER_B)):
    members = [{"DBInstanceIdentifier": writer, "IsClusterWriter": True}]
    members += [{"DBInstanceIdentifier": reader, "IsClusterWriter": False}
                for reader in readers]
    return {
        "DBClusterIdentifier": CLUSTER, "Engine": "aurora-postgresql",
        "Status": "available", "DBClusterMembers": members,
        "Endpoint": f"{CLUSTER}.cluster.example.com",
        "ReaderEndpoint": f"{CLUSTER}.cluster-ro.example.com",
    }


def _elbv2_client():
    client = mock.Mock()
    client.describe_load_balancers.return_value = {"LoadBalancers": []}
    client.describe_target_groups.return_value = {"TargetGroups": []}
    client.describe_target_health.return_value = {"TargetHealthDescriptions": []}
    return client


def _ec2_client():
    client = mock.Mock()
    client.describe_instances.return_value = {"Reservations": []}
    client.describe_addresses.return_value = {"Addresses": []}
    return client


def _clear_claims():
    from mojo.apps.aws.services import capacity
    for action in capacity.ACTIONS:
        for resource in ("fleet", CLUSTER, WRITER, READER_A, READER_B,
                         STANDALONE, STANDALONE_REPLICA, CACHE_GROUP,
                         SECOND_GROUP):
            capacity._release(capacity._claim_key(action, resource))
    capacity.invalidate()


@th.django_unit_setup()
def setup_capacity_resize(opts):
    # Claims live in a shared cache with a 90-minute TTL, so a previous run's
    # key would refuse this one. Delete what this module will create.
    _clear_claims()


# ── helpers: the modify request shapes ──────────────────────────────────────

@th.django_unit_test("a cache resize sends exactly the group, the type, and immediate")
def test_cache_resize_modify_shape(opts):
    from mojo.helpers.aws import elasticache

    client, stubber = _stub("elasticache")
    # expected_params is exact: a member this code invents (or a parameter
    # group riding along uninvited) fails here.
    stubber.add_response("modify_replication_group", {"ReplicationGroup": {}}, {
        "ReplicationGroupId": CACHE_GROUP,
        "CacheNodeType": "cache.r7g.large",
        "ApplyImmediately": True})
    with stubber:
        elasticache.modify_replication_group_node_type(
            CACHE_GROUP, "cache.r7g.large", True, client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("the DB resize carries the tier in the SAME call — and never off-Aurora")
def test_db_resize_modify_shape(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    # Aurora writer: PromotionTier rides in the same ModifyDBInstance, so a
    # successful resize can never leave the tier half-set.
    stubber.add_response("modify_db_instance", {"DBInstance": {}}, {
        "DBInstanceIdentifier": WRITER,
        "DBInstanceClass": "db.r6g.2xlarge",
        "ApplyImmediately": True,
        "PromotionTier": 0})
    # Standalone: the member is absent entirely, not sent as null.
    stubber.add_response("modify_db_instance", {"DBInstance": {}}, {
        "DBInstanceIdentifier": STANDALONE,
        "DBInstanceClass": "db.r6g.large",
        "ApplyImmediately": True})
    with stubber:
        rds.modify_instance_class(WRITER, "db.r6g.2xlarge", True,
                                  promotion_tier=0, client=client)
        rds.modify_instance_class(STANDALONE, "db.r6g.large", True,
                                  promotion_tier=None, client=client)
    stubber.assert_no_pending_responses()


# ── service: the report's resize facts ──────────────────────────────────────

@th.django_unit_test("the report states each group's node type and interruption case")
def test_resize_cache_impact_and_offers(opts):
    from mojo.apps.aws.services import capacity

    cache_client = _cache_client([
        _cache_group(identifier=CACHE_GROUP, replicas=1, failover="enabled"),
        _cache_group(identifier=SECOND_GROUP, replicas=0, failover="disabled",
                     multi_az="disabled", node_type="cache.t4g.medium"),
    ])
    envelope = capacity.report(
        elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
        rds_client=_rds_client(), cache_client=cache_client)
    rows = {row["identifier"]: row for row in envelope["caches"]}
    assert rows[CACHE_GROUP]["node_type"] == "cache.t4g.micro", \
        f"the group's node type is missing from its row: {rows[CACHE_GROUP]}"
    assert rows[CACHE_GROUP]["resize_impact"] == "rolling", \
        (f"failover + a replica must report a rolling resize: "
         f"{rows[CACHE_GROUP]}")
    assert rows[SECOND_GROUP]["resize_impact"] == "downtime", \
        (f"no replica must report resize downtime BEFORE apply: "
         f"{rows[SECOND_GROUP]}")

    assert envelope["actions"]["resize_cache"] == \
        {"offered": True, "blocked_reason": None}, \
        f"resize_cache was not offered: {envelope['actions']['resize_cache']}"
    assert envelope["actions"]["resize_database"] == \
        {"offered": False, "blocked_reason": "no_database"}, \
        (f"an empty region still offered resize_database: "
         f"{envelope['actions']['resize_database']}")

    with_db = capacity.report(
        elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
        rds_client=_rds_client(
            [_db_instance(WRITER, cls="db.r6g.xlarge", cluster=CLUSTER)],
            [_aurora_cluster(readers=())]),
        cache_client=cache_client)
    assert with_db["actions"]["resize_database"] == \
        {"offered": True, "blocked_reason": None}, \
        (f"a region with a database did not offer resize_database: "
         f"{with_db['actions']['resize_database']}")


# ── service: the shared cache-group claim ───────────────────────────────────

@th.django_unit_test("a cache resize and a replica change serialize on ONE group claim")
def test_resize_shares_the_cache_group_claim(opts):
    from mojo.apps.aws.services import capacity

    _clear_claims()
    assert capacity._claim_key(capacity.ACTION_RESIZE_CACHE, CACHE_GROUP) == \
        capacity._claim_key(capacity.ACTION_SET_CACHE_REPLICAS, CACHE_GROUP), \
        "the two cache mutations do not share the group claim key"
    # The literal matters: the deployed set_cache_replicas key is reused so a
    # claim held across a deploy stays honored.
    assert capacity._claim_key(capacity.ACTION_RESIZE_CACHE, CACHE_GROUP) == \
        f"{capacity.CACHE_PREFIX}:claim:set_cache_replicas:{CACHE_GROUP}", \
        (f"the shared claim is not the deployed literal: "
         f"{capacity._claim_key(capacity.ACTION_RESIZE_CACHE, CACHE_GROUP)}")

    capacity._claim(capacity.ACTION_SET_CACHE_REPLICAS, CACHE_GROUP, 1)
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._claim(capacity.ACTION_RESIZE_CACHE, CACHE_GROUP, 2)
    assert caught.exception.error_code == "capacity_in_progress", \
        (f"a resize was not refused while a replica change holds the group: "
         f"{caught.exception.error_code}")
    _clear_claims()

    capacity._claim(capacity.ACTION_RESIZE_CACHE, CACHE_GROUP, 1)
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._claim(capacity.ACTION_SET_CACHE_REPLICAS, CACHE_GROUP, 2)
    assert caught.exception.error_code == "capacity_in_progress", \
        (f"a replica change was not refused while a resize holds the group: "
         f"{caught.exception.error_code}")
    _clear_claims()
