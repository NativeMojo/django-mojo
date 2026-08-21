"""Admin capacity actions: the AWS request shapes, the guards, and the gate.

The helper tests use ``botocore.stub.Stubber`` rather than a plain Mock, for
the same reason ``maintenance.py`` does and one more. A Mock accepts any
keyword, so it would happily accept ``MetadataOptions`` spelled wrong, an
``IamInstanceProfile`` shaped wrong, or a ``ReplicasToRemove`` on a decrease we
deliberately never send. Stubber validates every request against the real
service model, and botocore's own parameter validator runs before the network
would, so a member this code invents fails locally.

Where a value (not just a member name) is the thing under test — the IMDSv2
hostname lines inside UserData, the exact tag set — a captured Mock call is
used ALONGSIDE a Stubber call, never instead of one: Stubber proves the request
is legal, the capture proves it says what it should.
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


REGION = "us-east-1"
BALANCER_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                "loadbalancer/net/mojo-api-nlb/0123456789abcdef")
GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
             "targetgroup/mojo-api/abcdef0123456789")
OTHER_GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                   "targetgroup/mojo-internal/1111111111111111")
NODE_A = "i-0a1b2c3d4e5f60011"
NODE_B = "i-0a1b2c3d4e5f60022"
NEW_NODE = "i-0a1b2c3d4e5f60044"
PROFILE_ARN = "arn:aws:iam::123456789012:instance-profile/mojo-api"
CLUSTER = "mojo-test-aurora"
STANDALONE = "mojo-test-postgres"
CACHE_GROUP = "mojo-test-redis"


# ── fixtures ────────────────────────────────────────────────────────────────

def _stub(service):
    """A real bounded client plus its Stubber. Credentials are never used."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name=REGION,
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def _instance(instance_id, name, state="running", subnet="subnet-0aaa",
              zone="us-east-1a", profile=PROFILE_ARN, dns="ip-10-0-1-11"):
    row = {
        "InstanceId": instance_id, "InstanceType": "m6i.large",
        "ImageId": "ami-0source", "SubnetId": subnet, "VpcId": "vpc-0aaa",
        "State": {"Name": state}, "Placement": {"AvailabilityZone": zone},
        "PrivateIpAddress": "10.0.1.11",
        "PrivateDnsName": f"{dns}.ec2.internal",
        "SecurityGroups": [{"GroupId": "sg-0aaa"}, {"GroupId": "sg-0bbb"}],
        "Tags": [{"Key": "Name", "Value": name}],
    }
    if profile:
        row["IamInstanceProfile"] = {"Arn": profile}
    return row


def _ec2_client(instances=()):
    client = mock.Mock()
    client.describe_instances.return_value = {
        "Reservations": [{"Instances": list(instances)}]}
    return client


def _target(instance_id, state, port=443):
    return {"Target": {"Id": instance_id, "Port": port},
            "TargetHealth": {"State": state}}


def _elbv2_client(groups=None, health=None):
    """One ELBv2 mock whose describe_target_health answers PER GROUP ARN."""
    groups = groups if groups is not None else [
        {"TargetGroupArn": GROUP_ARN, "TargetGroupName": "mojo-api",
         "TargetType": "instance", "Protocol": "TCP", "Port": 443,
         "LoadBalancerArns": [BALANCER_ARN]}]
    health = health or {GROUP_ARN: [_target(NODE_A, "healthy"),
                                    _target(NODE_B, "healthy")]}
    client = mock.Mock()
    client.describe_load_balancers.return_value = {"LoadBalancers": [{
        "LoadBalancerArn": BALANCER_ARN, "LoadBalancerName": "mojo-api-nlb",
        "Type": "network", "Scheme": "internet-facing",
        "State": {"Code": "active"}, "DNSName": "nlb.example.com",
        "AvailabilityZones": [{"ZoneName": "us-east-1a",
                               "LoadBalancerAddresses": []}]}]}
    client.describe_target_groups.return_value = {"TargetGroups": list(groups)}
    client.describe_target_health.side_effect = lambda TargetGroupArn: {
        "TargetHealthDescriptions": list(health.get(TargetGroupArn, []))}
    client.describe_target_group_attributes.return_value = {
        "Attributes": [{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}]}
    return client


def _rds_client(clusters=(), instances=()):
    client = mock.Mock()
    client.describe_db_clusters.return_value = {"DBClusters": list(clusters)}
    client.describe_db_instances.return_value = {"DBInstances": list(instances)}
    return client


def _cache_client(groups=()):
    client = mock.Mock()
    client.describe_replication_groups.return_value = {
        "ReplicationGroups": list(groups)}
    return client


def _cache_group(replicas=1, failover="enabled", multi_az="enabled",
                 cluster_enabled=False):
    members = [{"CacheClusterId": f"{CACHE_GROUP}-001", "CurrentRole": "primary"}]
    for index in range(replicas):
        members.append({"CacheClusterId": f"{CACHE_GROUP}-{index + 2:03d}",
                        "CurrentRole": "replica"})
    return {
        "ReplicationGroupId": CACHE_GROUP, "Status": "available",
        "ClusterEnabled": cluster_enabled, "AutomaticFailover": failover,
        "MultiAZ": multi_az,
        "NodeGroups": [{"NodeGroupId": "0001", "NodeGroupMembers": members}],
        "MemberClusters": [member["CacheClusterId"] for member in members],
    }


def _actor(pk=1):
    return SimpleNamespace(pk=pk, username="capacity-actor", is_superuser=True)


def _clear_claims():
    from mojo.apps.aws.services import capacity
    for action in capacity.ACTIONS:
        for resource in ("fleet", NODE_A, NODE_B, NEW_NODE, CLUSTER,
                         STANDALONE, CACHE_GROUP, f"{CLUSTER}-2"):
            capacity._release(capacity._claim_key(action, resource))
    capacity.invalidate()


@th.django_unit_setup()
def setup_capacity(opts):
    # Claims live in a shared cache with a 90-minute TTL, so a previous run's
    # key would refuse this one. Delete what this module will create.
    _clear_claims()


# ── helpers: EC2 request shapes ─────────────────────────────────────────────

@th.django_unit_test("an image of a serving node is captured WITHOUT rebooting it")
def test_capture_image_never_reboots(opts):
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    stubber.add_response("create_image", {"ImageId": "ami-0new"}, {
        "InstanceId": NODE_A, "Name": "mojo-fleet-test", "NoReboot": True,
        "Description": "admin capacity clone source " + NODE_A,
        "TagSpecifications": [{"ResourceType": "image", "Tags": [
            {"Key": "Name", "Value": "mojo-fleet-test"},
            {"Key": "mojo:fleet-image", "Value": "admin-capacity"}]}]})
    with stubber:
        image = ec2.capture_image(NODE_A, "mojo-fleet-test", "admin-capacity",
                                  client=client)
    assert image == "ami-0new", f"the captured image id was not returned: {image!r}"
    stubber.assert_no_pending_responses()


@th.django_unit_test("a clone launch is a request the EC2 service model accepts")
def test_launch_clone_is_model_valid(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    # No expected_params: the point of THIS test is that botocore's own
    # validator accepts every member we send — MetadataOptions,
    # IamInstanceProfile, TagSpecifications — against the real service model. A
    # Mock would accept a misspelling of any of them.
    stubber.add_response("run_instances", {"Instances": [{"InstanceId": NEW_NODE}]})
    source = {"instance_type": "m6i.large", "subnet_id": "subnet-0aaa",
              "security_group_ids": ["sg-0aaa"],
              "iam_instance_profile_arn": PROFILE_ARN}
    with stubber:
        created = ec2.launch_clone(
            source, "ami-0new", "subnet-0aaa", "mojo-api-a-clone",
            capacity.node_user_data("mojo-api-a"), client=client)
    assert created == NEW_NODE, f"the new instance id was not returned: {created!r}"
    stubber.assert_no_pending_responses()


@th.django_unit_test("a clone forces IMDSv2, carries the source role, and names itself")
def test_launch_clone_values(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2

    client = mock.Mock()
    client.run_instances.return_value = {"Instances": [{"InstanceId": NEW_NODE}]}
    source = {"instance_type": "m6i.large", "subnet_id": "subnet-0aaa",
              "security_group_ids": ["sg-0aaa", "sg-0bbb"],
              "iam_instance_profile_arn": PROFILE_ARN}
    ec2.launch_clone(source, "ami-0new", "subnet-0aaa", "mojo-api-a-clone",
                     capacity.node_user_data("mojo-api-a"),
                     tags={"mojo:role": "app"}, client=client)
    params = client.run_instances.call_args.kwargs

    assert params["MetadataOptions"]["HttpTokens"] == "required", \
        f"a clone was launched with IMDSv1 reachable: {params['MetadataOptions']}"
    assert params["IamInstanceProfile"] == {"Arn": PROFILE_ARN}, \
        f"the source's instance role was not carried onto the clone: {params}"
    assert params["InstanceType"] == "m6i.large" \
        and params["SubnetId"] == "subnet-0aaa" \
        and params["SecurityGroupIds"] == ["sg-0aaa", "sg-0bbb"], \
        f"the clone does not match its source's placement: {params}"
    assert params["MinCount"] == 1 and params["MaxCount"] == 1, \
        f"one add must launch exactly one node: {params}"

    user_data = params["UserData"]
    assert "169.254.169.254/latest/api/token" in user_data, \
        "the clone's user-data does not fetch an IMDSv2 token"
    assert "X-aws-ec2-metadata-token:" in user_data, \
        "the clone's user-data reads metadata without the IMDSv2 token header"
    assert "hostnamectl set-hostname" in user_data and "${IID##*-}" in user_data, \
        f"the clone does not take a hostname derived from its instance id:\n{user_data}"
    assert "config-sync.service" in user_data, \
        "the clone never pulls its own configuration"
    assert "mojo.deploy.node_setup" in user_data, \
        "the clone never converges its units and cron"
    assert "rm " not in user_data, \
        f"the clone's user-data deletes something; baked config must survive:\n{user_data}"

    tags = {tag["Key"]: tag["Value"]
            for tag in params["TagSpecifications"][0]["Tags"]}
    assert tags.get("mojo:created-by") == "admin-capacity", \
        f"a capacity-added node is not identifiable from its tags: {tags}"
    assert tags.get("Name") == "mojo-api-a-clone", f"the clone is unnamed: {tags}"
    assert tags.get("mojo:role") == "app", f"caller tags were dropped: {tags}"


@th.django_unit_test("the server predicts the hostname its own user-data will set")
def test_expected_node_id_matches_user_data(opts):
    from mojo.apps.aws.services import capacity

    node_id = capacity.expected_node_id("mojo-api-a", NEW_NODE)
    suffix = NEW_NODE.rsplit("-", 1)[-1]
    assert node_id == f"mojo-api-a-{suffix}", \
        f"the predicted node id is not the shell expansion's answer: {node_id}"
    assert capacity.expected_runner_id(node_id) == f"{node_id}-engine", \
        "the runner id is not the node id plus the engine suffix"
    # The shell writes "<base>-${IID##*-}"; that expansion is a rsplit on '-'.
    assert f'"mojo-api-a-${{IID##*-}}"' in capacity.node_user_data("mojo-api-a"), \
        "the user-data no longer derives the hostname the server predicts"


# ── helpers: ELBv2 request shapes ───────────────────────────────────────────

@th.django_unit_test("register and deregister send one exact target each")
def test_target_registration_shapes(opts):
    from mojo.helpers.aws import elbv2

    client, stubber = _stub("elbv2")
    stubber.add_response("register_targets", {}, {
        "TargetGroupArn": GROUP_ARN, "Targets": [{"Id": NEW_NODE, "Port": 443}]})
    stubber.add_response("deregister_targets", {}, {
        "TargetGroupArn": GROUP_ARN, "Targets": [{"Id": NODE_B}]})
    with stubber:
        elbv2.register_target(GROUP_ARN, NEW_NODE, 443, client=client)
        elbv2.deregister_target(GROUP_ARN, NODE_B, client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("a draining target is never reported as drained")
def test_draining_is_not_drained(opts):
    from mojo.helpers.aws import elbv2

    assert elbv2.drained([{"state": "draining"}]) is False, \
        "a draining target was treated as finished draining"
    assert elbv2.drained([{"state": "unhealthy.draining"}]) is False, \
        "an unhealthy draining target was treated as finished draining"
    assert elbv2.drained([{"state": "healthy"}]) is False, \
        "a healthy target was treated as drained"
    assert elbv2.drained([{"state": "unused"}]) is True, \
        "an unused target is drained and was not reported so"
    assert elbv2.drained([]) is True, \
        "a target that is gone from the group is drained"


# ── helpers: RDS reader dispatch ────────────────────────────────────────────

@th.django_unit_test("an Aurora reader is create_db_instance, never a read replica")
def test_aurora_reader_dispatch(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    stubber.add_response("create_db_instance", {"DBInstance": {}}, {
        "DBInstanceIdentifier": f"{CLUSTER}-reader-abcd1234",
        "DBClusterIdentifier": CLUSTER,
        "DBInstanceClass": "db.r6g.large",
        "Engine": "aurora-postgresql"})
    with stubber:
        rds.create_cluster_reader(
            CLUSTER, f"{CLUSTER}-reader-abcd1234", "db.r6g.large",
            "aurora-postgresql", client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("a standalone reader is create_db_instance_read_replica")
def test_standalone_reader_dispatch(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    stubber.add_response("create_db_instance_read_replica", {"DBInstance": {}}, {
        "DBInstanceIdentifier": f"{STANDALONE}-reader-abcd1234",
        "SourceDBInstanceIdentifier": STANDALONE,
        "DBInstanceClass": "db.m6g.large"})
    with stubber:
        rds.create_read_replica(
            STANDALONE, f"{STANDALONE}-reader-abcd1234", "db.m6g.large",
            client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("a reader delete takes no final snapshot")
def test_reader_delete_skips_snapshot(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    stubber.add_response("delete_db_instance", {"DBInstance": {}}, {
        "DBInstanceIdentifier": f"{CLUSTER}-2", "SkipFinalSnapshot": True})
    with stubber:
        rds.delete_instance(f"{CLUSTER}-2", client=client)
    stubber.assert_no_pending_responses()


# ── helpers: ElastiCache replica count ──────────────────────────────────────

@th.django_unit_test("a replica increase names only the new count")
def test_increase_replica_count_shape(opts):
    from mojo.helpers.aws import elasticache

    client, stubber = _stub("elasticache")
    stubber.add_response("increase_replica_count", {"ReplicationGroup": {}}, {
        "ReplicationGroupId": CACHE_GROUP, "NewReplicaCount": 2,
        "ApplyImmediately": True})
    facts = {"cluster_enabled": False, "replica_count": 1,
             "automatic_failover_on": True, "multi_az_on": True}
    with stubber:
        result = elasticache.set_replica_count(
            CACHE_GROUP, 2, True, facts=facts, client=client)
    assert result["operation"] == "elasticache.increase_replica_count", \
        f"the wrong operation was dispatched: {result}"
    stubber.assert_no_pending_responses()


@th.django_unit_test("a replica decrease NEVER names which node to remove")
def test_decrease_replica_count_sends_no_removals(opts):
    from mojo.helpers.aws import elasticache

    client, stubber = _stub("elasticache")
    # expected_params is exact, so a ReplicasToRemove sneaking in fails here.
    # Choosing the node to kill means choosing which zone loses its standby,
    # and ElastiCache picks better than a portal that cannot see the layout.
    stubber.add_response("decrease_replica_count", {"ReplicationGroup": {}}, {
        "ReplicationGroupId": CACHE_GROUP, "NewReplicaCount": 1,
        "ApplyImmediately": True})
    facts = {"cluster_enabled": False, "replica_count": 2,
             "automatic_failover_on": True, "multi_az_on": True}
    with stubber:
        result = elasticache.set_replica_count(
            CACHE_GROUP, 1, True, facts=facts, client=client)
    assert result["operation"] == "elasticache.decrease_replica_count", \
        f"the wrong operation was dispatched: {result}"
    stubber.assert_no_pending_responses()


@th.django_unit_test("the failover floor and cluster mode are refused before any call")
def test_replica_count_refusals(opts):
    from mojo.helpers.aws import elasticache

    client = mock.Mock()
    failover_on = {"cluster_enabled": False, "replica_count": 1,
                   "automatic_failover_on": True, "multi_az_on": False}
    with th.assert_raises(elasticache.ReplicaCountError) as caught:
        elasticache.set_replica_count(CACHE_GROUP, 0, True, facts=failover_on,
                                      client=client)
    assert caught.exception.reason == elasticache.FAILOVER_REQUIRES_REPLICA, \
        f"the failover floor gave the wrong reason: {caught.exception.reason}"

    sharded = {"cluster_enabled": True, "replica_count": 2,
               "automatic_failover_on": True, "multi_az_on": True}
    with th.assert_raises(elasticache.ReplicaCountError) as caught:
        elasticache.set_replica_count(CACHE_GROUP, 3, True, facts=sharded,
                                      client=client)
    assert caught.exception.reason == elasticache.CLUSTER_MODE_UNSUPPORTED, \
        f"cluster mode gave the wrong reason: {caught.exception.reason}"

    assert client.increase_replica_count.call_count == 0 \
        and client.decrease_replica_count.call_count == 0, \
        "a refused replica-count change still reached the provider"

    # Failover OFF: zero replicas is allowed, and the loss is the caller's to
    # state — the helper does not second-guess an explicit decision.
    failover_off = {"cluster_enabled": False, "replica_count": 1,
                    "automatic_failover_on": False, "multi_az_on": False}
    client.decrease_replica_count.return_value = {"ReplicationGroup": {}}
    result = elasticache.set_replica_count(CACHE_GROUP, 0, True,
                                           facts=failover_off, client=client)
    assert result["changed"] is True and result["replica_count"] == 0, \
        f"a failover-off group could not drop its last replica: {result}"


# ── service: node guards ────────────────────────────────────────────────────

@th.django_unit_test("the last healthy target of ANY attached group cannot be removed")
def test_last_healthy_target_across_groups(opts):
    from mojo.apps.aws.services import capacity

    # NODE_B is one of two healthy targets in the public group, but the ONLY
    # healthy target of a second, internal group. A guard that looked at the
    # dashboard's single balancer row would let this through.
    groups = [
        {"TargetGroupArn": GROUP_ARN, "TargetGroupName": "mojo-api",
         "TargetType": "instance", "Protocol": "TCP", "Port": 443,
         "LoadBalancerArns": [BALANCER_ARN]},
        {"TargetGroupArn": OTHER_GROUP_ARN, "TargetGroupName": "mojo-internal",
         "TargetType": "instance", "Protocol": "TCP", "Port": 8443,
         "LoadBalancerArns": [BALANCER_ARN]},
    ]
    health = {
        GROUP_ARN: [_target(NODE_A, "healthy"), _target(NODE_B, "healthy")],
        OTHER_GROUP_ARN: [_target(NODE_A, "unhealthy", 8443),
                          _target(NODE_B, "healthy", 8443)],
    }
    elbv2_client = _elbv2_client(groups, health)
    ec2_client = _ec2_client([_instance(NODE_A, "mojo-api-a"),
                              _instance(NODE_B, "mojo-api-b")])
    with mock.patch.object(capacity, "_local_hostname", return_value="laptop"), \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_DRAIN_NODE, NODE_B,
                           elbv2_client=elbv2_client, ec2_client=ec2_client)
    assert caught.exception.error_code == "last_healthy_target", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert caught.exception.status == 409, \
        f"a stranding refusal answered {caught.exception.status}"
    assert dispatched.call_count == 0, \
        "a refused drain still started an operation"
    _clear_claims()


@th.django_unit_test("the node answering the request cannot remove itself")
def test_cannot_remove_self(opts):
    from mojo.apps.aws.services import capacity

    elbv2_client = _elbv2_client()
    ec2_client = _ec2_client([_instance(NODE_A, "mojo-api-a", dns="mojo-api-a"),
                              _instance(NODE_B, "mojo-api-b", dns="mojo-api-b")])
    with mock.patch.object(capacity, "_local_hostname", return_value="mojo-api-a"), \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_DRAIN_NODE, NODE_A,
                           elbv2_client=elbv2_client, ec2_client=ec2_client)
    assert caught.exception.error_code == "cannot_remove_self", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert dispatched.call_count == 0, "a self-removal still started an operation"
    _clear_claims()


@th.django_unit_test("an unmatchable hostname reports 'unavailable', never a clean pass")
def test_self_check_unavailable_is_honest(opts):
    from mojo.apps.aws.services import capacity

    elbv2_client = _elbv2_client()
    ec2_client = _ec2_client([_instance(NODE_A, "mojo-api-a"),
                              _instance(NODE_B, "mojo-api-b")])
    with mock.patch.object(capacity, "_local_hostname", return_value="laptop"), \
            mock.patch.object(capacity, "_dispatch"):
        record = capacity.apply(_actor(), capacity.ACTION_DRAIN_NODE, NODE_A,
                                elbv2_client=elbv2_client, ec2_client=ec2_client)
    assert record["detail"]["self_check"] == "unavailable", \
        (f"an unresolvable self check was recorded as "
         f"{record['detail']['self_check']!r} — absent evidence must never read "
         f"as a passed check")

    envelope = capacity.report(elbv2_client=elbv2_client, ec2_client=ec2_client,
                               rds_client=_rds_client(), cache_client=_cache_client())
    assert envelope["nodes"]["self_check"] == "unavailable", \
        f"the report claimed a self check it could not make: {envelope['nodes']}"
    assert envelope["nodes"]["self"] is None, \
        "the report named a self instance it could not identify"
    _clear_claims()


@th.django_unit_test("a node that has not finished draining cannot be terminated")
def test_terminate_requires_a_proven_drain(opts):
    from mojo.apps.aws.services import capacity

    ec2_client = _ec2_client([_instance(NODE_A, "mojo-api-a"),
                              _instance(NODE_B, "mojo-api-b")])
    for state in ("healthy", "draining", "unhealthy.draining"):
        elbv2_client = _elbv2_client(health={GROUP_ARN: [
            _target(NODE_A, "healthy"), _target(NODE_B, state)]})
        with mock.patch.object(capacity, "_local_hostname", return_value="laptop"), \
                mock.patch.object(capacity, "_dispatch") as dispatched:
            with th.assert_raises(capacity.CapacityError) as caught:
                capacity.apply(_actor(), capacity.ACTION_TERMINATE_NODE, NODE_B,
                               elbv2_client=elbv2_client, ec2_client=ec2_client)
        assert caught.exception.error_code == "not_drained", \
            f"a {state} target was terminable: {caught.exception.error_code}"
        assert caught.exception.status == 409, \
            f"a not-drained refusal answered {caught.exception.status}"
        assert dispatched.call_count == 0, \
            f"a {state} target still started a terminate"

    # `unused` IS drained, and the same call now proceeds.
    elbv2_client = _elbv2_client(health={GROUP_ARN: [
        _target(NODE_A, "healthy"), _target(NODE_B, "unused")]})
    with mock.patch.object(capacity, "_local_hostname", return_value="laptop"), \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        record = capacity.apply(_actor(), capacity.ACTION_TERMINATE_NODE, NODE_B,
                                elbv2_client=elbv2_client, ec2_client=ec2_client)
    assert dispatched.call_count == 1 and record["action"] == "terminate_node", \
        "a drained node could not be terminated"
    _clear_claims()


@th.django_unit_test("a pinned EDGE_NODE_ID blocks adding a node, in the report and the apply")
def test_node_id_pinned_blocks_add(opts):
    from mojo.apps.aws.services import capacity

    elbv2_client = _elbv2_client()
    ec2_client = _ec2_client([_instance(NODE_A, "mojo-api-a"),
                              _instance(NODE_B, "mojo-api-b")])
    settings_map = {"EDGE_NODE_ID": "fleet-node", "AWS_REGION": REGION}
    with mock.patch.object(capacity, "_setting",
                           side_effect=lambda name, default=None:
                           settings_map.get(name, default)), \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_ADD_NODE,
                           elbv2_client=elbv2_client, ec2_client=ec2_client)
        envelope = capacity.report(
            elbv2_client=elbv2_client, ec2_client=ec2_client,
            rds_client=_rds_client(), cache_client=_cache_client())
    assert caught.exception.error_code == "node_id_pinned", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert dispatched.call_count == 0, "a refused add still started an operation"
    assert envelope["actions"]["add_node"] == {
        "offered": False, "blocked_reason": "node_id_pinned"}, \
        f"the report still offered add_node: {envelope['actions']['add_node']}"
    _clear_claims()


@th.django_unit_test("with no healthy node there is nothing to clone")
def test_no_source_node(opts):
    from mojo.apps.aws.services import capacity

    elbv2_client = _elbv2_client(health={GROUP_ARN: [
        _target(NODE_A, "unhealthy"), _target(NODE_B, "unused")]})
    ec2_client = _ec2_client([_instance(NODE_A, "mojo-api-a"),
                              _instance(NODE_B, "mojo-api-b")])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_ADD_NODE,
                           elbv2_client=elbv2_client, ec2_client=ec2_client)
    assert caught.exception.error_code == "no_source_node", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert dispatched.call_count == 0, "a refused add still started an operation"
    _clear_claims()


@th.django_unit_test("the clone source prefers a non-primary node")
def test_source_node_prefers_non_primary(opts):
    from mojo.apps.aws.services import capacity

    ec2_client = _ec2_client([
        _instance(NODE_A, "mojo-api-a", dns="mojo-api-a"),
        _instance(NODE_B, "mojo-api-b", dns="mojo-api-b")])
    with mock.patch.object(capacity, "_primary_host", return_value="mojo-api-a"):
        chosen = capacity._source_node([NODE_A, NODE_B], client=ec2_client)
    assert chosen["instance_id"] == NODE_B, \
        f"the certbot primary was chosen as the clone source: {chosen['instance_id']}"

    # Only the primary is healthy: clone it anyway. A NoReboot image does not
    # interrupt it, and the clone takes its own hostname.
    only_primary = _ec2_client([_instance(NODE_A, "mojo-api-a", dns="mojo-api-a")])
    with mock.patch.object(capacity, "_primary_host", return_value="mojo-api-a"):
        chosen = capacity._source_node([NODE_A], client=only_primary)
    assert chosen["instance_id"] == NODE_A, \
        "a fleet whose only healthy node is the primary could not add capacity"


# ── service: database and cache guards ──────────────────────────────────────

@th.django_unit_test("a primary database is never removable as a reader")
def test_remove_reader_refuses_a_primary(opts):
    from mojo.apps.aws.services import capacity

    rds_client = _rds_client(instances=[{
        "DBInstanceIdentifier": STANDALONE, "DBInstanceStatus": "available",
        "Engine": "postgres", "EngineVersion": "16.4",
        "DBInstanceClass": "db.m6g.large"}])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_REMOVE_READER, STANDALONE,
                           rds_client=rds_client)
    assert caught.exception.error_code == "not_a_reader", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert dispatched.call_count == 0, \
        "a primary database still started a delete operation"

    # An Aurora WRITER is refused for the same reason, through the cluster.
    cluster_client = _rds_client(
        clusters=[{"DBClusterIdentifier": CLUSTER, "Engine": "aurora-postgresql",
                   "Status": "available", "DBClusterMembers": [
                       {"DBInstanceIdentifier": f"{CLUSTER}-1", "IsClusterWriter": True},
                       {"DBInstanceIdentifier": f"{CLUSTER}-2", "IsClusterWriter": False}]}],
        instances=[{"DBInstanceIdentifier": f"{CLUSTER}-1",
                    "DBInstanceStatus": "available", "Engine": "aurora-postgresql",
                    "DBClusterIdentifier": CLUSTER}])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_REMOVE_READER,
                           f"{CLUSTER}-1", rds_client=cluster_client)
    assert caught.exception.error_code == "not_a_reader", \
        f"an Aurora writer was removable: {caught.exception.error_code}"
    assert dispatched.call_count == 0, "an Aurora writer started a delete"
    _clear_claims()


@th.django_unit_test("a cluster-mode cache group is refused by name, before any call")
def test_cache_cluster_mode_refused(opts):
    from mojo.apps.aws.services import capacity

    cache_client = _cache_client([_cache_group(replicas=2, cluster_enabled=True)])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_SET_CACHE_REPLICAS,
                           CACHE_GROUP, count=3, apply_immediately=True,
                           cache_client=cache_client)
    assert caught.exception.error_code == "cluster_mode_unsupported", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert dispatched.call_count == 0, "a cluster-mode group started an operation"
    _clear_claims()


@th.django_unit_test("the replica floor is failover-aware, and refused up front")
def test_cache_failover_floor(opts):
    from mojo.apps.aws.services import capacity

    failover_on = _cache_client([_cache_group(replicas=1, failover="enabled",
                                              multi_az="enabled")])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), capacity.ACTION_SET_CACHE_REPLICAS,
                           CACHE_GROUP, count=0, apply_immediately=True,
                           cache_client=failover_on)
    assert caught.exception.error_code == "automatic_failover_requires_replica", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert dispatched.call_count == 0, "a floor refusal still started an operation"

    # Failover off: zero is allowed, and the report says the floor is zero so
    # the panel can offer it with the loss stated.
    failover_off = _cache_client([_cache_group(replicas=1, failover="disabled",
                                               multi_az="disabled")])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        record = capacity.apply(_actor(), capacity.ACTION_SET_CACHE_REPLICAS,
                                CACHE_GROUP, count=0, apply_immediately=True,
                                cache_client=failover_off)
    assert dispatched.call_count == 1 and record["detail"]["to_count"] == 0, \
        "a failover-off group could not drop to zero replicas"
    _clear_claims()

    envelope = capacity.report(
        elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
        rds_client=_rds_client(), cache_client=failover_on)
    row = envelope["caches"][0]
    assert row["min_replicas"] == 1, \
        f"a failover-enabled group reported a floor of {row['min_replicas']}"


@th.django_unit_test("ElastiCache has no maintenance window, and the apply says so")
def test_cache_requires_immediate_apply(opts):
    from mojo.apps.aws.services import capacity

    cache_client = _cache_client([_cache_group(replicas=1)])
    with mock.patch.object(capacity, "_dispatch") as dispatched:
        with th.assert_raises(capacity.CapacityError):
            capacity.apply(_actor(), capacity.ACTION_SET_CACHE_REPLICAS,
                           CACHE_GROUP, count=2, apply_immediately=False,
                           cache_client=cache_client)
        with th.assert_raises(capacity.CapacityError):
            capacity.apply(_actor(), capacity.ACTION_SET_CACHE_REPLICAS,
                           CACHE_GROUP, apply_immediately=True,
                           cache_client=cache_client)
    assert dispatched.call_count == 0, \
        "an unstated or deferred replica change still started an operation"
    _clear_claims()


# ── service: external mode, single flight, and the join leg ─────────────────

@th.django_unit_test("external infrastructure mode refuses EVERY capacity action")
def test_external_mode_refuses_every_action(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers import infrastructure

    with mock.patch.object(infrastructure, "is_external", return_value=True), \
            mock.patch.object(capacity, "_dispatch") as dispatched:
        for action in capacity.ACTIONS:
            with th.assert_raises(capacity.CapacityError) as caught:
                capacity.apply(_actor(), action, NODE_B, count=2,
                               apply_immediately=True)
            assert caught.exception.error_code == infrastructure.ERROR_CODE, \
                (f"{action} refused with {caught.exception.error_code!r} rather "
                 f"than the infrastructure-mode code")
            assert caught.exception.status == 403, \
                f"{action} answered {caught.exception.status} in external mode"
    assert dispatched.call_count == 0, \
        "an external-mode installation still started an operation"

    # The READ stays available and names the mode, so the panel hides controls
    # rather than the facts.
    with mock.patch.object(infrastructure, "infrastructure_mode",
                           return_value=infrastructure.EXTERNAL):
        envelope = capacity.report(
            elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
            rds_client=_rds_client(), cache_client=_cache_client())
    assert envelope["mode"] == infrastructure.EXTERNAL, \
        f"the report did not name the mode: {envelope['mode']}"
    for name, offer in envelope["actions"].items():
        assert offer == {"offered": False,
                         "blocked_reason": infrastructure.ERROR_CODE}, \
            f"{name} was still offered in external mode: {offer}"


@th.django_unit_test("adds serialize on ONE fixed key, whatever resource they name")
def test_add_node_single_flight_is_literal(opts):
    from mojo.apps.aws.services import capacity

    _clear_claims()
    capacity._claim(capacity.ACTION_ADD_NODE, "fleet", 1)
    # A second add naming a DIFFERENT resource must still collide: two
    # concurrent adds would race on the image, the topology write and the
    # convergence, and a per-resource key would let both through.
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._claim(capacity.ACTION_ADD_NODE, NODE_B, 2)
    assert caught.exception.error_code == "capacity_in_progress", \
        f"a second add was not serialized: {caught.exception.error_code}"
    assert caught.exception.status == 409, \
        f"a serialized add answered {caught.exception.status}"

    # A DIFFERENT action on a different resource is genuinely independent.
    capacity._claim(capacity.ACTION_DRAIN_NODE, NODE_B, 3)
    _clear_claims()


@th.django_unit_test("a cache that cannot answer is a 503, never a go-ahead")
def test_claim_refuses_when_the_cache_is_down(opts):
    from mojo.apps.aws.services import capacity

    broken = mock.Mock()
    broken.add.side_effect = RuntimeError("redis is gone")
    with mock.patch.object(capacity, "cache", broken):
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity._claim(capacity.ACTION_ADD_NODE, "fleet", 1)
    assert caught.exception.error_code == "cache_unavailable", \
        f"the wrong refusal: {caught.exception.error_code}"
    assert caught.exception.status == 503, \
        f"a cache outage answered {caught.exception.status}"

    # `add` returning False with nothing behind it is the SAME outage, not a
    # holder — a silently-doing-nothing backend must not read as "in progress".
    silent = mock.Mock()
    silent.add.return_value = False
    silent.get.return_value = None
    with mock.patch.object(capacity, "cache", silent):
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity._claim(capacity.ACTION_ADD_NODE, "fleet", 1)
    assert caught.exception.status == 503, \
        f"a silent cache backend answered {caught.exception.status}"


def _add_record(capacity):
    return {
        "schema_version": 1, "id": "op-test", "action": capacity.ACTION_ADD_NODE,
        "resource": NODE_A, "actor": None, "started": "", "state": "running",
        "phase": "capturing", "phases": list(capacity.PHASES[capacity.ACTION_ADD_NODE]),
        "message": "requested", "error_code": None, "warnings": [],
        "detail": {"source_instance": NODE_A, "source_name": "mojo-api-a",
                   "target_groups": [{"arn": GROUP_ARN, "name": "mojo-api",
                                      "port": 443}]},
        "claim": "test-claim",
    }


@th.django_unit_test("an unproven node is NEVER registered behind the balancer")
def test_registration_is_gated_on_proof(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper
    from mojo.helpers.aws import elbv2 as elbv2_helper
    from mojo.apps.edge.services import platform_deploy

    record = _add_record(capacity)
    row = SimpleNamespace(pk="18180000-0000-4000-8000-000000000001",
                          sha="a" * 40, framework_version="1.13.0")
    source = {"instance_id": NODE_A, "name": "mojo-api-a", "state": "running",
              "instance_type": "m6i.large", "subnet_id": "subnet-0aaa",
              "security_group_ids": ["sg-0aaa"],
              "iam_instance_profile_arn": PROFILE_ARN}
    running = dict(source, instance_id=NEW_NODE)
    with mock.patch.object(capacity, "_sleep"), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(capacity, "_await_runner", return_value=True), \
            mock.patch.object(capacity, "_await_proof", return_value=False) as proof, \
            mock.patch.object(ec2_helper, "instance_facts",
                              side_effect=lambda value, **kw:
                              source if value == NODE_A else running), \
            mock.patch.object(ec2_helper, "find_reusable_image",
                              return_value={"image_id": "ami-0new", "age_days": 2}), \
            mock.patch.object(ec2_helper, "launch_clone", return_value=NEW_NODE), \
            mock.patch.object(elbv2_helper, "register_target") as registered, \
            mock.patch.object(platform_deploy, "last_converged_deployment",
                              return_value=row), \
            mock.patch("mojo.apps.edge.asyncjobs._publish_deploy_node") as published:
        capacity._run_add_node(record)

    assert proof.call_count == 1, "the proof leg never ran"
    assert registered.call_count == 0, \
        "a node that never proved its commit was registered behind the balancer"
    assert record["state"] == "failed" and record["error_code"] == "proof_timeout", \
        f"an unproven node did not fail as proof_timeout: {record}"
    assert published.call_count == 1, \
        f"the converge leg published {published.call_count} deploy jobs, not one"


@th.django_unit_test("a proven node is registered, and the topology is EXTENDED")
def test_proof_registers_and_extends_topology(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2 as ec2_helper
    from mojo.helpers.aws import elbv2 as elbv2_helper
    from mojo.apps.edge.services import platform_deploy

    record = _add_record(capacity)
    row = SimpleNamespace(pk="18180000-0000-4000-8000-000000000001",
                          sha="a" * 40, framework_version="1.13.0")
    source = {"instance_id": NODE_A, "name": "mojo-api-a", "state": "running",
              "instance_type": "m6i.large", "subnet_id": "subnet-0aaa",
              "security_group_ids": ["sg-0aaa"],
              "iam_instance_profile_arn": PROFILE_ARN}
    running = dict(source, instance_id=NEW_NODE)
    with mock.patch.object(capacity, "_sleep"), \
            mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "invalidate"), \
            mock.patch.object(capacity, "_release"), \
            mock.patch.object(capacity, "_await_runner", return_value=True), \
            mock.patch.object(capacity, "_await_proof", return_value=True), \
            mock.patch.object(capacity, "_await_healthy", return_value=True), \
            mock.patch.object(capacity, "_converge_pools") as converged, \
            mock.patch.object(ec2_helper, "instance_facts",
                              side_effect=lambda value, **kw:
                              source if value == NODE_A else running), \
            mock.patch.object(ec2_helper, "find_reusable_image",
                              return_value={"image_id": "ami-0new", "age_days": 2}), \
            mock.patch.object(ec2_helper, "launch_clone", return_value=NEW_NODE), \
            mock.patch.object(elbv2_helper, "register_target") as registered, \
            mock.patch.object(platform_deploy, "last_converged_deployment",
                              return_value=row), \
            mock.patch.object(system_settings, "get_value",
                              return_value={"nodes": ["mojo-api-a"],
                                            "pools": ["default"]}), \
            mock.patch.object(system_settings, "set_value") as wrote, \
            mock.patch("mojo.apps.edge.asyncjobs._publish_deploy_node"):
        capacity._run_add_node(record)

    assert record["state"] == "done", f"a proven node did not finish: {record}"
    assert registered.call_count == 1, \
        f"a proven node was registered {registered.call_count} times, not one"
    assert registered.call_args[0][0] == GROUP_ARN, \
        f"the node was registered into the wrong group: {registered.call_args}"
    assert converged.call_count == 1, \
        "the pool convergence sweep was never triggered after the add"

    assert wrote.call_count == 1, \
        f"the topology was written {wrote.call_count} times, not one"
    _, key, value = wrote.call_args[0]
    assert key == system_settings.EXPECTED_EDGE_TOPOLOGY, \
        f"the wrong setting was written: {key}"
    expected_node = capacity.expected_node_id("mojo-api-a", NEW_NODE)
    assert value["nodes"] == sorted(["mojo-api-a", expected_node]), \
        (f"the topology write did not EXTEND the existing node list: "
         f"{value['nodes']}")
    assert value["pools"] == ["default"], \
        f"the topology write changed the pools: {value['pools']}"


@th.django_unit_test("a topology that cannot be written warns; it never fails the add")
def test_topology_failure_is_a_warning(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services import capacity

    record = _add_record(capacity)
    with mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(system_settings, "get_value",
                              return_value={"nodes": ["mojo-api-a"],
                                            "pools": ["default"]}), \
            mock.patch.object(system_settings, "set_value",
                              side_effect=RuntimeError("denied")):
        capacity._extend_topology(record, "mojo-api-a-newnode")
    codes = [warning["code"] for warning in record["warnings"]]
    assert codes == ["topology_not_updated"], \
        f"a failed topology write did not warn: {record['warnings']}"
    assert record["state"] == "running", \
        "a failed topology write failed the whole add"

    # An UNCONFIGURED topology is left unconfigured. Writing one where none
    # existed would newly constrain a fleet that deliberately had none.
    record = _add_record(capacity)
    with mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(system_settings, "get_value", return_value=None), \
            mock.patch.object(system_settings, "set_value") as wrote:
        capacity._extend_topology(record, "mojo-api-a-newnode")
    assert wrote.call_count == 0, \
        "an unconfigured topology was created by an add"
    assert record["warnings"][0]["code"] == "topology_not_updated", \
        f"the unconfigured case did not warn: {record['warnings']}"


# ── REST ────────────────────────────────────────────────────────────────────

def _view(name):
    import inspect
    from mojo.apps.aws.rest import capacity as views
    return inspect.unwrap(getattr(views, name))


def _request(user, **data):
    from objict import objict
    return SimpleNamespace(user=user, DATA=objict(**data), META={})


def _user(pk=1, superuser=False, perms=()):
    granted = set(perms)
    user = mock.Mock(is_superuser=superuser, pk=pk, username=f"user-{pk}")
    user.has_permission.side_effect = lambda wanted: bool(granted & set(wanted))
    return user


@th.django_unit_test("manage_aws and manage_platform cannot change capacity — only a superuser")
def test_apply_requires_a_superuser(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import capacity

    view = _view("on_capacity_apply")
    body = {"action": "drain_node", "resource": NODE_B, "confirm_resource": NODE_B}
    with mock.patch.object(capacity, "apply") as applied:
        for perms in (["manage_aws"], ["manage_aws", "manage_platform"],
                      ["manage_aws", "admin"]):
            with th.assert_raises(me.PermissionDeniedException):
                view(_request(_user(perms=perms), **body))
        assert applied.call_count == 0, \
            "a non-superuser reached the capacity apply service"

        applied.return_value = {"id": "op-1"}
        with mock.patch("mojo.apps.account.services.admin_platform.audit_after_commit"):
            view(_request(_user(superuser=True), **body))
        assert applied.call_count == 1, "a superuser was refused"


@th.django_unit_test("a mismatched typed confirmation is refused before anything reaches AWS")
def test_apply_confirmation_is_checked_first(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import capacity

    view = _view("on_capacity_apply")
    actor = _user(superuser=True)
    with mock.patch.object(capacity, "apply") as applied:
        for confirm in (NODE_A, "", "i-0a1b2c3d4e5f6002"):
            with th.assert_raises(me.ValueException):
                view(_request(actor, action="drain_node", resource=NODE_B,
                              confirm_resource=confirm))
        # add_node has no resource of its own, so its echo is the action word.
        with th.assert_raises(me.ValueException):
            view(_request(actor, action="add_node", confirm_resource="yes"))
        assert applied.call_count == 0, \
            "a mismatched confirmation still reached the apply service"

        applied.return_value = {"id": "op-1"}
        with mock.patch("mojo.apps.account.services.admin_platform.audit_after_commit"):
            view(_request(actor, action="add_node", confirm_resource="add_node"))
        assert applied.call_count == 1, \
            "the add_node echo the panel shows was not accepted"


@th.django_unit_test("a replica change states its count and its window, or it is a 400")
def test_apply_replica_fields_have_no_defaults(opts):
    from mojo import errors as me
    from mojo.apps.aws.services import capacity

    view = _view("on_capacity_apply")
    actor = _user(superuser=True)
    base = {"action": "set_cache_replicas", "resource": CACHE_GROUP,
            "confirm_resource": CACHE_GROUP}
    with mock.patch.object(capacity, "apply") as applied:
        with th.assert_raises(me.ValueException):
            view(_request(actor, **base, apply_immediately=True))
        with th.assert_raises(me.ValueException):
            view(_request(actor, **base, count=1))
        # A string is not a number, and "false" would be truthy.
        with th.assert_raises(me.ValueException):
            view(_request(actor, **base, count="1", apply_immediately=True))
        with th.assert_raises(me.ValueException):
            view(_request(actor, **base, count=1, apply_immediately="false"))
        assert applied.call_count == 0, \
            "an unstated replica count or window reached the apply service"


@th.django_unit_test("one capacity apply writes exactly one audit event, naming the action")
def test_apply_audits_once(opts):
    from mojo.apps.aws.services import capacity

    view = _view("on_capacity_apply")
    with mock.patch.object(capacity, "apply", return_value={"id": "op-9"}), \
            mock.patch("mojo.apps.account.services.admin_platform.audit_after_commit") \
            as audit:
        view(_request(_user(superuser=True), action="terminate_node",
                      resource=NODE_B, confirm_resource=NODE_B))
    assert audit.call_count == 1, \
        f"one apply wrote {audit.call_count} audit events"
    _, action, target = audit.call_args[0]
    assert action == "aws_capacity_terminate_node", \
        f"the audit action is {action!r}"
    assert NODE_B in target and "op-9" in target, \
        f"the audit target does not identify what changed: {target!r}"


@th.django_unit_test("the capacity write declares key denial and a fixed fresh-auth window")
def test_capacity_write_decorators(opts):
    from mojo.apps.aws.rest import capacity as views

    func = views.on_capacity_apply
    assert getattr(func, "_mojo_denies_key_backed_session", False), \
        "the capacity apply endpoint accepts key-backed sessions"
    assert getattr(func, "_mojo_requires_fresh_auth", False), \
        "the capacity apply endpoint does not require fresh auth"
    assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, \
        "the capacity apply endpoint does not pin its fresh-auth window"


@th.django_unit_test("capacity endpoints refuse an unauthenticated caller")
def test_capacity_endpoints_require_auth(opts):
    opts.client.logout()
    for path in ("/api/aws/capacity", "/api/aws/capacity/status?operation=x"):
        response = opts.client.get(path)
        assert response.status_code in (401, 403), \
            f"{path} answered {response.status_code} to an anonymous caller"
    response = opts.client.post("/api/aws/capacity/apply", {"action": "add_node"})
    assert response.status_code in (401, 403), \
        f"apply answered {response.status_code} to an anonymous caller"


@th.django_unit_test("an unknown operation id is a 404, not an empty success")
def test_unknown_operation_is_not_found(opts):
    from mojo.apps.aws.services import capacity

    with th.assert_raises(capacity.CapacityError) as caught:
        capacity.operation_status("op-that-never-existed")
    assert caught.exception.status == 404 \
        and caught.exception.error_code == "operation_not_found", \
        f"an unknown operation answered {caught.exception.status}"


@th.django_unit_test("the status route resolves — its 404 is an answer, not a missing route")
def test_status_route_is_registered(opts):
    # The escalation sweep's "not 404" registration check cannot cover this
    # path: an unknown operation id is a legitimate 404 here, so a missing
    # route and a correct answer look identical from the status code alone.
    # The error_code is what tells them apart.
    from mojo.apps.account.models import User

    User.objects.filter(username="capacity-root").delete()
    root = User.objects.create_user(
        email="capacity-root@test.com", username="capacity-root",
        password="example")
    root.is_active = True
    root.is_superuser = True
    root.save()
    opts.client.login("capacity-root", "example")
    try:
        response = opts.client.get(
            "/api/aws/capacity/status?operation=op-that-never-existed")
        assert response.status_code == 404, \
            (f"the status route answered {response.status_code}: "
             f"{opts.client.last_response.body}")
        body = response.json or {}
        assert body.get("error_code") == "operation_not_found", \
            f"the 404 came from routing, not from the handler: {body}"
    finally:
        opts.client.logout()


@th.django_unit_test("the capacity panel capability is superuser AND manage_aws")
def test_capacity_capability(opts):
    from mojo.apps.account.services.admin_features import platform

    both = platform.describe(None, {"setup": True, "manage_aws": True})
    assert both["capabilities"]["capacity"] is True, \
        "a superuser holding manage_aws was not offered the capacity panel"
    for capabilities in ({"setup": True, "manage_aws": False},
                         {"setup": False, "manage_aws": True},
                         {"manage_platform": True, "manage_aws": True}):
        described = platform.describe(None, capabilities)
        assert described["capabilities"]["capacity"] is False, \
            f"capacity was offered to {capabilities}"


@th.django_unit_test("the report self-reports reader routing, honestly per node")
def test_report_reader_routing(opts):
    from mojo.apps.aws.services import capacity

    # The live shape: the test project configures no reader of either kind, so
    # the envelope must say so rather than omit the block or guess.
    envelope = capacity.report(
        elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
        rds_client=_rds_client(), cache_client=_cache_client([_cache_group()]))
    routing = envelope["reader_routing"]
    assert routing["database"]["active"] is False, \
        "no reader alias is configured, yet database routing reported active"
    assert routing["redis"]["active"] is False, \
        "no REDIS_READER_* is configured, yet redis reported a reader"

    # The configured shapes, via the injectable form — no process-global
    # settings mutation in the default tier.
    active = capacity._reader_routing(
        {"databases": [{"reader_endpoint": "db.cluster-ro.example.com"}]},
        django_databases={"default": {"HOST": "db.example.com"},
                          "reader": {"HOST": "db.cluster-ro.example.com"}},
        django_routers=["mojo.db.router.ReaderRouter"],
        skip_reason="", redis_reader_on=True)
    assert active["database"]["active"] is True, \
        "an installed alias + router did not report active"
    assert active["database"]["matches_reader_endpoint"] is True, \
        "a host equal to the cluster reader endpoint did not match"
    assert active["redis"]["active"] is True, \
        "a resolved redis reader did not report active"

    mismatch = capacity._reader_routing(
        {"databases": [{"reader_endpoint": "db.cluster-ro.example.com"}]},
        django_databases={"default": {"HOST": "db.example.com"},
                          "reader": {"HOST": "db.cluster-ro.TYPO.example.com"}},
        django_routers=["mojo.db.router.ReaderRouter"],
        skip_reason="", redis_reader_on=False)
    assert mismatch["database"]["matches_reader_endpoint"] is False, \
        "a wrong reader host was not flagged against the cluster endpoint"

    skipped = capacity._reader_routing(
        {"databases": []},
        django_databases={"default": {"HOST": "db.example.com"}},
        django_routers=[],
        skip_reason="MIDDLEWARE is required for request-scoped reader routing",
        redis_reader_on=False)
    assert skipped["database"]["active"] is False, \
        "a skipped injection still reported active routing"
    assert "MIDDLEWARE" in skipped["database"]["skip_reason"], \
        "the skip reason did not surface in the report"
    # Nothing to compare against: unknown, never a false alarm.
    assert skipped["database"]["matches_reader_endpoint"] is None, \
        "an inactive config produced an endpoint verdict"
