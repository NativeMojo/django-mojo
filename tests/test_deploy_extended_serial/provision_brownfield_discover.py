import base64

from objict import objict
from testit import helpers as th

from test_deploy.brownfield_fixture import managed_topology, topology


class _Clients:
    def __init__(self, **clients):
        self.clients = clients

    def get(self, name):
        return self.clients[name]


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
