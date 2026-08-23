import base64

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
def test_second_page_duplicate_node_prevents_adoption(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    tags = [
        {"Key": "Name", "Value": declaration["name"]},
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "node"},
        {"Key": "mojo:application-role", "Value": declaration["role"]},
    ]

    class _EC2:
        def describe_instances(self, **kwargs):
            suffix = "b" if kwargs.get("NextToken") else "a"
            answer = {"Reservations": [{"Instances": [{
                "InstanceId": f"i-{suffix * 17}", "Tags": tags,
            }]}]}
            if not kwargs.get("NextToken"):
                answer["NextToken"] = "next"
            return answer

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        declaration["role"]: {
            "profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)
    th.assert_eq(list(observed.instances), [],
                 "duplicate names on a later page must never be adopted")
    th.assert_true(any(
        row.status == report.BLIND and "match count" in row.message
        for row in findings),
        f"the complete-set duplicate must block convergence: {findings}")


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
def test_owned_node_hardware_and_root_volume_drift_are_each_withheld(opts):
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

    def rows(case):
        instance = {
            "InstanceId": "i-aaaaaaaaaaaaaaaaa",
            "InstanceType": manifest["nodes"]["instance_type"],
            "ImageId": manifest["nodes"]["ami_id"],
            "VpcId": manifest["network"]["vpc_id"],
            "SubnetId": declaration["subnet_id"],
            "Placement": {"AvailabilityZone": declaration["availability_zone"]},
            "State": {"Name": "running"},
            "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
            "SecurityGroups": [{"GroupId":
                manifest["network"]["node_security_group_id"]}],
            "RootDeviceName": "/dev/xvda",
            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda",
                                     "Ebs": {"VolumeId": "vol-aaaaaaaa"}}],
            "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
        }
        volume = {"VolumeId": "vol-aaaaaaaa",
                  "Size": manifest["nodes"]["volume_gb"], "Encrypted": True}
        if case == "instance type":
            instance["InstanceType"] = "t3.large"
        elif case == "AMI":
            instance["ImageId"] = "ami-deadbeef"
        elif case == "root volume size":
            volume["Size"] += 1
        elif case == "root volume encryption":
            volume["Encrypted"] = False
        return instance, volume

    for case in ("instance type", "AMI", "root volume size",
                 "root volume encryption"):
        instance, volume = rows(case)

        class _EC2:
            def describe_instances(self, **kwargs):
                return {"Reservations": [{"Instances": [instance]}]}

            def describe_volumes(self, **kwargs):
                return {"Volumes": [volume]}

        findings, observed, inventory = [], objict(), {}
        observed.brownfield_profiles = {
            "api": {"profile_arn": declaration["instance_profile_arn"]}}
        brownfield_discover._instances(
            _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)
        th.assert_eq(list(observed.instances), [],
                     f"{case} drift must never reach balancer input")
        th.assert_true(any(case in item.message and item.status == report.BLIND
                           for item in findings),
                       f"{case} drift must fail closed: {findings}")


@th.django_unit_test()
def test_request_service_role_drift_requires_node_replacement(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    declaration["request_service"] = False
    tags = {
        "Name": declaration["name"], "managed-by": "django-mojo",
        "mojo:project": spec.project, "mojo:env": spec.env,
        "mojo:fleet": spec.fleet, "mojo:role": "node",
        "mojo:application-role": declaration["role"],
    }
    instance = {
        "InstanceId": "i-aaaaaaaaaaaaaaaaa",
        "InstanceType": manifest["nodes"]["instance_type"],
        "ImageId": manifest["nodes"]["ami_id"],
        "VpcId": manifest["network"]["vpc_id"],
        "SubnetId": declaration["subnet_id"],
        "Placement": {"AvailabilityZone": declaration["availability_zone"]},
        "State": {"Name": "running"},
        "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
        "SecurityGroups": [{"GroupId":
            manifest["network"]["node_security_group_id"]}],
        "RootDeviceName": "/dev/xvda",
        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda",
                                 "Ebs": {"VolumeId": "vol-aaaaaaaa"}}],
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }

    class _EC2:
        def describe_instances(self, **kwargs):
            return {"Reservations": [{"Instances": [instance]}]}

        def describe_volumes(self, **kwargs):
            return {"Volumes": [{"VolumeId": "vol-aaaaaaaa",
                                  "Size": manifest["nodes"]["volume_gb"],
                                  "Encrypted": True}]}

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        "api": {"profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)

    th.assert_eq(list(observed.instances), [],
                 "a legacy request-serving node must not be reported as "
                 "request_service=false merely because the manifest changed")
    th.assert_true(any("request-service" in item.message
                       and item.status == report.BLIND for item in findings),
                   f"request-role drift must block for replacement: {findings}")


@th.django_unit_test()
def test_explicit_request_role_requires_exact_launch_user_data(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    declaration["request_service"] = False
    tags = {
        "Name": declaration["name"], "managed-by": "django-mojo",
        "mojo:project": spec.project, "mojo:env": spec.env,
        "mojo:fleet": spec.fleet, "mojo:role": "node",
        "mojo:application-role": declaration["role"],
        "mojo:request-service": "false",
    }
    instance = {
        "InstanceId": "i-aaaaaaaaaaaaaaaaa",
        "InstanceType": manifest["nodes"]["instance_type"],
        "ImageId": manifest["nodes"]["ami_id"],
        "VpcId": manifest["network"]["vpc_id"],
        "SubnetId": declaration["subnet_id"],
        "Placement": {"AvailabilityZone": declaration["availability_zone"]},
        "State": {"Name": "running"},
        "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
        "SecurityGroups": [{"GroupId":
            manifest["network"]["node_security_group_id"]}],
        "RootDeviceName": "/dev/xvda",
        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda",
                                 "Ebs": {"VolumeId": "vol-aaaaaaaa"}}],
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }

    class _EC2:
        def describe_instances(self, **kwargs):
            return {"Reservations": [{"Instances": [instance]}]}

        def describe_volumes(self, **kwargs):
            return {"Volumes": [{"VolumeId": "vol-aaaaaaaa",
                                  "Size": manifest["nodes"]["volume_gb"],
                                  "Encrypted": True}]}

        def describe_instance_attribute(self, **kwargs):
            return {"UserData": {"Value": base64.b64encode(
                b"#!/bin/bash\n# stale request-serving bootstrap\n").decode(
                    "ascii")}}

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        "api": {"profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)

    th.assert_eq(list(observed.instances), [],
                 "a matching mutable tag must not override wrong launch evidence")
    th.assert_true(any("request-role user data digest" in item.message
                       and item.status == report.BLIND for item in findings),
                   f"wrong immutable launch evidence must block: {findings}")


@th.django_unit_test()
def test_cache_engine_must_be_valkey(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    wanted = spec.brownfield_manifest["cache"]

    class _Cache:
        def describe_replication_groups(self, **kwargs):
            return {"ReplicationGroups": [{
                "ARN": wanted["replication_group_arn"],
                "ReplicationGroupId": wanted["identifier"], "Engine": "redis",
                "Status": "available", "TransitEncryptionEnabled": True,
                "AuthTokenEnabled": False,
                "NodeGroups": [{"PrimaryEndpoint": {
                    "Address": wanted["endpoint"], "Port": wanted["port"]}}],
                "SecurityGroups": [{"SecurityGroupId":
                    wanted["security_group_ids"][0]}],
                "CacheSubnetGroupName": wanted["subnet_group_name"],
            }]}

        def describe_cache_subnet_groups(self, **kwargs):
            return {"CacheSubnetGroups": [{
                "CacheSubnetGroupName": wanted["subnet_group_name"],
                "VpcId": spec.brownfield_manifest["network"]["vpc_id"]}]}

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._cache(
        _Clients(elasticache=_Cache()), spec.brownfield_manifest,
        spec.brownfield_manifest["network"], findings, observed, inventory)
    th.assert_true(any("Valkey engine" in item.message
                       and item.status == report.BLIND for item in findings),
                   f"Redis must not satisfy an exact Valkey declaration: {findings}")


@th.django_unit_test()
def test_credential_metadata_key_and_application_user_are_enforced(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    credential = spec.brownfield_manifest["database"]["credential"]

    class _S3:
        def __init__(self, metadata):
            self.metadata = metadata

        def head_object(self, **kwargs):
            return {"VersionId": credential["object"]["version_id"],
                    "Metadata": dict(self.metadata,
                                     sha256=credential["object"]["sha256"]),
                    "ETag": "etag", "ContentLength": 10}

    for metadata, expected_phrase in (({}, "metadata key"),
                                      ({"application-user": "wrong"},
                                       "application user metadata")):
        findings, inventory = [], {}
        brownfield_discover._credential_metadata(
            _Clients(s3=_S3(metadata)), credential, findings,
            "database credential", inventory,
            spec.brownfield_manifest["account_id"],
            expected_metadata_value=spec.brownfield_manifest[
                "database"]["application_user"])
        th.assert_true(any(expected_phrase in item.message
                           and item.status == report.BLIND for item in findings),
                       f"{expected_phrase} must fail closed: {findings}")


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
def test_pagination_merges_complete_results_and_discards_partial_failures(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import brownfield_discover, report

    class _EC2:
        def __init__(self, pages):
            self.pages = iter(pages)

        def describe_vpcs(self, **kwargs):
            page = next(self.pages)
            if isinstance(page, Exception):
                raise page
            return page

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "ec2.describe_vpcs", _EC2([
            {"Vpcs": [{"VpcId": "vpc-first"}], "NextToken": "next"},
            {"Vpcs": [{"VpcId": "vpc-second"}]},
        ]))
    th.assert_eq([row["VpcId"] for row in answer["Vpcs"]],
                 ["vpc-first", "vpc-second"],
                 "every provider page must enter the dependency inventory")
    th.assert_eq(findings, [],
                 "complete pagination must not add a blocking finding")

    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "later page"}},
        "DescribeVpcs")
    findings = []
    answer = brownfield_discover._read_pages(
        findings, "ec2.describe_vpcs", _EC2([
            {"Vpcs": [{"VpcId": "vpc-first"}], "NextToken": "next"},
            error,
        ]))
    th.assert_eq(answer, {"Vpcs": []},
                 "a failed later page must discard every partial result")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"a later-page failure must block apply: {findings}")

    for token in (None, "again"):
        pages = [{"Vpcs": [{"VpcId": "vpc-first"}],
                  "NextToken": "again"}]
        if token == "again":
            pages.append({"Vpcs": [{"VpcId": "vpc-second"}],
                          "NextToken": "again"})
        else:
            pages = [{"Vpcs": [{"VpcId": "vpc-first"}],
                      "NextToken": token}]
        findings = []
        answer = brownfield_discover._read_pages(
            findings, "ec2.describe_vpcs", _EC2(pages))
        if token is None:
            th.assert_eq(answer, {"Vpcs": [{"VpcId": "vpc-first"}]},
                         "an absent token without a continuation claim is final")
            th.assert_eq(findings, [],
                         "a provider may finish by omitting its token")
        else:
            th.assert_eq(answer, {"Vpcs": []},
                         "a repeated token must discard partial inventory")
            th.assert_true(any(
                row.code == "dependency.enumeration_truncated"
                for row in findings),
                f"a repeated token must fail closed: {findings}")

    class _KMS:
        def list_grants(self, **kwargs):
            return {"Grants": [{"GrantId": "partial"}], "Truncated": True}

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "kms.list_grants", _KMS(), {"KeyId": "alias/test"})
    th.assert_eq(answer, {"Grants": []},
                 "a missing required continuation token must discard results")
    th.assert_true(any(
        row.code == "dependency.enumeration_truncated" for row in findings),
        f"a missing continuation token must fail closed: {findings}")


@th.django_unit_test()
def test_second_page_iam_policy_collision_is_rejected(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = managed_topology()
    manifest = spec.brownfield_manifest
    managed = manifest["nodes"]["profiles"]["api"]["managed"]
    tags = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "identity"},
        {"Key": "mojo:application-role", "Value": "api"},
    ]
    role_arn = f"arn:aws:iam::123456789012:role/{managed['role_name']}"

    class _IAM:
        def get_instance_profile(self, **kwargs):
            return {"InstanceProfile": {
                "Arn": ("arn:aws:iam::123456789012:instance-profile/"
                        f"{managed['profile_name']}"),
                "Tags": tags, "Roles": [{"Arn": role_arn}]}}

        def get_role(self, **kwargs):
            return {"Role": {"Arn": role_arn, "Tags": tags,
                             "AssumeRolePolicyDocument":
                                 brownfield_discover.EC2_TRUST}}

        def list_role_policies(self, **kwargs):
            if kwargs.get("Marker"):
                return {"PolicyNames": ["unexpected-admin"],
                        "IsTruncated": False}
            return {"PolicyNames": [f"{managed['role_name']}-runtime"],
                    "IsTruncated": True, "Marker": "next"}

        def list_attached_role_policies(self, **kwargs):
            return {"AttachedPolicies": [], "IsTruncated": False}

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._profiles(
        _Clients(iam=_IAM()), manifest, findings, observed, inventory)
    row = observed.brownfield_profiles["api"]
    th.assert_true(row.role_collision and row.profile_collision,
                   f"a second-page policy must withhold the role: {row}")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"the second-page policy collision must block: {findings}")


@th.django_unit_test()
def test_missing_elb_resource_remains_an_optional_empty_collection(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import brownfield_discover, report

    class _ELB:
        def describe_load_balancers(self, **kwargs):
            raise ClientError({"Error": {
                "Code": "LoadBalancerNotFound", "Message": "not found"}},
                "DescribeLoadBalancers")

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "elbv2.describe_load_balancers", _ELB(),
        {"Names": ["missing"]}, not_found=True)
    th.assert_eq(answer, {"LoadBalancers": []},
                 "an absent optional balancer must remain creatable")
    th.assert_eq(findings, [],
                 "normal ELB absence must not be reported as blind")

    class _VanishingELB:
        def describe_load_balancers(self, **kwargs):
            if not kwargs.get("Marker"):
                return {"LoadBalancers": [{"LoadBalancerArn": "partial"}],
                        "NextMarker": "next"}
            raise ClientError({"Error": {
                "Code": "LoadBalancerNotFound", "Message": "vanished"}},
                "DescribeLoadBalancers")

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "elbv2.describe_load_balancers", _VanishingELB(),
        {"Names": ["vanishing"]}, not_found=True)
    th.assert_eq(answer, {"LoadBalancers": []},
                 "later-page absence must discard partial edge evidence")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"later-page absence must fail closed: {findings}")


@th.django_unit_test()
def test_storage_inventory_uses_exact_metadata_without_prefix_occupancy(opts):
    from mojo.deploy.provision import brownfield_discover

    spec = topology()
    manifest = spec.brownfield_manifest
    by_key = {row["key"]: row for row in manifest["bootstrap"].values()}

    class _S3:
        def get_bucket_location(self, **kwargs):
            return {"LocationConstraint": manifest["region"]}

        def head_object(self, **kwargs):
            reference = by_key[kwargs["Key"]]
            return {"VersionId": reference["version_id"], "ETag": "etag",
                    "ContentLength": 10,
                    "Metadata": {"sha256": reference["sha256"]}}

        def list_objects_v2(self, **kwargs):
            raise AssertionError("storage prefixes must never be enumerated")

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._storage(
        _Clients(s3=_S3()), manifest, findings, observed, inventory)
    expected = {
        label: {"bucket": reference["bucket"],
                "prefix": reference["prefix"],
                "region": manifest["region"]}
        for label, reference in manifest["storage"].items()
    }
    th.assert_eq(inventory["storage"], expected,
                 "storage evidence must contain only exact stable fields")
    th.assert_true(observed.bootstrap_payload,
                   f"pinned bootstrap metadata must still be proven: {inventory}")
    th.assert_eq(findings, [],
                 f"exact storage metadata should converge: {findings}")


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
    private_group = {"IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
        "IpRanges": [{"CidrIp": "172.31.0.0/16"}]}]}
    th.assert_true(brownfield_discover._allows_cidr_port(
        private_group, 443, "172.31.0.0/16"),
        "disabled client-IP preservation must accept the exact VPC source path")
    th.assert_eq(brownfield_discover._allows_cidr_port(
        private_group, 443, "10.0.0.0/8"), False,
        "a broader or unrelated source CIDR must not be inferred safe")
    th.assert_eq(brownfield_discover._allows_world_port(
        private_group, 443), False,
        "the private NLB path must not accidentally prove world ingress")
    policy = {"Statement": [{
        "Effect": "Allow", "Principal": {"AWS":
            "arn:aws:iam::123456789012:root"}, "Action": "kms:*"}]}
    th.assert_true(brownfield_discover._policy_allows(
        policy, "arn:aws:iam::123456789012:root"),
        "the exact account IAM delegation must be recognized")
    th.assert_eq(brownfield_discover._policy_allows(
        policy, "arn:aws:iam::999999999999:root"), False,
        "a different account principal must not prove decrypt reachability")


@th.django_unit_test()
def test_node_ingress_tracks_declared_client_ip_posture(opts):
    from mojo.deploy.provision import brownfield_discover, report

    world = {"IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        for port in (80, 443)]}
    private = {"IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}, {
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
        "IpRanges": [{"CidrIp": "172.31.0.0/16"}]}]}

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, world, "172.31.0.0/16", {})
    th.assert_eq(findings, [],
                 "omission must preserve the existing world-ingress contract")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, private, "172.31.0.0/16",
        {"api_preserve_client_ip": False})
    th.assert_eq(findings, [],
                 "disabled preservation must accept exact VPC-only API ingress")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, world, "172.31.0.0/16",
        {"api_preserve_client_ip": False})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"world ingress must block a private-source declaration: {findings}")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, {"IpPermissions": []}, "172.31.0.0/16",
        {"api_preserve_client_ip": False})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"missing VPC ingress must block NLB target launch: {findings}")

    nlb_group_id = "sg-nlb"
    node_group_id = "sg-node"
    locked_node = {"GroupId": node_group_id, "IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "UserIdGroupPairs": [{"GroupId": nlb_group_id}]}
        for port in (80, 443)]}
    public_nlb = {"GroupId": nlb_group_id, "IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        for port in (80, 443)], "IpPermissionsEgress": [{
            "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
            "UserIdGroupPairs": [{"GroupId": node_group_id}]}
            for port in (80, 443)]}
    findings = []
    brownfield_discover._validate_node_ingress(
        findings, locked_node, "172.31.0.0/16",
        {"security_group_id": nlb_group_id,
         "api_preserve_client_ip": True,
         "certbot_preserve_client_ip": True},
        {nlb_group_id: public_nlb})
    th.assert_eq(findings, [],
                 "an exact NLB-SG boundary must preserve client IPs safely")

    ipv6_only = dict(public_nlb, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}
        for port in (80, 443)])
    findings = []
    brownfield_discover._validate_node_ingress(
        findings, locked_node, "172.31.0.0/16",
        {"security_group_id": nlb_group_id},
        {nlb_group_id: ipv6_only})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"IPv6-only ingress cannot prove an IPv4 NLB public: {findings}")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, world, "172.31.0.0/16",
        {"security_group_id": nlb_group_id},
        {nlb_group_id: public_nlb})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the NLB SG must never coexist with direct world target ingress: {findings}")

    narrowed_direct = dict(locked_node, IpPermissions=(
        list(locked_node["IpPermissions"]) + [{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "IpRanges": [{"CidrIp": "198.51.100.0/24"}]}]))
    findings = []
    brownfield_discover._validate_node_ingress(
        findings, narrowed_direct, "172.31.0.0/16",
        {"security_group_id": nlb_group_id},
        {nlb_group_id: public_nlb})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"any non-NLB source on a target port must block: {findings}")


@th.django_unit_test()
def test_existing_nlb_must_carry_the_declared_create_time_security_group(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["load_balancer"]["security_group_id"] = (
        "sg-3123456789abcdef0")
    balancer = {
        "Type": "network", "Scheme": "internet-facing",
        "AvailabilityZones": [{"SubnetId": subnet_id}
                              for subnet_id in spec.nlb_subnet_ids],
        "SecurityGroups": ["sg-4123456789abcdef0"],
    }
    findings = []
    accepted = brownfield_discover._validate_balancer_shape(
        findings, balancer, manifest)
    th.assert_eq(accepted, False,
                 "an NLB with a wrong/missing first SG must never be adopted")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the irreversible SG mismatch must block apply: {findings}")

    balancer["SecurityGroups"] = ["sg-3123456789abcdef0"]
    findings = []
    accepted = brownfield_discover._validate_balancer_shape(
        findings, balancer, manifest)
    th.assert_true(accepted,
                   f"the exact declared NLB SG must be reusable: {findings}")


@th.django_unit_test()
def test_omitted_client_ip_fields_add_no_attribute_provider_call(opts):
    from mojo.deploy.provision import brownfield_discover

    class _EC2:
        def describe_addresses(self, **kwargs):
            return {"Addresses": []}

    class _ELB:
        def __init__(self):
            self.attribute_arns = []

        def describe_load_balancers(self, **kwargs):
            return {"LoadBalancers": []}

        def describe_target_groups(self, Names):
            return {"TargetGroups": [{
                "TargetGroupName": Names[0],
                "TargetGroupArn": f"arn:tg:{Names[0]}",
                "VpcId": topology().brownfield_manifest["network"]["vpc_id"],
            }]}

        def describe_tags(self, ResourceArns):
            spec = topology()
            return {"TagDescriptions": [{
                "ResourceArn": ResourceArns[0], "Tags": [
                    {"Key": key, "Value": value} for key, value in {
                        "managed-by": "django-mojo",
                        "mojo:project": spec.project,
                        "mojo:env": spec.env,
                        "mojo:fleet": spec.fleet,
                        "mojo:role": "balancer",
                    }.items()]}]}

        def describe_target_group_attributes(self, TargetGroupArn):
            self.attribute_arns.append(TargetGroupArn)
            return {"Attributes": [{
                "Key": "preserve_client_ip.enabled", "Value": "true"}]}

        def describe_target_health(self, **kwargs):
            return {"TargetHealthDescriptions": []}

    spec = topology()
    elb = _ELB()
    observed, inventory, findings = objict(), {}, []
    brownfield_discover._owned_edge(
        _Clients(ec2=_EC2(), elbv2=elb), spec,
        spec.brownfield_manifest, findings, observed, inventory)
    th.assert_eq(elb.attribute_arns, [],
                 "omission must not require a new IAM read/provider call")
    th.assert_eq("target_group_attributes" in inventory["owned_edge"], False,
                 "omission must not change the dependency digest shape")

    spec.brownfield_manifest["load_balancer"][
        "api_preserve_client_ip"] = True
    elb = _ELB()
    observed, inventory, findings = objict(), {}, []
    brownfield_discover._owned_edge(
        _Clients(ec2=_EC2(), elbv2=elb), spec,
        spec.brownfield_manifest, findings, observed, inventory)
    th.assert_eq(len(elb.attribute_arns), 1,
                 "only the explicitly declared target group may be read")
    th.assert_eq(inventory["owned_edge"]["target_group_attributes"]["api"],
                 {"preserve_client_ip.enabled": "true"},
                 "the exact observed value must enter the dependency digest")
