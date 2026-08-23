"""Admin capacity actions: the AWS request shapes, the report, and the routes.

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

The guard, runner, and REST-gate tests that exercise capacity's internals by
patching shared module attributes (``_dispatch``, ``_local_hostname``,
``capacity.apply``, the aws helper modules, …) live in
``tests/test_aws_extended_serial/capacity.py`` — the opt-in serial tier —
because those patches are not parallel-safe (maestro #2558).
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


REGION = "us-east-1"
BALANCER_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                "loadbalancer/net/mojo-api-nlb/0123456789abcdef")
GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
             "targetgroup/mojo-api/abcdef0123456789")
SITES_BALANCER_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                      "loadbalancer/net/mojo-sites-nlb/fedcba9876543210")
SITES_GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                   "targetgroup/mojo-sites/9876543210fedcba")
NODE_A = "i-0a1b2c3d4e5f60011"
NODE_B = "i-0a1b2c3d4e5f60022"
NEW_NODE = "i-0a1b2c3d4e5f60044"
SUBNET_A = "subnet-0aaa"
SUBNET_B = "subnet-0bbb"
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


def _ec2_client(instances=()):
    client = mock.Mock()
    client.describe_instances.return_value = {
        "Reservations": [{"Instances": list(instances)}]}
    return client


def _instance(instance_id, name, subnet=SUBNET_A, vpc="vpc-0aaa",
              zone="us-east-1a", state="running"):
    return {
        "InstanceId": instance_id, "InstanceType": "m6i.large",
        "ImageId": "ami-0source", "SubnetId": subnet, "VpcId": vpc,
        "State": {"Name": state}, "Placement": {"AvailabilityZone": zone},
        "PrivateIpAddress": "10.0.1.11",
        "PrivateDnsName": "ip-10-0-1-11.ec2.internal",
        "SecurityGroups": [{"GroupId": "sg-0aaa"}],
        "IamInstanceProfile": {"Arn": PROFILE_ARN},
        "Tags": [{"Key": "Name", "Value": name}],
    }


def _subnet(subnet_id, vpc="vpc-0aaa", zone="us-east-1a", free=250,
            public=True):
    return {
        "SubnetId": subnet_id, "VpcId": vpc, "AvailabilityZone": zone,
        "State": "available", "CidrBlock": "10.0.1.0/24",
        "AvailableIpAddressCount": free, "MapPublicIpOnLaunch": public,
        "Tags": [{"Key": "Name", "Value": subnet_id}],
    }


def _placement_client(instances=(), subnets=()):
    """One EC2 mock routing describe_instances AND describe_subnets.

    Routed by the subnet-id filter on purpose: the placement checks read the
    REQUESTED subnet and the SOURCE's subnet, and a mock answering both with
    the same first row would hide a vpc or addressing mismatch entirely.
    """
    rows = {row["SubnetId"]: row for row in subnets}
    client = mock.Mock()
    client.describe_instances.return_value = {
        "Reservations": [{"Instances": list(instances)}]}
    client.describe_subnets.side_effect = lambda Filters: {
        "Subnets": [rows[key] for key in Filters[0]["Values"] if key in rows]}
    return client


def _placement_serving(api_zones=("us-east-1a", "us-east-1b"),
                       sites_zones=("us-east-1a", "us-east-1b")):
    """The Maestro shape: two balancers, one healthy target each.

    Which balancer's fleet an add grows is decided ENTIRELY by which node it
    clones, so a one-group map could not show the mechanism working.
    """
    return {
        "balancers": [
            {"arn": BALANCER_ARN, "name": "mojo-api-nlb",
             "zones": list(api_zones)},
            {"arn": SITES_BALANCER_ARN, "name": "mojo-sites-nlb",
             "zones": list(sites_zones)}],
        "groups": [
            {"arn": GROUP_ARN, "name": "mojo-api", "balancers": [BALANCER_ARN],
             "targets": [{"id": NODE_A, "port": 443, "state": "healthy"}]},
            {"arn": SITES_GROUP_ARN, "name": "mojo-sites",
             "balancers": [SITES_BALANCER_ARN],
             "targets": [{"id": NODE_B, "port": 8443, "state": "healthy"}]}],
    }


def _spec():
    """A resolved environment. The test project declares none, so every named
    source would otherwise be refused by the unconfigured-install guard."""
    return SimpleNamespace(project="mojo-test", env="prod")


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


@th.django_unit_test("one subnet is read by FILTER, and a missing one is None")
def test_subnet_facts_reads_one_subnet(opts):
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    stubber.add_response(
        "describe_subnets", {"Subnets": [_subnet(
            SUBNET_B, zone="us-east-1b", free=12, public=False)]},
        {"Filters": [{"Name": "subnet-id", "Values": [SUBNET_B]}]})
    with stubber:
        facts = ec2.subnet_facts(SUBNET_B, client=client)
    stubber.assert_no_pending_responses()
    assert facts["vpc_id"] == "vpc-0aaa" \
        and facts["availability_zone"] == "us-east-1b", \
        f"the subnet's VPC and zone were not projected: {facts}"
    assert facts["available_ip_count"] == 12, \
        f"the free-address count was not projected: {facts}"
    assert facts["map_public_ip_on_launch"] is False, \
        f"public addressing was not projected honestly: {facts}"

    # A subnet that does not exist must be a None the caller refuses on, not
    # an exception that reads as a provider failure. This is why the describe
    # is FILTERED and never SubnetIds=.
    client, stubber = _stub("ec2")
    stubber.add_response("describe_subnets", {"Subnets": []}, {
        "Filters": [{"Name": "subnet-id", "Values": ["subnet-0nope"]}]})
    with stubber:
        missing = ec2.subnet_facts("subnet-0nope", client=client)
    assert missing is None, f"a missing subnet did not read as None: {missing!r}"


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


@th.django_unit_test("a clone lands in the subnet the caller named, not the source's")
def test_launch_clone_places_in_a_named_subnet(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws import ec2

    client, stubber = _stub("ec2")
    user_data = capacity.node_user_data("mojo-api-a")
    source = {"instance_type": "m6i.large", "subnet_id": SUBNET_A,
              "security_group_ids": ["sg-0aaa"],
              "iam_instance_profile_arn": PROFILE_ARN}
    # expected_params is an EXACT match of the whole request, so this pins
    # SubnetId to the named subnet while the source names a different one —
    # the fallback silently winning is exactly the bug it would catch.
    stubber.add_response(
        "run_instances", {"Instances": [{"InstanceId": NEW_NODE}]}, {
            "ImageId": "ami-0new", "InstanceType": "m6i.large",
            "MinCount": 1, "MaxCount": 1, "SubnetId": SUBNET_B,
            "SecurityGroupIds": ["sg-0aaa"], "UserData": user_data,
            "MetadataOptions": dict(ec2.IMDS_V2_ONLY),
            "IamInstanceProfile": {"Arn": PROFILE_ARN},
            "TagSpecifications": [{"ResourceType": "instance", "Tags": [
                {"Key": "Name", "Value": "mojo-api-a-clone"},
                {"Key": "mojo:created-by", "Value": "admin-capacity"}]}]})
    with stubber:
        created = ec2.launch_clone(source, "ami-0new", SUBNET_B,
                                   "mojo-api-a-clone", user_data, client=client)
    assert created == NEW_NODE, f"the new instance id was not returned: {created!r}"
    stubber.assert_no_pending_responses()


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
    tags = {"managed-by": "django-mojo", "mojo:env": "prod",
            "mojo:project": "mojo-test", "mojo:role": "database"}
    stubber.add_response("create_db_instance", {"DBInstance": {}}, {
        "DBInstanceIdentifier": f"{CLUSTER}-reader-abcd1234",
        "DBClusterIdentifier": CLUSTER,
        "DBInstanceClass": "db.r6g.large",
        "Engine": "aurora-postgresql",
        "Tags": [{"Key": key, "Value": tags[key]} for key in sorted(tags)]})
    with stubber:
        rds.create_cluster_reader(
            CLUSTER, f"{CLUSTER}-reader-abcd1234", "db.r6g.large",
            "aurora-postgresql", tags=tags, client=client)
    stubber.assert_no_pending_responses()


@th.django_unit_test("a standalone reader is create_db_instance_read_replica")
def test_standalone_reader_dispatch(opts):
    from mojo.helpers.aws import rds

    client, stubber = _stub("rds")
    tags = {"managed-by": "django-mojo", "mojo:env": "prod",
            "mojo:project": "mojo-test", "mojo:role": "database"}
    stubber.add_response("create_db_instance_read_replica", {"DBInstance": {}}, {
        "DBInstanceIdentifier": f"{STANDALONE}-reader-abcd1234",
        "SourceDBInstanceIdentifier": STANDALONE,
        "DBInstanceClass": "db.m6g.large",
        "Tags": [{"Key": key, "Value": tags[key]} for key in sorted(tags)]})
    with stubber:
        rds.create_read_replica(
            STANDALONE, f"{STANDALONE}-reader-abcd1234", "db.m6g.large",
            tags=tags, client=client)
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


# ── service: explicit placement ─────────────────────────────────────────────
#
# Every one of these calls _prepare_add_node DIRECTLY with injected clients and
# an injected fleet spec — no patching, so they stay in the default tier. The
# whole point of the feature is that a bad placement is refused in the request,
# before a claim, an AMI, or an instance exists.

@th.django_unit_test("the source picks which fleet grows, and placement is proven first")
def test_add_node_placement_is_validated_before_anything_is_created(opts):
    from mojo.apps.aws.services import capacity

    serving = _placement_serving()
    instances = [_instance(NODE_A, "mojo-api-a"),
                 _instance(NODE_B, "mojo-sites-a")]
    subnets = [_subnet(SUBNET_A), _subnet(SUBNET_B, zone="us-east-1b"),
               _subnet("subnet-0zzz", vpc="vpc-0zzz")]

    # (a) naming the sites node grows the SITES fleet: the groups the runner
    # will register into are derived from the source, and only from it.
    client = _placement_client(instances, subnets)
    source, groups, placement = capacity._prepare_add_node(
        serving, ec2_client=client, source_instance=NODE_B,
        subnet_id=SUBNET_B, fleet_spec=_spec())
    assert source["instance_id"] == NODE_B, \
        f"the named source was not the node chosen to clone: {source}"
    assert [group["arn"] for group in groups] == [SITES_GROUP_ARN], \
        f"naming a source did not choose that source's fleet: {groups}"
    assert groups[0]["port"] == 8443, \
        f"the clone would register on the wrong port: {groups}"
    assert placement == {"subnet_id": SUBNET_B,
                         "availability_zone": "us-east-1b",
                         "selected": "requested"}, \
        f"the named subnet was not the placement decided: {placement}"

    # (b) a source outside the fleet's healthy targets can never be widened in.
    client = _placement_client(instances, subnets)
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._prepare_add_node(serving, ec2_client=client,
                                   source_instance=NEW_NODE,
                                   fleet_spec=_spec())
    assert caught.exception.error_code == "source_not_serving" \
        and caught.exception.status == 409, \
        f"a foreign source was not refused: {caught.exception.error_code}"

    # (c) a clone carries its source's VPC-scoped security groups, so a subnet
    # in another VPC is a launch AWS would hard-reject.
    client = _placement_client(instances, subnets)
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._prepare_add_node(serving, ec2_client=client,
                                   source_instance=NODE_A,
                                   subnet_id="subnet-0zzz", fleet_spec=_spec())
    assert caught.exception.error_code == "subnet_not_usable" \
        and caught.exception.data.get("reason") == "vpc_mismatch", \
        f"a cross-VPC subnet was not refused: {caught.exception.data}"

    # (d) omitting both is byte-identical to the old behavior AND costs no
    # provider call — DescribeSubnets is only needed when a subnet is named.
    client = _placement_client(instances, subnets)
    source, groups, placement = capacity._prepare_add_node(
        serving, ec2_client=client, fleet_spec=_spec())
    assert placement == {"subnet_id": SUBNET_A,
                         "availability_zone": "us-east-1a",
                         "selected": "source"}, \
        f"the automatic path stopped landing in the source's subnet: {placement}"
    assert client.describe_subnets.call_count == 0, \
        "the automatic path called DescribeSubnets, which it must never need"


@th.django_unit_test("a subnet in a zone the balancer does not serve is refused up front")
def test_add_node_refuses_a_subnet_the_balancer_does_not_serve(opts):
    from mojo.apps.aws.services import capacity

    instances = [_instance(NODE_A, "mojo-api-a")]
    subnets = [_subnet(SUBNET_A), _subnet(SUBNET_B, zone="us-east-1b")]

    # A target in a zone the balancer has not enabled either throws
    # InvalidTarget at RegisterTargets — held claim, "do not replay" — or
    # never goes healthy. Both burn the AMI, the boot, the converge and the
    # proof first. Refusing here costs one describe.
    one_zone = _placement_serving(api_zones=("us-east-1a",))
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._prepare_add_node(
            one_zone, ec2_client=_placement_client(instances, subnets),
            source_instance=NODE_A, subnet_id=SUBNET_B, fleet_spec=_spec())
    assert caught.exception.error_code == "subnet_az_not_served" \
        and caught.exception.status == 409, \
        f"an unserved zone was not refused: {caught.exception.error_code}"
    assert caught.exception.data.get("served_zones") == ["us-east-1a"], \
        f"the refusal does not say which zones ARE served: {caught.exception.data}"

    # The same balancer, the zone it does serve: accepted.
    _source, _groups, placement = capacity._prepare_add_node(
        one_zone, ec2_client=_placement_client(instances, subnets),
        source_instance=NODE_A, subnet_id=SUBNET_A, fleet_spec=_spec())
    assert placement["selected"] == "requested", \
        f"a subnet in a served zone was not accepted: {placement}"

    # Absent zone data is not a refusal — the check skips rather than guesses.
    silent = _placement_serving(api_zones=())
    _source, _groups, placement = capacity._prepare_add_node(
        silent, ec2_client=_placement_client(instances, subnets),
        source_instance=NODE_A, subnet_id=SUBNET_B, fleet_spec=_spec())
    assert placement["availability_zone"] == "us-east-1b", \
        f"a balancer reporting no zones turned into a refusal: {placement}"


@th.django_unit_test("a private subnet is refused when the source's subnet is public")
def test_add_node_refuses_a_subnet_without_public_addressing(opts):
    from mojo.apps.aws.services import capacity

    serving = _placement_serving()
    instances = [_instance(NODE_A, "mojo-api-a")]

    # launch_clone sets no AssociatePublicIpAddress and uses no
    # NetworkInterfaces block, so addressing is whatever the subnet does. A
    # same-VPC private subnet otherwise passes every check and the node dies
    # at RUNNER_TIMEOUT having never reached anything.
    mixed = [_subnet(SUBNET_A, public=True),
             _subnet(SUBNET_B, zone="us-east-1b", public=False)]
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._prepare_add_node(
            serving, ec2_client=_placement_client(instances, mixed),
            source_instance=NODE_A, subnet_id=SUBNET_B, fleet_spec=_spec())
    assert caught.exception.error_code == "subnet_not_usable" \
        and caught.exception.data.get("reason") == "no_public_addressing", \
        f"a private subnet under a public source was not refused: " \
        f"{caught.exception.data}"

    # Compared against the SOURCE, never asserted absolutely: a fleet that
    # lives in private subnets stays able to add nodes.
    private = [_subnet(SUBNET_A, public=False),
               _subnet(SUBNET_B, zone="us-east-1b", public=False)]
    _source, _groups, placement = capacity._prepare_add_node(
        serving, ec2_client=_placement_client(instances, private),
        source_instance=NODE_A, subnet_id=SUBNET_B, fleet_spec=_spec())
    assert placement["selected"] == "requested", \
        f"a private→private placement was refused: {placement}"


@th.django_unit_test("a named source is refused on an install with no environment")
def test_add_node_refuses_a_named_source_on_an_unconfigured_install(opts):
    from mojo.apps.aws.services import capacity

    serving = _placement_serving()
    instances = [_instance(NODE_A, "mojo-api-a"), _instance(NODE_B, "mojo-sites-a")]
    subnets = [_subnet(SUBNET_A)]

    # No environment declaration means _fleet_serving skipped its narrowing
    # and this map is every balancer in the ACCOUNT. Naming a source would
    # make a neighbouring installation's node clonable — and launch_clone
    # copies the source's instance profile verbatim.
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._prepare_add_node(
            serving, ec2_client=_placement_client(instances, subnets),
            source_instance=NODE_B, fleet_spec=None)
    assert caught.exception.error_code == "source_not_serving" \
        and caught.exception.status == 409, \
        f"a named source on an unconfigured install was allowed: " \
        f"{caught.exception.error_code}"

    # The automatic path is unchanged there — it picks deterministically, so
    # exactly one instance was ever reachable.
    source, _groups, placement = capacity._prepare_add_node(
        serving, ec2_client=_placement_client(instances, subnets),
        fleet_spec=None)
    assert source["instance_id"] in (NODE_A, NODE_B), \
        f"the automatic path stopped working without an environment: {source}"
    assert placement["selected"] == "source", \
        f"the automatic path did not fall back to the source's subnet: {placement}"


# ── service: single flight ──────────────────────────────────────────────────

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


# ── REST ────────────────────────────────────────────────────────────────────


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


@th.django_unit_test("the report carries a per-instance class for every database row")
def test_report_carries_instance_classes(opts):
    from mojo.apps.aws.services import capacity

    # Filter-aware on purpose: instance_role describes ONE instance by
    # identifier, and a mock answering every filter with the same first row
    # would fold every replica onto the wrong source.
    instances = {
        f"{CLUSTER}-1": {
            "DBInstanceIdentifier": f"{CLUSTER}-1", "DBInstanceStatus": "available",
            "Engine": "aurora-postgresql", "EngineVersion": "16.4",
            "DBInstanceClass": "db.r6g.xlarge", "DBClusterIdentifier": CLUSTER},
        f"{CLUSTER}-2": {
            "DBInstanceIdentifier": f"{CLUSTER}-2", "DBInstanceStatus": "available",
            "Engine": "aurora-postgresql", "EngineVersion": "16.4",
            "DBInstanceClass": "db.t4g.medium", "DBClusterIdentifier": CLUSTER},
        STANDALONE: {
            "DBInstanceIdentifier": STANDALONE, "DBInstanceStatus": "available",
            "Engine": "postgres", "EngineVersion": "16.4",
            "DBInstanceClass": "db.m6g.large"},
        f"{STANDALONE}-replica": {
            "DBInstanceIdentifier": f"{STANDALONE}-replica",
            "DBInstanceStatus": "available", "Engine": "postgres",
            "EngineVersion": "16.4", "DBInstanceClass": "db.t4g.medium",
            "ReadReplicaSourceDBInstanceIdentifier": STANDALONE},
    }
    rds_client = mock.Mock()
    rds_client.describe_db_instances.side_effect = lambda **kwargs: {
        "DBInstances": ([instances[kwargs["DBInstanceIdentifier"]]]
                        if kwargs.get("DBInstanceIdentifier") in instances
                        else list(instances.values())
                        if not kwargs.get("DBInstanceIdentifier") else [])}
    rds_client.describe_db_clusters.return_value = {"DBClusters": [{
        "DBClusterIdentifier": CLUSTER, "Engine": "aurora-postgresql",
        "Status": "available", "DBClusterMembers": [
            {"DBInstanceIdentifier": f"{CLUSTER}-1", "IsClusterWriter": True},
            {"DBInstanceIdentifier": f"{CLUSTER}-2", "IsClusterWriter": False}]}]}

    envelope = capacity.report(
        elbv2_client=_elbv2_client(), ec2_client=_ec2_client(),
        rds_client=rds_client, cache_client=_cache_client())
    rows = {row["identifier"]: row for row in envelope["databases"]}

    aurora = rows[CLUSTER]
    assert aurora["writer_instance_class"] == "db.r6g.xlarge", \
        f"the aurora writer's class is missing from its row: {aurora}"
    assert aurora["reader_instance_classes"] == {
        f"{CLUSTER}-2": "db.t4g.medium"}, \
        f"the aurora readers' classes are missing from their row: {aurora}"

    standalone = rows[STANDALONE]
    assert standalone["instance_class"] == "db.m6g.large", \
        f"the standalone primary's class is missing: {standalone}"
    assert standalone["readers"] == [f"{STANDALONE}-replica"], \
        f"the folded replica is missing from its source: {standalone}"
    assert standalone["reader_instance_classes"] == {
        f"{STANDALONE}-replica": "db.t4g.medium"}, \
        f"the folded replica's class is missing from its source: {standalone}"
