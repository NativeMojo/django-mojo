from objict import objict
from testit import helpers as th

from .brownfield_fixture import managed_topology, topology


class _Clients:
    def __init__(self, **clients):
        self.clients = clients

    def get(self, name):
        return self.clients[name]


@th.django_unit_test()
def test_unowned_exact_name_balancer_collision_is_not_exposed(opts):
    from mojo.deploy.provision import brownfield_discover, report

    class _ELB:
        def describe_tags(self, ResourceArns):
            return {"TagDescriptions": [{"ResourceArn": ResourceArns[0],
                                         "Tags": [{"Key": "Name",
                                                   "Value": "collision"}]}]}

    findings = []
    accepted = brownfield_discover._owned_elbv2(
        _ELB(), topology(), "arn:aws:elasticloadbalancing:us-west-2:"
        "123456789012:loadbalancer/net/maestro-shadow-nlb/abc", findings,
        "load balancer")
    th.assert_eq(accepted, False,
                 "an exact-name resource without fleet tags is not adoptable")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the collision must block apply, not look missing: {findings}")


@th.django_unit_test()
def test_unowned_node_name_collision_never_enters_node_convergence(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    row = {
        "InstanceId": "i-aaaaaaaaaaaaaaaaa", "VpcId": manifest["network"]["vpc_id"],
        "SubnetId": declaration["subnet_id"],
        "Placement": {"AvailabilityZone": declaration["availability_zone"]},
        "State": {"Name": "running"},
        "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
        "SecurityGroups": [{"GroupId": manifest["network"]["node_security_group_id"]}],
        "Tags": [{"Key": "Name", "Value": declaration["name"]}],
    }

    class _EC2:
        def describe_instances(self, **kwargs):
            return {"Reservations": [{"Instances": [row]}]}

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        "api": {"profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)
    th.assert_eq(list(observed.instances), [],
                 "the unowned collision must not be handed to ensure_nodes")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the collision must block the dependency step: {findings}")


@th.django_unit_test()
def test_node_observation_validates_vpc_subnet_profile_role_and_security_group(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    tags = {
        "Name": declaration["name"], "managed-by": "django-mojo",
        "mojo:project": spec.project, "mojo:env": spec.env,
        "mojo:fleet": spec.fleet, "mojo:role": "node",
        "mojo:application-role": declaration["role"],
    }
    row = {
        "InstanceId": "i-aaaaaaaaaaaaaaaaa", "VpcId": "vpc-deadbeef",
        "SubnetId": declaration["subnet_id"],
        "Placement": {"AvailabilityZone": declaration["availability_zone"]},
        "State": {"Name": "running"},
        "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
        "SecurityGroups": [{"GroupId": manifest["network"]["node_security_group_id"]}],
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }

    class _EC2:
        def describe_instances(self, **kwargs):
            return {"Reservations": [{"Instances": [row]}]}

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        "api": {"profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)
    th.assert_eq(list(observed.instances), [],
                 "a wrong-VPC node must never become an owned node")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"a wrong VPC must block apply: {findings}")


@th.django_unit_test()
def test_telemetry_exact_name_collisions_are_recorded_not_adopted(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    prefix = f"/mojo/{spec.project}-{spec.fleet}"

    class _Logs:
        def describe_log_groups(self, **kwargs):
            return {"logGroups": [{"logGroupName": f"{prefix}/app"}]}

        def list_tags_log_group(self, **kwargs):
            return {"tags": {"managed-by": "someone-else"}}

    class _CloudWatch:
        def describe_alarms(self, **kwargs):
            return {"MetricAlarms": [{
                "AlarmName": "maestro-shadow-api-unhealthy",
                "AlarmArn": "arn:aws:cloudwatch:us-west-2:123456789012:alarm:x"}]}

        def list_tags_for_resource(self, **kwargs):
            return {"Tags": [{"Key": "managed-by", "Value": "someone-else"}]}

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._telemetry(
        _Clients(logs=_Logs(), cloudwatch=_CloudWatch()), spec, findings,
        observed, inventory)
    th.assert_in(f"{prefix}/app", observed.log_group_collisions,
                 "the colliding log group must be withheld from convergence")
    th.assert_in("maestro-shadow-api-unhealthy", observed.alarm_collisions,
                 "the colliding alarm must be withheld from convergence")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"telemetry collisions must block apply: {findings}")


@th.django_unit_test()
def test_managed_iam_role_and_profile_collisions_are_not_exposed(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = managed_topology()

    class _IAM:
        def get_instance_profile(self, **kwargs):
            return {"InstanceProfile": {
                "Arn": "arn:aws:iam::123456789012:instance-profile/maestro-shadow-api",
                "Tags": [{"Key": "managed-by", "Value": "someone-else"}],
                "Roles": []}}

        def get_role(self, **kwargs):
            return {"Role": {
                "Arn": "arn:aws:iam::123456789012:role/maestro-shadow-api",
                "Tags": [{"Key": "managed-by", "Value": "someone-else"}]}}

        def __getattr__(self, name):
            raise AssertionError(
                f"unowned role must not be inspected for dependent {name}")

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._profiles(
        _Clients(iam=_IAM()), spec.brownfield_manifest, findings, observed,
        inventory)
    row = observed.brownfield_profiles["api"]
    th.assert_true(row.role_collision and row.profile_collision,
                   f"both unowned exact-name resources must be collisions: {row}")
    th.assert_eq(row.role_arn, None,
                 "an unowned role ARN must never reach identity convergence")
    th.assert_eq(row.profile_arn, None,
                 "an unowned profile ARN must never reach node launch")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"IAM collisions must block apply: {findings}")


@th.django_unit_test()
def test_owned_iam_role_with_admin_policy_is_rejected(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = managed_topology()
    tags = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "identity"},
        {"Key": "mojo:application-role", "Value": "api"},
    ]
    role_arn = "arn:aws:iam::123456789012:role/maestro-shadow-api"

    class _IAM:
        def get_instance_profile(self, **kwargs):
            return {"InstanceProfile": {
                "Arn": "arn:aws:iam::123456789012:instance-profile/maestro-shadow-api",
                "Tags": tags, "Roles": [{"Arn": role_arn}]}}

        def get_role(self, **kwargs):
            return {"Role": {"Arn": role_arn, "Tags": tags,
                             "AssumeRolePolicyDocument":
                                 brownfield_discover.EC2_TRUST}}

        def list_role_policies(self, **kwargs):
            return {"PolicyNames": ["maestro-shadow-api-runtime"]}

        def list_attached_role_policies(self, **kwargs):
            return {"AttachedPolicies": [{
                "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]}

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._profiles(
        _Clients(iam=_IAM()), spec.brownfield_manifest, findings, observed,
        inventory)
    row = observed.brownfield_profiles["api"]
    th.assert_true(row.role_collision and row.profile_collision,
                   f"a broadened owned role must be withheld: {row}")
    th.assert_eq(row.role_arn, None,
                 "AdministratorAccess must never reach node launch")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"the broadened role must block apply: {findings}")


@th.django_unit_test()
def test_owned_eip_ambiguity_and_wrong_border_group_fail_closed(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    subnet = spec.nlb_subnet_ids[0]
    name = f"{spec.nlb_name}:{subnet}"
    tags = {
        "Name": name, "managed-by": "django-mojo",
        "mojo:project": spec.project, "mojo:env": spec.env,
        "mojo:fleet": spec.fleet, "mojo:role": "balancer",
    }
    rows = [{
        "AllocationId": f"eipalloc-{index}", "Domain": "vpc",
        "NetworkBorderGroup": "us-east-1",
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    } for index in (1, 2)]
    findings = []
    accepted = brownfield_discover._owned_addresses(
        spec, spec.brownfield_manifest, rows, None, findings)
    th.assert_eq(accepted, [],
                 "ambiguous subnet-bound addresses must not enter NLB creation")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"ambiguity/border drift must block preparation: {findings}")


@th.django_unit_test()
def test_security_group_and_kms_reachability_helpers_are_exact(opts):
    from mojo.deploy.provision import brownfield_discover

    group = {"IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
        "UserIdGroupPairs": [{"GroupId": "sg-node"}]}]}
    th.assert_true(brownfield_discover._allows_group_port(
        group, 5432, "sg-node"), "the exact node-to-database path must pass")
    th.assert_eq(brownfield_discover._allows_group_port(
        group, 6379, "sg-node"), False,
        "a different port must not be inferred reachable")
    policy = {"Statement": [{
        "Effect": "Allow", "Principal": {"AWS":
            "arn:aws:iam::123456789012:root"}, "Action": "kms:*"}]}
    th.assert_true(brownfield_discover._policy_allows(
        policy, "arn:aws:iam::123456789012:root"),
        "the exact account IAM delegation must be recognized")
    th.assert_eq(brownfield_discover._policy_allows(
        policy, "arn:aws:iam::999999999999:root"), False,
        "a different account principal must not prove decrypt reachability")
