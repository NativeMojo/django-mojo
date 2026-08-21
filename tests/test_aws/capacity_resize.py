"""Capacity resize actions: curated sizes, the guards, and the settle loops.

Same testing stance as capacity.py's own module: ``botocore.stub.Stubber``
proves every request is legal against the real service model (a Mock would
accept ``PromotionTier`` spelled wrong, or silently accept it on a standalone
modify where it must not be sent), captured Mocks prove the values, and the
validators are exercised through injected clients with ``_dispatch`` patched.
"""

from types import SimpleNamespace
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


def _actor(pk=1):
    return SimpleNamespace(pk=pk, username="resize-actor", is_superuser=True)


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


def _resize_record(capacity, action, resource, detail):
    return {
        "schema_version": 1, "id": "op-resize-test", "action": action,
        "resource": resource, "actor": None, "started": "",
        "state": "running", "phase": "resizing",
        "phases": list(capacity.PHASES[action]),
        "message": "requested", "error_code": None, "warnings": [],
        "detail": dict(detail), "claim": "test-claim",
    }


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


# ── service: the curated allowlist ──────────────────────────────────────────

@th.django_unit_test("only the four curated size keys are accepted — a raw type never is")
def test_resize_sizes_are_curated(opts):
    from mojo.apps.aws.services import capacity
    from mojo.deploy.provision import spec as provision_spec

    cache_client = _cache_client([_cache_group()])
    rds_client = _rds_client([_db_instance(STANDALONE)])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        for bad in ("cache.m5.large", "huge", "", None):
            with th.assert_raises(capacity.CapacityError) as caught:
                capacity.apply(_actor(), capacity.ACTION_RESIZE_CACHE,
                               CACHE_GROUP, size=bad, apply_immediately=True,
                               cache_client=cache_client)
            assert caught.exception.error_code == "invalid_request", \
                f"size {bad!r} was refused as {caught.exception.error_code}"
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_RESIZE_DATABASE,
                           STANDALONE, size="db.r6g.large",
                           apply_immediately=True, rds_client=rds_client)
        assert caught.exception.error_code == "invalid_request", \
            f"a raw DB type was refused as {caught.exception.error_code}"
    assert dispatched.call_count == 0, \
        "an uncurated size still started an operation"
    assert cache_client.describe_replication_groups.call_count == 0 \
        and rds_client.describe_db_instances.call_count == 0, \
        "an uncurated size still reached the provider before being refused"

    # Each key maps to exactly the ladder's type, case- and space-insensitive.
    for ladder in (provision_spec.CACHE_SIZES, provision_spec.DB_SIZES):
        for key, _label, itype in ladder:
            assert capacity._resolve_size(ladder, key) == itype, \
                f"size key {key!r} did not resolve to {itype}"
            assert capacity._resolve_size(ladder, f" {key.upper()} ") == itype, \
                f"size key {key!r} did not normalize before resolving"

    envelope = capacity.report(
        elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
        rds_client=_rds_client(), cache_client=_cache_client())
    for kind in ("cache", "database"):
        rows = envelope["sizes"][kind]
        assert [row["size"] for row in rows] == \
            ["small", "medium", "large", "xlarge"], \
            f"the {kind} size ladder is not the four curated rungs: {rows}"
        for row in rows:
            assert row.get("monthly_usd"), \
                (f"the {kind} rung {row['size']} carries no monthly cost — "
                 f"the panel would offer a size with no number on it: {row}")
            assert row.get("label") and row.get("type"), \
                f"the {kind} rung {row['size']} is missing label or type: {row}"


# ── service: cache guards ───────────────────────────────────────────────────

@th.django_unit_test("the cache resize guards refuse before any mutation")
def test_resize_cache_guards(opts):
    from mojo.apps.aws.services import capacity

    def refuse(client, expected_code, **params):
        with mock.patch.object(capacity, "_dispatch") as dispatched:
            with th.assert_raises(capacity.CapacityError) as caught:
                capacity.apply(_actor(), capacity.ACTION_RESIZE_CACHE,
                               CACHE_GROUP, cache_client=client, **params)
        assert dispatched.call_count == 0, \
            f"a {expected_code} refusal still started an operation"
        assert caught.exception.error_code == expected_code, \
            (f"the wrong refusal for {params}: "
             f"{caught.exception.error_code} != {expected_code}")
        return caught.exception

    refuse(_cache_client(), "resource_not_found",
           size="large", apply_immediately=True)
    refuse(_cache_client([_cache_group(cluster_enabled=True)]),
           "cluster_mode_unsupported", size="large", apply_immediately=True)
    refuse(_cache_client([_cache_group(node_type="cache.r7g.large")]),
           "no_change", size="large", apply_immediately=True)
    error = refuse(_cache_client([_cache_group(status="modifying")]),
                   "not_settled", size="large", apply_immediately=True)
    assert "modifying" in error.message, \
        (f"the not_settled refusal did not quote the provider state: "
         f"{error.message}")
    assert "in flight" not in error.message.lower(), \
        ("the refusal blamed an in-flight change it cannot know about: "
         f"{error.message}")
    refuse(_cache_client([_cache_group()]), "invalid_request",
           size="large", apply_immediately=False)
    refuse(_cache_client([_cache_group()]), "invalid_request", size="large")

    # The nightly backup is not a conflict: snapshotting is accepted.
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        capacity.apply(
            _actor(), capacity.ACTION_RESIZE_CACHE, CACHE_GROUP,
            size="large", apply_immediately=True,
            cache_client=_cache_client([_cache_group(status="snapshotting")]))
    assert dispatched.call_count == 1, \
        "a snapshotting group could not be resized"
    _clear_claims()

    # Happy path records everything the runner and the note need.
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        record = capacity.apply(
            _actor(), capacity.ACTION_RESIZE_CACHE, CACHE_GROUP,
            size="large", apply_immediately=True,
            cache_client=_cache_client([_cache_group()]))
    assert dispatched.call_count == 1, "the happy path did not dispatch"
    detail = record["detail"]
    assert detail["from_type"] == "cache.t4g.micro" \
        and detail["to_type"] == "cache.r7g.large", \
        f"the operation does not record what it moves between: {detail}"
    assert detail["size"] == "large", f"the size key was not recorded: {detail}"
    assert detail["impact"] == "rolling", \
        f"failover + a replica must resize rolling, not {detail['impact']!r}"
    assert detail["downsize"] is False, \
        f"micro -> large recorded downsize={detail['downsize']!r}"
    assert detail["monthly_usd"], \
        f"the target size's monthly cost was not recorded: {detail}"
    _clear_claims()


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


# ── service: database guards, per instance ──────────────────────────────────

@th.django_unit_test("resize_database targets ONE named instance, tier by role")
def test_resize_database_is_per_instance(opts):
    from mojo.apps.aws.services import capacity

    rds_client = _rds_client(
        [_db_instance(WRITER, cls="db.r6g.xlarge", cluster=CLUSTER),
         _db_instance(READER_A, cls="db.t4g.medium", cluster=CLUSTER),
         _db_instance(READER_B, cls="db.t4g.medium", cluster=CLUSTER)],
        [_aurora_cluster()])

    with mock.patch.object(capacity, "_dispatch") as dispatched:
        writer_op = capacity.apply(
            _actor(), capacity.ACTION_RESIZE_DATABASE, WRITER,
            size="xlarge", apply_immediately=True, rds_client=rds_client)
        reader_a = capacity.apply(
            _actor(), capacity.ACTION_RESIZE_DATABASE, READER_A,
            size="medium", apply_immediately=True, rds_client=rds_client)
        reader_b = capacity.apply(
            _actor(), capacity.ACTION_RESIZE_DATABASE, READER_B,
            size="medium", apply_immediately=True, rds_client=rds_client)
    assert dispatched.call_count == 3, \
        (f"three instances of one cluster dispatched {dispatched.call_count} "
         f"operations — sizing is per instance, and two readers must claim "
         f"independently")

    assert writer_op["detail"]["role"] == "writer" \
        and writer_op["detail"]["promotion_tier"] == 0, \
        f"the writer was not recorded at tier 0: {writer_op['detail']}"
    assert writer_op["detail"]["kind"] == "aurora" \
        and writer_op["detail"]["cluster"] == CLUSTER, \
        f"the writer's cluster context is missing: {writer_op['detail']}"
    for record in (reader_a, reader_b):
        assert record["detail"]["role"] == "reader" \
            and record["detail"]["promotion_tier"] == 1, \
            f"a reader was not recorded at tier 1: {record['detail']}"
        assert record["detail"]["from_class"] == "db.t4g.medium" \
            and record["detail"]["to_class"] == "db.r6g.large", \
            f"a reader's class move was not recorded: {record['detail']}"

    assert capacity._claim_key(capacity.ACTION_RESIZE_DATABASE, READER_A) != \
        capacity._claim_key(capacity.ACTION_RESIZE_DATABASE, READER_B), \
        "two readers share one claim key — resizes on different instances " \
        "must not serialize on each other"
    _clear_claims()


@th.django_unit_test("the database resize guards refuse before any mutation")
def test_resize_database_guards(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import rds as rds_helper

    def refuse(client, resource, expected_code, **params):
        with mock.patch.object(capacity, "_dispatch") as dispatched:
            with th.assert_raises(capacity.CapacityError) as caught:
                capacity.apply(_actor(), capacity.ACTION_RESIZE_DATABASE,
                               resource, rds_client=client, **params)
        assert dispatched.call_count == 0, \
            f"a {expected_code} refusal still started an operation"
        assert caught.exception.error_code == expected_code, \
            (f"the wrong refusal for {resource}: "
             f"{caught.exception.error_code} != {expected_code}")
        return caught.exception

    error = refuse(_rds_client(), "no-such-instance", "resource_not_found",
                   size="large", apply_immediately=True)
    assert error.status == 404, \
        f"an unknown instance answered {error.status}"
    refuse(_rds_client([_db_instance(STANDALONE, cls="db.r6g.large")]),
           STANDALONE, "no_change", size="medium", apply_immediately=True)
    # deleting stays refused — that is the remove_reader race this guard closes.
    error = refuse(
        _rds_client([_db_instance(STANDALONE, status="deleting")]),
        STANDALONE, "not_settled", size="large", apply_immediately=True)
    assert "deleting" in error.message, \
        (f"the not_settled refusal did not quote the provider state: "
         f"{error.message}")

    # backing-up is a routine background window; the resize proceeds.
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        primary = capacity.apply(
            _actor(), capacity.ACTION_RESIZE_DATABASE, STANDALONE,
            size="large", apply_immediately=True,
            rds_client=_rds_client(
                [_db_instance(STANDALONE, status="backing-up")]))
    assert dispatched.call_count == 1, \
        "a backing-up instance could not be resized"
    assert primary["detail"]["role"] == "writer" \
        and primary["detail"]["promotion_tier"] is None, \
        (f"a standalone primary must be a tier-less writer: "
         f"{primary['detail']}")
    _clear_claims()

    with mock.patch.object(capacity, "_dispatch") as dispatched:
        replica = capacity.apply(
            _actor(), capacity.ACTION_RESIZE_DATABASE, STANDALONE_REPLICA,
            size="large", apply_immediately=True,
            rds_client=_rds_client([
                _db_instance(STANDALONE_REPLICA, cls="db.t4g.medium",
                             replica_of=STANDALONE)]))
    assert replica["detail"]["role"] == "reader" \
        and replica["detail"]["promotion_tier"] is None, \
        (f"a standalone replica must be a tier-less reader: "
         f"{replica['detail']}")
    _clear_claims()

    # Neither standalone shape may claim a failover tier in its note, and
    # each keeps its role's honest wording.
    for record, wording in ((primary, "downtime"),
                            (replica, "writer kept serving")):
        run = _resize_record(capacity, capacity.ACTION_RESIZE_DATABASE,
                             record["resource"], record["detail"])
        settled = {"status": "available",
                   "instance_class": record["detail"]["to_class"]}
        with mock.patch.object(capacity, "_sleep"), \
                mock.patch.object(capacity, "_write_operation",
                                  side_effect=lambda r: r), \
                mock.patch.object(capacity, "invalidate"), \
                mock.patch.object(capacity, "_release"), \
                mock.patch.object(rds_helper, "modify_instance_class"), \
                mock.patch.object(rds_helper, "instance_role",
                                  return_value=settled):
            capacity._run_resize_database(run)
        assert run["state"] == "done", f"the resize did not finish: {run}"
        assert "tier" not in run["message"].lower(), \
            (f"a standalone {record['detail']['role']} claimed a failover "
             f"tier it does not have: {run['message']}")
        assert wording in run["message"], \
            (f"a standalone {record['detail']['role']} lost its honest "
             f"wording: {run['message']}")


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


# ── the runners ─────────────────────────────────────────────────────────────

@th.django_unit_test("the resize runners settle on proof and time out honestly")
def test_resize_runners_settle_and_time_out(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import elasticache as elasticache_helper
    from mojo.helpers.aws import rds as rds_helper

    # Cache, rolling, downsized: finishes only when the fresh read shows the
    # new type on a settled group, and the note carries the interruption and
    # the memory caution.
    record = _resize_record(capacity, capacity.ACTION_RESIZE_CACHE, CACHE_GROUP, {
        "from_type": "cache.r7g.large", "to_type": "cache.t4g.medium",
        "size": "medium", "impact": "rolling", "downsize": True})
    reads = [None,
             {"status": "modifying", "node_type": "cache.r7g.large"},
             {"status": "available", "node_type": "cache.t4g.medium"}]
    with mock.patch.object(capacity, "_sleep"), \
            mock.patch.object(capacity, "_write_operation",
                              side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(elasticache_helper,
                              "modify_replication_group_node_type") as modified, \
            mock.patch.object(elasticache_helper, "replication_group_facts",
                              side_effect=reads) as read:
        capacity._run_resize_cache(record)
    assert modified.call_args == mock.call(CACHE_GROUP, "cache.t4g.medium", True), \
        f"the modify was not the recorded target: {modified.call_args}"
    assert read.call_count == 3, \
        (f"the runner settled after {read.call_count} read(s) — a transient "
         f"miss and a modifying read must both keep it polling")
    assert record["state"] == "done", f"a settled resize did not finish: {record}"
    assert "brief failover" in record["message"], \
        f"a rolling resize did not say so: {record['message']}"
    assert "maxmemory" in record["message"], \
        f"a downsize did not carry the memory caution: {record['message']}"

    # Cache, no replica: the note owns the downtime instead of dressing it up.
    record = _resize_record(capacity, capacity.ACTION_RESIZE_CACHE, CACHE_GROUP, {
        "from_type": "cache.t4g.micro", "to_type": "cache.t4g.medium",
        "size": "medium", "impact": "downtime", "downsize": False})
    with mock.patch.object(capacity, "_sleep"), \
            mock.patch.object(capacity, "_write_operation",
                              side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(elasticache_helper,
                              "modify_replication_group_node_type"), \
            mock.patch.object(elasticache_helper, "replication_group_facts",
                              return_value={"status": "available",
                                            "node_type": "cache.t4g.medium"}):
        capacity._run_resize_cache(record)
    assert record["state"] == "done" and "down while" in record["message"], \
        f"a no-replica resize did not own its downtime: {record['message']}"
    assert "maxmemory" not in record["message"], \
        f"an upsize carried a downsize caution: {record['message']}"

    # Cache, never settles: resize_timeout once the deadline passes.
    record = _resize_record(capacity, capacity.ACTION_RESIZE_CACHE, CACHE_GROUP, {
        "from_type": "cache.t4g.micro", "to_type": "cache.t4g.medium",
        "size": "medium", "impact": "rolling", "downsize": False})
    fake_time = mock.Mock()
    fake_time.time.side_effect = [0, 1, 2, 999999]
    with mock.patch.object(capacity, "time", fake_time), \
            mock.patch.object(capacity, "_write_operation",
                              side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(elasticache_helper,
                              "modify_replication_group_node_type"), \
            mock.patch.object(elasticache_helper, "replication_group_facts",
                              return_value={"status": "modifying",
                                            "node_type": "cache.t4g.micro"}):
        capacity._run_resize_cache(record)
    assert record["state"] == "failed" \
        and record["error_code"] == "resize_timeout", \
        f"a never-settling group did not fail resize_timeout: {record}"
    assert "ElastiCache console" in record["message"], \
        f"the timeout does not name where to look: {record['message']}"

    # Database, Aurora reader: waits for the new class, keeps the tier honest.
    record = _resize_record(capacity, capacity.ACTION_RESIZE_DATABASE, READER_A, {
        "kind": "aurora", "cluster": CLUSTER, "role": "reader",
        "from_class": "db.t4g.medium", "to_class": "db.r6g.large",
        "size": "medium", "promotion_tier": 1})
    reads = [None,
             {"status": "modifying", "instance_class": "db.t4g.medium"},
             {"status": "available", "instance_class": "db.t4g.medium"},
             {"status": "available", "instance_class": "db.r6g.large"}]
    with mock.patch.object(capacity, "_sleep"), \
            mock.patch.object(capacity, "_write_operation",
                              side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(rds_helper, "modify_instance_class") as modified, \
            mock.patch.object(rds_helper, "instance_role",
                              side_effect=reads) as read:
        capacity._run_resize_database(record)
    assert modified.call_args == mock.call(READER_A, "db.r6g.large", True,
                                           promotion_tier=1), \
        f"the reader modify did not carry tier 1: {modified.call_args}"
    assert read.call_count == 4, \
        (f"the runner settled after {read.call_count} read(s) — available on "
         f"the OLD class must keep it polling")
    assert record["state"] == "done", f"a settled resize did not finish: {record}"
    assert "writer kept serving" in record["message"] \
        and "tier 1" in record["message"], \
        f"the reader note is wrong: {record['message']}"

    # Database, never settles: resize_timeout.
    record = _resize_record(capacity, capacity.ACTION_RESIZE_DATABASE, WRITER, {
        "kind": "aurora", "cluster": CLUSTER, "role": "writer",
        "from_class": "db.r6g.xlarge", "to_class": "db.r6g.2xlarge",
        "size": "xlarge", "promotion_tier": 0})
    fake_time = mock.Mock()
    fake_time.time.side_effect = [0, 1, 2, 999999]
    with mock.patch.object(capacity, "time", fake_time), \
            mock.patch.object(capacity, "_write_operation",
                              side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(rds_helper, "modify_instance_class"), \
            mock.patch.object(rds_helper, "instance_role",
                              return_value={"status": "modifying",
                                            "instance_class": "db.r6g.xlarge"}):
        capacity._run_resize_database(record)
    assert record["state"] == "failed" \
        and record["error_code"] == "resize_timeout", \
        f"a never-settling instance did not fail resize_timeout: {record}"
    assert "RDS console" in record["message"], \
        f"the timeout does not name where to look: {record['message']}"


# ── REST ────────────────────────────────────────────────────────────────────

def _view(name):
    import inspect
    from mojo.apps.aws.rest import capacity as views
    return inspect.unwrap(getattr(views, name))


def _request(user, **data):
    from objict import objict
    return SimpleNamespace(user=user, DATA=objict(**data), META={})


def _superuser(pk=1):
    user = mock.Mock(is_superuser=True, pk=pk, username=f"user-{pk}")
    user.has_permission.return_value = True
    return user


@th.django_unit_test("a resize states its size and its window, or it is a 400")
def test_apply_resize_rest_fields(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import capacity

    view = _view("on_capacity_apply")
    actor = _superuser()
    base = {"action": "resize_database", "resource": READER_A,
            "confirm_resource": READER_A}
    with mock.patch.object(capacity, "apply") as applied:
        with th.assert_raises(me.ValueException):
            view(_request(actor, **base, apply_immediately=True))
        with th.assert_raises(me.ValueException):
            view(_request(actor, **base, size="medium"))
        # A string is not a boolean, and "false" would be truthy.
        with th.assert_raises(me.ValueException):
            view(_request(actor, **base, size="medium",
                          apply_immediately="true"))
        with th.assert_raises(me.ValueException):
            view(_request(actor, action="resize_cache", resource=CACHE_GROUP,
                          confirm_resource="wrong-group", size="large",
                          apply_immediately=True))
        assert applied.call_count == 0, \
            "an incomplete or misconfirmed resize reached the apply service"

        applied.return_value = {"id": "op-9"}
        with mock.patch("mojo.apps.account.services.admin_platform"
                        ".audit_after_commit") as audit:
            view(_request(actor, **base, size="medium",
                          apply_immediately=True))
        assert applied.call_count == 1, "a complete resize was refused"
        assert applied.call_args.kwargs == {"size": "medium",
                                            "apply_immediately": True}, \
            f"the resize params were not passed through: {applied.call_args}"
        assert audit.call_count == 1, \
            f"one resize wrote {audit.call_count} audit events"
        _, action, target = audit.call_args[0]
        assert action == "aws_capacity_resize_database", \
            f"the audit action is {action!r}"
        assert READER_A in target and "op-9" in target, \
            f"the audit target does not identify what changed: {target!r}"
