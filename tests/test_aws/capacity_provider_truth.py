"""Provider-truth regressions for the Admin capacity surfaces.

These tests exercise the capacity service at its AWS-helper seams.  They prove
that transitional resources remain visible, foreign resources remain outside
the control boundary, and an uncertain mutation can only be reconciled -- it
is never made replayable by releasing its single-flight claim.
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


REGION = "us-west-2"
PROJECT = "mojoland"
ENVIRONMENT = "prod"
NODE = "i-00000000000000001"
CLUSTER = "mojoland-prod-aurora"
WRITER = f"{CLUSTER}-writer"
READER = f"{CLUSTER}-reader-1"
CACHE = "mojoland-prod-cache"
CACHE_ARN = (
    "arn:aws:elasticache:us-west-2:311883575386:replicationgroup:"
    f"{CACHE}"
)


def _spec():
    from mojo.deploy.provision import spec

    return spec.build(PROJECT, ENVIRONMENT, REGION)


def _tags(role):
    return {
        "mojo:project": PROJECT,
        "mojo:env": ENVIRONMENT,
        "mojo:role": role,
        "managed-by": "django-mojo",
    }


def _actor():
    return SimpleNamespace(pk=2585, username="capacity-provider-truth",
                           is_superuser=True)


@th.django_unit_setup()
def setup_capacity_provider_truth(opts):
    from mojo.apps.aws.services import capacity

    capacity.invalidate()


@th.django_unit_test(
    "owned EC2 inventory remains visible before load-balancer registration")
def test_unregistered_node_uses_ec2_lifecycle(opts):
    from mojo.apps.aws.services import capacity

    facts = {
        NODE: {
            "instance_id": NODE,
            "name": "mojoland1",
            "state": "pending",
            "instance_type": "t3.small",
            "availability_zone": "us-west-2a",
            "tags": _tags("node"),
        }
    }
    rows = capacity._node_rows(
        {"balancers": [], "groups": []}, facts, None, "")

    assert len(rows) == 1, f"the unregistered owned node vanished: {rows}"
    row = rows[0]
    assert row["id"] == NODE and row["state"] == "unregistered", row
    assert row["instance_state"] == "pending", row
    assert row["lifecycle_state"] == "creating", row
    assert row["registered"] is False and row["healthy"] is False, row
    assert row["can_drain"] is False, row

    serving = {"balancers": [], "groups": [{
        "arn": "arn:target-group", "targets": [{
            "id": NODE, "state": "unhealthy"}],
    }]}
    registered = capacity._node_rows(serving, {
        NODE: {**facts[NODE], "state": "running"}}, None, "")[0]
    assert registered["healthy"] is False, registered
    assert registered["can_drain"] is True, \
        "a registered unhealthy node could not be safely drained"


@th.django_unit_test(
    "EC2 inventory discovery is bounded by project environment and node tags")
def test_ec2_fleet_inventory_filters_at_provider_boundary(opts):
    from mojo.helpers.aws import ec2

    client = mock.Mock()
    client.describe_instances.return_value = {"Reservations": []}
    assert ec2.fleet_instance_map(
        PROJECT, ENVIRONMENT, client=client, region=REGION) == {}

    filters = client.describe_instances.call_args.kwargs["Filters"]
    by_name = {row["Name"]: row["Values"] for row in filters}
    assert by_name["tag:mojo:project"] == [PROJECT], filters
    assert by_name["tag:mojo:env"] == [ENVIRONMENT], filters
    assert by_name["tag:mojo:role"] == ["node"], filters
    assert "running" in by_name["instance-state-name"] \
        and "pending" in by_name["instance-state-name"], filters


@th.django_unit_test(
    "an ambiguous environment fails closed without account-wide AWS reads")
def test_unresolved_declared_identity_disables_capacity(opts):
    from mojo.apps.aws.services import capacity
    from mojo.apps.aws.services import infra_setup

    problem = {
        "explanation": "multiple environments",
        "remediation": "select one",
        "details": {"environment_count": 2},
    }
    ec2_client = mock.Mock()
    rds_client = mock.Mock()
    cache_client = mock.Mock()
    elbv2_client = mock.Mock()
    with mock.patch.object(infra_setup, "_resolve_spec",
                           return_value=(None, problem)):
        report = capacity._build(
            elbv2_client=elbv2_client, ec2_client=ec2_client,
            rds_client=rds_client, cache_client=cache_client)

    assert report["identity_available"] is False, report
    assert report["warnings"][0]["code"] == "identity", report["warnings"]
    assert all(not row["offered"] for row in report["actions"].values()), \
        report["actions"]
    assert {row["blocked_reason"] for row in report["actions"].values()} == {
        "identity_unavailable"}, report["actions"]
    assert elbv2_client.mock_calls == [] and ec2_client.mock_calls == [], \
        "an unresolved identity still read account-wide network or EC2 state"
    assert rds_client.mock_calls == [] and cache_client.mock_calls == [], \
        "an unresolved identity still read account-wide data services"


@th.django_unit_test(
    "RDS member state is per instance and foreign clusters are excluded")
def test_database_rows_use_owned_member_truth(opts):
    from mojo.apps.aws.services import capacity

    clusters = {
        CLUSTER: {"status": "available", "tags": _tags("database")},
        "foreign-prod-aurora": {
            "status": "available",
            "tags": {**_tags("database"), "mojo:project": "foreign"},
        },
    }
    instances = {
        WRITER: {
            "status": "available", "instance_class": "db.t4g.medium",
            "tags": _tags("database"),
        },
        READER: {
            "status": "creating", "instance_class": "db.t4g.medium",
            "tags": _tags("database"),
        },
    }
    detail = {
        "engine": "aurora-postgresql",
        "writer": WRITER,
        "readers": [READER],
        "endpoint": "writer.example.test",
        "reader_endpoint": "reader.example.test",
    }
    with mock.patch.object(capacity.rds_helper, "cluster_statuses",
                           return_value=clusters), \
            mock.patch.object(capacity.rds_helper, "instance_statuses",
                              return_value=instances), \
            mock.patch.object(capacity.rds_helper, "cluster_members",
                              return_value=detail) as members, \
            mock.patch.object(capacity.rds_helper, "instance_role",
                              return_value={"cluster": CLUSTER}):
        rows = capacity._database_rows(fleet_spec=_spec())

    assert [row["identifier"] for row in rows] == [CLUSTER], rows
    assert members.call_count == 1, "foreign cluster membership was inspected"
    row = rows[0]
    member_map = {member["id"]: member for member in row["members"]}
    assert member_map[WRITER]["lifecycle_state"] == "available", member_map
    assert member_map[WRITER]["can_resize"] is True, member_map
    assert member_map[READER]["status"] == "creating", member_map
    assert member_map[READER]["lifecycle_state"] == "creating", member_map
    assert member_map[READER]["can_remove"] is False, member_map
    assert row["blocked_reason"] == "resource_transitioning", row


@th.django_unit_test(
    "ElastiCache member state is per node and foreign groups are excluded")
def test_cache_rows_use_owned_member_truth(opts):
    from mojo.apps.aws.services import capacity

    owned = {
        "identifier": CACHE,
        "arn": CACHE_ARN,
        "status": "available",
        "replica_count": 1,
        "cluster_enabled": False,
        "automatic_failover_on": True,
        "multi_az_on": True,
        "node_type": "cache.t4g.micro",
        "members": [
            {"id": f"{CACHE}-001", "role": "primary"},
            {"id": f"{CACHE}-002", "role": "replica"},
        ],
    }
    foreign = {**owned, "identifier": "foreign-prod-cache",
               "arn": CACHE_ARN + "-foreign"}
    statuses = {
        CACHE: {"members": [
            {"id": f"{CACHE}-001", "status": "available"},
            {"id": f"{CACHE}-002", "status": "deleting"},
        ]}
    }

    def tags(arn, **kwargs):
        return _tags("cache") if arn == CACHE_ARN else {
            **_tags("cache"), "mojo:project": "foreign"}

    with mock.patch.object(capacity.elasticache_helper, "replication_groups",
                           return_value=[owned, foreign]), \
            mock.patch.object(capacity.elasticache_helper, "group_statuses",
                              return_value=statuses), \
            mock.patch.object(
                capacity.elasticache_helper, "replication_group_tags",
                side_effect=tags):
        rows = capacity._cache_rows(fleet_spec=_spec())

    assert [row["identifier"] for row in rows] == [CACHE], rows
    members = {member["id"]: member for member in rows[0]["members"]}
    assert members[f"{CACHE}-001"]["lifecycle_state"] == "available", members
    assert members[f"{CACHE}-002"]["status"] == "deleting", members
    assert members[f"{CACHE}-002"]["lifecycle_state"] == "deleting", members
    assert rows[0]["blocked_reason"] == "resource_transitioning", rows[0]


@th.django_unit_test(
    "an uncertain AWS mutation holds its claim and exposes only safe detail")
def test_uncertain_mutation_is_reconcile_only(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws.provider_call import ProviderCallError

    claim = capacity._claim_key(capacity.ACTION_ADD_READER, CLUSTER)
    record = capacity._new_operation(
        capacity.ACTION_ADD_READER, CLUSTER, _actor(), claim,
        {"cluster": CLUSTER})
    unsafe = "expired-secret-value@example.test"
    failure = ProviderCallError(
        "rds.create_db_instance", unsafe,
        "rds:CreateDBInstance", "request-12345678", True, "unknown", False)

    with mock.patch.object(capacity, "_run_add_reader", side_effect=failure), \
            mock.patch.object(capacity, "_release") as released, \
            mock.patch.object(capacity, "invalidate"):
        state = capacity.run_operation(record["id"])

    final = capacity._read_operation(record["id"])
    assert state == capacity.STATE_FAILED and final["state"] == state, final
    assert final["error_code"] == "mutation_state_unknown", final
    assert "do not replay" in final["message"].lower(), final
    assert released.call_count == 0, "an uncertain mutation released its claim"
    rendered = repr(final)
    assert unsafe not in rendered, "an unsafe provider value reached the record"
    failure_detail = final["detail"]["failure"]
    assert failure_detail["provider_code"] == "provider_error", failure_detail
    assert failure_detail["mutation_state"] == "unknown", failure_detail
