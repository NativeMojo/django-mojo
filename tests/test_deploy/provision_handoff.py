import io
import json
import os
import tempfile
from unittest import mock

from testit import helpers as th

from .brownfield_fixture import ACCOUNT, handoff_topology


def _mapping(az, subnet, allocation=None, public_ip=None,
             private_ip="172.31.0.10"):
    return {"availability_zone": az, "subnet_id": subnet,
            "allocation_id": allocation, "public_ip": public_ip,
            "private_ipv4": private_ip, "ipv6": None,
            "source_nat_ipv6_prefix": None}


def _source():
    return {
        "allocation_id": "eipalloc-0123456789abcdef0",
        "public_ip": "203.0.113.10", "association_id": "eipassoc-old",
        "network_interface_id": "eni-source", "private_ip": "172.31.9.10",
        "instance_id": "i-0123456789abcdef0",
        "target_subnet_id": "subnet-0123456789abcdef0",
        "availability_zone": "us-west-2a",
        "network_border_group": "us-west-2", "tags": {},
        "source_subnet_id": "subnet-source",
        "source_availability_zone": "us-west-2c",
        "source_vpc_id": "vpc-0123456789abcdef0", "source_status": "in-use",
        "source_attachment_id": "eni-attach-source",
        "source_device_index": 0, "source_attachment_status": "attached",
    }


def _plan(topology, mapping=None):
    from mojo.deploy.provision import handoff

    mapping = mapping or {
        "us-west-2a": _mapping(
            "us-west-2a", "subnet-0123456789abcdef0", None, "198.51.100.10"),
        "us-west-2b": _mapping(
            "us-west-2b", "subnet-1123456789abcdef0", None, "198.51.100.11"),
    }
    lb_arn = "arn:lb:shadow"
    owned = {"managed-by": "django-mojo", "mojo:env": "prod",
             "mojo:fleet": "shadow", "mojo:project": "maestro",
             "mojo:role": "balancer"}
    plan = {
        "schema": handoff.SCHEMA, "account_id": ACCOUNT,
        "region": "us-west-2", "role_arn": topology.eip_handoff_role_arn,
        "manifest_digest": topology.manifest_digest,
        "environment": "prod", "project": "maestro", "fleet": "shadow",
        "load_balancer": {"arn": lb_arn, "name": "shadow",
                          "state": "active", "map": mapping,
                          "cross_zone": True},
        "sources": {"us-west-2a": _source()},
        "listeners": [
            {"arn": "arn:listener:https", "port": 443, "protocol": "TCP",
             "target_group_arn": "arn:tg:api"},
            {"arn": "arn:listener:http", "port": 80, "protocol": "TCP",
             "target_group_arn": "arn:tg:certbot"}],
        "targets": {},
        "ownership_tags": {lb_arn: owned, "arn:tg:api": owned,
                            "arn:tg:certbot": owned},
        "canary_definitions": json.loads(json.dumps(
            topology.eip_handoff_canaries)),
        "canaries": [{"name": "api", "address": "198.51.100.10",
                      "ok": True, "status": 200, "certificate": True,
                      "marker": True}],
        "manage_dns": False,
        "local_journal": topology.eip_handoff_local_journal,
        "remote_prefix": (f"s3://{topology.eip_handoff_bucket}/"
                          f"{topology.eip_handoff_prefix}"),
        "inverse": {"us-west-2a": {
            key: _source()[key] for key in (
                "allocation_id", "network_interface_id", "private_ip",
                "source_subnet_id", "source_availability_zone", "source_vpc_id",
                "source_attachment_id", "source_device_index",
                "source_attachment_status",
                "target_subnet_id")}},
        "interruption": "bounded", "residual_public_edge": "single ingress",
    }
    plan["load_balancer"]["map_digest"] = handoff.digest(mapping)
    plan["plan_digest"] = handoff.digest(plan)
    return plan


class _S3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.counter = 0

    def get_bucket_versioning(self, **kwargs):
        return {"Status": "Enabled"}

    def get_bucket_encryption(self, **kwargs):
        return {"ServerSideEncryptionConfiguration": {"Rules": [{
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey",
                                          "Message": "missing"}}, "GetObject")
        body, etag = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": f'"{etag}"'}

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey",
                                          "Message": "missing"}}, "HeadObject")
        body, etag = self.objects[Key]
        return {"ContentLength": len(body), "ETag": f'"{etag}"'}

    def put_object(self, Bucket, Key, Body, **kwargs):
        from botocore.exceptions import ClientError
        self.calls.append((Key, dict(kwargs)))
        existing = self.objects.get(Key)
        if kwargs.get("IfNoneMatch") == "*" and existing:
            raise ClientError({"Error": {"Code": "PreconditionFailed",
                                          "Message": "exists"}}, "PutObject")
        if kwargs.get("IfMatch") and (
                not existing or existing[1] != kwargs["IfMatch"]):
            raise ClientError({"Error": {"Code": "PreconditionFailed",
                                          "Message": "etag"}}, "PutObject")
        self.counter += 1
        etag = f"etag-{self.counter}"
        self.objects[Key] = (bytes(Body), etag)
        return {"ETag": f'"{etag}"'}


class _CaptureStore:
    def __init__(self):
        self.snapshots = []

    def advance(self, journal):
        self.snapshots.append(json.loads(json.dumps(journal)))


class _STS:
    def get_caller_identity(self):
        return {"Account": ACCOUNT,
                "Arn": (f"arn:aws:sts::{ACCOUNT}:assumed-role/"
                        "mojo-eip-handoff/test")}


class _EC2:
    def __init__(self, source_state="free"):
        self.source_state = source_state
        self.target = False
        self.associate_calls = []
        self.disassociate_calls = []

    def describe_addresses(self, AllocationIds):
        source = _source()
        row = {"AllocationId": source["allocation_id"],
               "PublicIp": source["public_ip"], "Domain": "vpc",
               "NetworkBorderGroup": "us-west-2"}
        if self.target:
            row.update({"AssociationId": "eipassoc-elb",
                        "NetworkInterfaceId": "eni-elb",
                        "PrivateIpAddress": "172.31.0.20"})
        elif self.source_state == "source":
            row.update({"AssociationId": "eipassoc-old",
                        "NetworkInterfaceId": source["network_interface_id"],
                        "PrivateIpAddress": source["private_ip"],
                        "InstanceId": source["instance_id"]})
        elif self.source_state == "unexpected":
            row.update({"AssociationId": "eipassoc-stranger",
                        "NetworkInterfaceId": "eni-stranger",
                        "PrivateIpAddress": "172.31.99.99",
                        "InstanceId": "i-stranger"})
        return {"Addresses": [row]}

    def describe_network_interfaces(self, NetworkInterfaceIds):
        rows = []
        for eni_id in NetworkInterfaceIds:
            if eni_id == "eni-elb":
                rows.append({"NetworkInterfaceId": eni_id,
                             "RequesterManaged": True,
                             "InterfaceType": "network_load_balancer",
                             "SubnetId": "subnet-0123456789abcdef0",
                             "VpcId": "vpc-0123456789abcdef0",
                             "AvailabilityZone": "us-west-2a"})
            elif eni_id == "eni-source":
                source = _source()
                rows.append({
                    "NetworkInterfaceId": eni_id, "RequesterManaged": False,
                    "InterfaceType": "interface", "VpcId":
                    "vpc-0123456789abcdef0", "SubnetId": "subnet-source",
                    "AvailabilityZone": "us-west-2c",
                    "Status": "in-use",
                    "Attachment": {"InstanceId": source["instance_id"],
                                   "AttachmentId": "eni-attach-source",
                                   "DeviceIndex": 0, "Status": "attached"},
                    "PrivateIpAddresses": [{
                        "PrivateIpAddress": source["private_ip"]}],
                })
        return {"NetworkInterfaces": rows}

    def associate_address(self, **kwargs):
        self.associate_calls.append(kwargs)
        self.source_state = "source"
        self.target = False
        return {"AssociationId": "eipassoc-restored"}

    def disassociate_address(self, **kwargs):
        self.disassociate_calls.append(kwargs)
        self.source_state = "free"
        self.target = False
        return {}


class _ELB:
    def __init__(self, ec2, mapping):
        self.ec2 = ec2
        self.mapping = dict(mapping)
        self.set_calls = []

    def describe_load_balancers(self, **kwargs):
        return {"LoadBalancers": [{
            "LoadBalancerArn": "arn:lb:shadow", "State": {"Code": "active"},
            "AvailabilityZones": [{
                "ZoneName": az, "SubnetId": row["subnet_id"],
                "LoadBalancerAddresses": [{
                    "AllocationId": row.get("allocation_id"),
                    "IpAddress": row.get("public_ip"),
                    "PrivateIPv4Address": row.get("private_ipv4")}]}
                for az, row in sorted(self.mapping.items())]}]}

    def describe_tags(self, ResourceArns):
        tags = [{"Key": key, "Value": value} for key, value in {
            "managed-by": "django-mojo", "mojo:project": "maestro",
            "mojo:env": "prod", "mojo:fleet": "shadow",
            "mojo:role": "balancer"}.items()]
        return {"TagDescriptions": [
            {"ResourceArn": arn, "Tags": tags} for arn in ResourceArns]}

    def set_subnets(self, LoadBalancerArn, SubnetMappings):
        self.set_calls.append(list(SubnetMappings))
        by_subnet = {
            "subnet-0123456789abcdef0": "us-west-2a",
            "subnet-1123456789abcdef0": "us-west-2b"}
        updated = {}
        for row in SubnetMappings:
            az = by_subnet[row["SubnetId"]]
            allocation = row.get("AllocationId")
            prior = self.mapping.get(az) or {}
            updated[az] = _mapping(
                az, row["SubnetId"], allocation,
                "203.0.113.10" if allocation else prior.get(
                    "public_ip", f"198.51.100.{10 + len(updated)}"),
                "172.31.0.20" if allocation else row.get(
                    "PrivateIPv4Address", prior.get(
                        "private_ipv4", "172.31.0.10")))
            if allocation:
                self.ec2.target = True
                self.ec2.source_state = "free"
        if "us-west-2a" in updated and not updated["us-west-2a"].get(
                "allocation_id"):
            self.ec2.target = False
        self.mapping = updated
        return {"AvailabilityZones": []}


class _MultiEC2:
    def __init__(self, sources, states=None):
        self.sources = sources
        self.states = dict(states or {az: "source" for az in sources})
        self.disassociate_calls = []
        self.associate_calls = []

    def describe_addresses(self, AllocationIds):
        by_allocation = {row["allocation_id"]: (az, row)
                         for az, row in self.sources.items()}
        answer = []
        for allocation in AllocationIds:
            az, source = by_allocation[allocation]
            row = {"AllocationId": allocation,
                   "PublicIp": source["public_ip"], "Domain": "vpc",
                   "NetworkBorderGroup": "us-west-2"}
            if self.states[az] == "source":
                row.update({
                    "AssociationId": source["association_id"],
                    "NetworkInterfaceId": source["network_interface_id"],
                    "PrivateIpAddress": source["private_ip"],
                    "InstanceId": source["instance_id"]})
            elif self.states[az] == "target":
                row.update({"AssociationId": f"eipassoc-elb-{az}",
                            "NetworkInterfaceId": f"eni-elb-{az}",
                            "PrivateIpAddress": f"172.31.0.{20 + len(answer)}"})
            answer.append(row)
        return {"Addresses": answer}

    def describe_network_interfaces(self, NetworkInterfaceIds):
        return {"NetworkInterfaces": [{
            "NetworkInterfaceId": eni, "RequesterManaged": True,
            "InterfaceType": "network_load_balancer",
            "SubnetId": self.sources[eni[len("eni-elb-"):]]["target_subnet_id"],
            "VpcId": self.sources[eni[len("eni-elb-"):]]["source_vpc_id"],
            "AvailabilityZone": eni[len("eni-elb-"):],
        } for eni in NetworkInterfaceIds]}

    def disassociate_address(self, AssociationId):
        for az, source in self.sources.items():
            if source["association_id"] == AssociationId:
                self.states[az] = "free"
                self.disassociate_calls.append(az)
                return {}
        raise AssertionError(f"unexpected association {AssociationId}")

    def associate_address(self, **kwargs):
        for az, source in self.sources.items():
            if source["allocation_id"] == kwargs["AllocationId"]:
                self.states[az] = "source"
                self.associate_calls.append(az)
                return {"AssociationId": f"eipassoc-restored-{az}"}
        raise AssertionError(f"unexpected allocation {kwargs}")


class _MultiELB:
    def __init__(self, ec2, mapping):
        self.ec2 = ec2
        self.mapping = dict(mapping)
        self.set_calls = []

    def describe_load_balancers(self, **kwargs):
        return {"LoadBalancers": [{
            "LoadBalancerArn": "arn:lb:shadow", "State": {"Code": "active"},
            "AvailabilityZones": [{
                "ZoneName": az, "SubnetId": row["subnet_id"],
                "LoadBalancerAddresses": [{
                    "AllocationId": row.get("allocation_id"),
                    "IpAddress": row.get("public_ip"),
                    "PrivateIPv4Address": row.get("private_ipv4")}]}
                for az, row in sorted(self.mapping.items())]}]}

    def set_subnets(self, LoadBalancerArn, SubnetMappings):
        self.set_calls.append([dict(row) for row in SubnetMappings])
        by_subnet = {
            "subnet-0123456789abcdef0": "us-west-2a",
            "subnet-1123456789abcdef0": "us-west-2b"}
        previous = dict(self.mapping)
        updated = {}
        for row in SubnetMappings:
            az = by_subnet[row["SubnetId"]]
            allocation = row.get("AllocationId")
            prior = previous.get(az) or {}
            source = self.ec2.sources.get(az) or {
                "public_ip": prior.get("public_ip")}
            if allocation:
                self.ec2.states[az] = "target"
            updated[az] = _mapping(
                az, row["SubnetId"], allocation,
                source["public_ip"] if allocation else prior.get(
                    "public_ip", f"198.51.100.{30 + len(updated)}"),
                f"172.31.0.{20 + len(updated)}" if allocation else prior.get(
                    "private_ipv4", "172.31.0.10"))
        for az, row in previous.items():
            if (az not in updated and row.get("allocation_id")
                    and self.ec2.states[az] == "target"):
                self.ec2.states[az] = "free"
        self.mapping = updated
        return {"AvailabilityZones": []}

    def describe_tags(self, ResourceArns):
        tags = [{"Key": key, "Value": value} for key, value in {
            "managed-by": "django-mojo", "mojo:project": "maestro",
            "mojo:env": "prod", "mojo:fleet": "shadow",
            "mojo:role": "balancer"}.items()]
        return {"TagDescriptions": [
            {"ResourceArn": arn, "Tags": tags} for arn in ResourceArns]}


def _two_source_plan(topology):
    from mojo.deploy.provision import handoff

    plan = _plan(topology)
    second = dict(_source())
    second.update({
        "allocation_id": "eipalloc-1123456789abcdef0",
        "public_ip": "203.0.113.11", "association_id": "eipassoc-old-b",
        "network_interface_id": "eni-source-b", "private_ip": "172.31.9.11",
        "instance_id": "i-1123456789abcdef0",
        "target_subnet_id": "subnet-1123456789abcdef0",
        "availability_zone": "us-west-2b",
        "source_subnet_id": "subnet-source-b",
        "source_availability_zone": "us-west-2d",
        "source_attachment_id": "eni-attach-source-b"})
    plan["sources"]["us-west-2b"] = second
    plan["inverse"]["us-west-2b"] = {
        key: second[key] for key in (
            "allocation_id", "network_interface_id", "private_ip",
            "source_subnet_id", "source_availability_zone", "source_vpc_id",
            "source_attachment_id", "source_device_index",
            "source_attachment_status",
            "target_subnet_id")}
    plan.pop("plan_digest")
    plan["plan_digest"] = handoff.digest(plan)
    return plan


def _clients(topology, ec2=None, elb=None, s3=None):
    from mojo.deploy.provision import handoff
    overrides = {"sts": _STS()}
    if ec2:
        overrides["ec2"] = ec2
    if elb:
        overrides["elbv2"] = elb
    if s3:
        overrides["s3"] = s3
    return handoff.HandoffClients(topology=topology, **overrides)


def _prime_rehearsal(s3, topology, plan, operation="op-prime-rehearsal"):
    from mojo.deploy.provision import handoff

    journal = handoff._new_journal(operation, plan, "rehearsal")
    store = handoff.JournalStore(
        _clients(topology, s3=s3), topology, operation)
    store.acquire_lock(plan["plan_digest"], journal)
    store.finish_lock("rehearsed")
    return store


class _PreflightSTS:
    def __init__(self, drift=None):
        self.drift = drift

    def get_caller_identity(self):
        account = "999999999999" if self.drift == "account" else ACCOUNT
        role = "other" if self.drift == "role" else "mojo-eip-handoff"
        return {"Account": account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/{role}/test"}


class _PreflightEC2:
    def __init__(self, topology, drift=None):
        self.topology = topology
        self.drift = drift

    def describe_subnets(self, SubnetIds):
        rows = []
        for declaration in self.topology.brownfield_manifest[
                "network"]["public_subnets"]:
            rows.append({"SubnetId": declaration["id"],
                         "AvailabilityZone": declaration["availability_zone"],
                         "VpcId": "vpc-0123456789abcdef0"})
        return {"Subnets": rows}

    def describe_addresses(self, AllocationIds):
        source = _source()
        row = {
            "AllocationId": source["allocation_id"],
            "PublicIp": source["public_ip"], "Domain": "vpc",
            "NetworkBorderGroup": ("us-east-1" if self.drift == "nbg"
                                   else "us-west-2"),
            "AssociationId": source["association_id"],
            "NetworkInterfaceId": source["network_interface_id"],
            "PrivateIpAddress": source["private_ip"],
            "InstanceId": source["instance_id"], "Tags": []}
        if self.drift == "eip-form":
            row["CustomerOwnedIp"] = "198.51.100.1"
        if self.drift == "source-association":
            row.pop("AssociationId")
        return {"Addresses": [row]}

    def describe_network_interfaces(self, NetworkInterfaceIds):
        source = _source()
        association = {
            "AllocationId": source["allocation_id"],
            "AssociationId": source["association_id"],
            "PublicIp": source["public_ip"]}
        return {"NetworkInterfaces": [{
            "NetworkInterfaceId": source["network_interface_id"],
            "VpcId": "vpc-0123456789abcdef0", "SubnetId": "subnet-source",
            "AvailabilityZone": "us-west-2c", "Status": "in-use",
            "Attachment": {"InstanceId": source["instance_id"],
                           "AttachmentId": "eni-attach-source",
                           "DeviceIndex": 0, "Status": "attached"},
            "PrivateIpAddresses": [{"PrivateIpAddress": source["private_ip"],
                                     "Association": association}],
        }]}

    def describe_instances(self, **kwargs):
        rows = []
        for index, declaration in enumerate(
                self.topology.brownfield_manifest["nodes"]["items"], 1):
            rows.append({"InstanceId": f"i-node-{index}",
                         "Tags": [{"Key": "Name",
                                   "Value": declaration["name"]}]})
        return {"Reservations": [{"Instances": rows}]}


class _PreflightELB:
    def __init__(self, topology, drift=None):
        from mojo.deploy.provision import balancer

        self.topology = topology
        self.drift = drift
        self.arn = (f"arn:aws:elasticloadbalancing:us-west-2:{ACCOUNT}:"
                    "loadbalancer/net/maestro-shadow/abcdef")
        self.groups = balancer.target_group_specs(
            topology, topology.brownfield_manifest["network"]["vpc_id"])
        for role in self.groups:
            self.groups[role] = dict(
                self.groups[role], TargetGroupArn=f"arn:tg:{role}")
        if drift == "target-group":
            self.groups["api"]["Port"] = 80

    def describe_load_balancers(self, **kwargs):
        subnet_rows = self.topology.brownfield_manifest[
            "network"]["public_subnets"]
        if self.drift == "one-az":
            subnet_rows = subnet_rows[:1]
        zones = []
        for index, declaration in enumerate(subnet_rows, 10):
            zones.append({
                "ZoneName": declaration["availability_zone"],
                "SubnetId": declaration["id"],
                "LoadBalancerAddresses": [{
                    "IpAddress": f"198.51.100.{index}",
                    "PrivateIPv4Address": f"172.31.0.{index}"}]})
        return {"LoadBalancers": [{
            "LoadBalancerArn": self.arn,
            "LoadBalancerName": "maestro-shadow-nlb",
            "VpcId": "vpc-0123456789abcdef0",
            "State": {"Code": "provisioning" if self.drift == "inactive"
                      else "active"},
            "AvailabilityZones": zones}]}

    def describe_load_balancer_attributes(self, **kwargs):
        return {"Attributes": [{
            "Key": "load_balancing.cross_zone.enabled",
            "Value": "false" if self.drift == "cross-zone" else "true"}]}

    def describe_target_groups(self, Names):
        role = ("api" if Names[0] ==
                self.topology.brownfield_manifest[
                    "load_balancer"]["api_target_group"] else "certbot")
        return {"TargetGroups": [self.groups[role]]}

    def describe_listeners(self, **kwargs):
        protocol = "TLS" if self.drift == "listener" else "TCP"
        return {"Listeners": [
            {"ListenerArn": "arn:listener:https", "Port": 443,
             "Protocol": protocol, "DefaultActions": [{
                 "Type": "forward", "TargetGroupArn": "arn:tg:api"}]},
            {"ListenerArn": "arn:listener:http", "Port": 80,
             "Protocol": "TCP", "DefaultActions": [{
                 "Type": "forward", "TargetGroupArn": "arn:tg:certbot"}]},
        ]}

    def describe_tags(self, ResourceArns):
        values = {
            "managed-by": "django-mojo", "mojo:project": "maestro",
            "mojo:env": "prod", "mojo:fleet": "shadow",
            "mojo:role": "balancer"}
        return {"TagDescriptions": [{
            "ResourceArn": arn,
            "Tags": [{"Key": key, "Value": value}
                     for key, value in values.items()]}
            for arn in ResourceArns]}

    def describe_target_health(self, TargetGroupArn):
        if TargetGroupArn == "arn:tg:api":
            targets = [("i-node-1", 443), ("i-node-2", 443),
                       (_source()["instance_id"], 443)]
        else:
            targets = [("i-node-1", 80)]
        state = "unhealthy" if self.drift == "health" else "healthy"
        return {"TargetHealthDescriptions": [{
            "Target": {"Id": instance, "Port": port},
            "TargetHealth": {"State": state}}
            for instance, port in targets]}


def _preflight_clients(topology, drift=None):
    from mojo.deploy.provision import handoff

    connection = handoff.HandoffClients(
        topology=topology, sts=_PreflightSTS(drift),
        ec2=_PreflightEC2(topology, drift),
        elbv2=_PreflightELB(topology, drift))
    if drift == "region":
        connection.region_name = "us-east-1"
    return connection


@th.django_unit_test()
def test_cutover_client_has_no_dns_release_or_broad_reassociation(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    connection = _clients(topology, ec2=_EC2())
    for service, method in (("route53", None), ("ec2", "release_address")):
        raised = None
        try:
            client = connection.get(service)
            if method:
                getattr(client, method)
        except handoff.HandoffRefused as err:
            raised = err
        th.assert_true(raised is not None,
                       f"{service}.{method} must be absent from cutover authority")

    import ast
    import inspect
    tree = ast.parse(inspect.getsource(handoff))
    forbidden_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if name in ("release_address", "delete_load_balancer",
                    "terminate_instances"):
            forbidden_calls.append(name)
    th.assert_eq(forbidden_calls, [],
                 f"handoff source must never expose permanent teardown: "
                 f"{forbidden_calls}")


@th.django_unit_test()
def test_identity_and_source_preflight_refuse_every_exactness_drift(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    good_identity = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/mojo-eip-handoff/test"}
    for label, identity, region in (
            ("account", dict(good_identity, Account="999999999999"),
             "us-west-2"),
            ("role", dict(good_identity,
                          Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/other/test"),
             "us-west-2"),
            ("region", good_identity, "us-east-1")):
        problems = []
        handoff._validate_identity(identity, region, topology, problems)
        th.assert_true(problems, f"preflight must refuse {label} drift")

    declaration = topology.brownfield_manifest["network"]["public_subnets"][0]
    base = {
        "AllocationId": _source()["allocation_id"], "Domain": "vpc",
        "NetworkBorderGroup": "us-west-2", "PublicIp": _source()["public_ip"],
        "AssociationId": _source()["association_id"],
        "NetworkInterfaceId": _source()["network_interface_id"],
        "PrivateIpAddress": _source()["private_ip"],
        "InstanceId": _source()["instance_id"], "Tags": []}
    for label, changes in (
            ("network border group", {"NetworkBorderGroup": "us-east-1"}),
            ("ownership", {"Tags": [{"Key": "managed-by",
                                       "Value": "someone-else"}]}),
            ("source reversibility", {"PrivateIpAddress": None}),
            ("ordinary VPC EIP", {"ServiceManaged": "alb"})):
        row = dict(base, **changes)
        problems = []
        handoff._validate_source_address(
            topology, row, declaration, {_source()["instance_id"]}, problems)
        th.assert_true(problems,
                       f"preflight must refuse {label} drift: {problems}")


@th.django_unit_test()
def test_build_plan_table_driven_pre_destructive_gates_and_success(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    for drift in ("account", "role", "region", "eip-form", "nbg",
                  "source-association", "inactive", "one-az", "cross-zone",
                  "listener", "target-group", "health", "canary"):
        refused = None
        try:
            handoff.build_plan(
                _preflight_clients(topology, drift), topology,
                canary_runner=(lambda definition, address, value=drift: {
                    "ok": value != "canary", "status": 200,
                    "certificate": value != "canary", "marker": True}))
        except handoff.HandoffRefused as err:
            refused = err
        th.assert_true(refused is not None,
                       f"build_plan must refuse preflight drift {drift}")

    plan = handoff.build_plan(
        _preflight_clients(topology), topology,
        canary_runner=lambda definition, address: {
            "ok": True, "status": 200, "certificate": True, "marker": True})
    handoff._bind_plan(plan, plan["plan_digest"])
    th.assert_eq(len(plan["ownership_tags"]), 3,
                 "preview digest must bind NLB plus both target-group tags")
    th.assert_eq([(row["id"], row["port"])
                  for row in plan["targets"]["api"]], [
                      ("i-0123456789abcdef0", 443),
                      ("i-node-1", 443), ("i-node-2", 443)],
                 "preview digest must bind exact API target id/port rows")
    th.assert_eq(len(plan["canaries"]), 2,
                 "success preview must prove the app through both shadow IPs")


@th.django_unit_test()
def test_resume_and_rollback_bind_current_manifest_and_coordinates(opts):
    from mojo.deploy.provision import handoff

    for label, mutate in (
            ("manifest digest",
             lambda topology: setattr(topology, "manifest_digest", "f" * 64)),
            ("canary weakening",
             lambda topology: topology.eip_handoff_canaries.clear()),
            ("journal prefix",
             lambda topology: setattr(
                 topology, "eip_handoff_prefix", "other/prefix")),
            ("allocation map",
             lambda topology: topology.nlb_eip_allocations.update({
                 "us-west-2a": "eipalloc-fffffffffffffffff"}))):
        topology = handoff_topology()
        plan = _plan(topology)
        mutate(topology)
        refused = None
        try:
            handoff._validate_topology_binding(topology, plan)
        except handoff.HandoffError as err:
            refused = err
        th.assert_true(refused is not None,
                       f"recovery must refuse current {label} drift")


@th.django_unit_test()
def test_cutover_role_policy_scopes_every_mutation_resource(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    plan = _plan(topology)
    old_arn = plan["load_balancer"]["arn"]
    plan["load_balancer"]["arn"] = (
        f"arn:aws:elasticloadbalancing:us-west-2:{ACCOUNT}:"
        "loadbalancer/net/maestro-shadow/0123456789abcdef")
    plan["ownership_tags"][plan["load_balancer"]["arn"]] = (
        plan["ownership_tags"].pop(old_arn))
    plan.pop("plan_digest")
    plan["plan_digest"] = handoff.digest(plan)
    policy = handoff.cutover_role_policy(topology, plan)
    by_action = {}
    for statement in policy["Statement"]:
        actions = statement["Action"]
        for action in actions if isinstance(actions, list) else [actions]:
            by_action.setdefault(action, []).append(statement)

    allocation = "eipalloc-0123456789abcdef0"
    eip_arn = f"arn:aws:ec2:us-west-2:{ACCOUNT}:elastic-ip/{allocation}"
    eni_arn = (f"arn:aws:ec2:us-west-2:{ACCOUNT}:"
               "network-interface/eni-source")
    for action in ("ec2:AssociateAddress", "ec2:DisassociateAddress"):
        statement = by_action[action][0]
        th.assert_eq(statement["Resource"], [eip_arn, eni_arn],
                     f"{action} must name only the exact EIP and source ENI")
        th.assert_eq(statement["Condition"]["StringEquals"], {
            "ec2:AllocationId": allocation,
            "ec2:NetworkInterfaceID": "eni-source",
            "ec2:Region": "us-west-2",
        }, f"{action} must also bind the available EC2 IAM condition keys")
    set_subnets = by_action["elasticloadbalancing:SetSubnets"][0]
    th.assert_eq(set_subnets["Resource"], plan["load_balancer"]["arn"],
                 "SetSubnets must name only the immutable target NLB")
    th.assert_eq(set_subnets["Condition"], {
        "ForAllValues:StringEqualsIgnoreCase": {
            "elasticloadbalancing:Subnet": [
                "subnet-0123456789abcdef0", "subnet-1123456789abcdef0"]}},
        "SetSubnets IAM must reject every undeclared subnet")
    th.assert_eq(by_action["s3:PutObject"][0]["Resource"],
                 "arn:aws:s3:::maestro-prod-config/"
                 "fleets/shadow/handoff/*",
                 "journal writes must stay in the exact handoff prefix")
    th.assert_eq("ec2:ReleaseAddress" in by_action, False,
                 "the dedicated role must never be able to release an EIP")
    mutation_actions = (
        "ec2:AssociateAddress", "ec2:DisassociateAddress",
        "elasticloadbalancing:SetSubnets", "s3:PutObject")
    wildcard_mutations = [
        action for action in mutation_actions
        if any(row["Resource"] == "*" for row in by_action[action])]
    th.assert_eq(wildcard_mutations, [],
                 f"no cutover mutation may use Resource '*': {wildcard_mutations}")


@th.django_unit_test()
def test_confirmation_binds_action_environment_allocations_and_digest(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    plan = _plan(topology)
    phrase = handoff.expected_confirmation(plan, "HANDOFF")
    for value in ("HANDOFF", "prod", "eipalloc-0123456789abcdef0",
                  plan["plan_digest"]):
        th.assert_in(value, phrase,
                     f"live confirmation must bind {value!r}: {phrase}")
    rollback = handoff.expected_confirmation(
        plan, "ROLLBACK", "op-confirm")
    th.assert_eq(rollback,
                 f"ROLLBACK op-confirm {plan['plan_digest']}",
                 "rollback confirmation must bind operation and plan")


@th.django_unit_test()
def test_operation_id_paths_reject_traversal_controls_and_oversize(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology(project_root=tempfile.mkdtemp(
        prefix="testit_handoff_path."))
    for value in ("", "../escape", "folder/name", "folder\\name",
                  ".hidden", "op..escape", "op\nnewline", "x" * 129):
        refused = None
        try:
            handoff.journal_coordinates(topology, value)
        except handoff.HandoffRefused as err:
            refused = err
        th.assert_true(refused is not None,
                       f"unsafe operation id must be rejected: {value!r}")
    coordinates = handoff.journal_coordinates(topology, "op-safe_1.2")
    local_root = os.path.dirname(os.path.abspath(
        topology.eip_handoff_local_journal))
    th.assert_eq(os.path.commonpath(
        [local_root, os.path.abspath(coordinates["local_journal"])]),
        local_root, f"local journal must remain contained: {coordinates}")
    prefix = topology.eip_handoff_prefix.rstrip("/") + "/"
    th.assert_true(coordinates["journal_key"].startswith(prefix),
                   f"S3 journal must remain in exact prefix: {coordinates}")


@th.django_unit_test()
def test_terminal_lock_is_cas_reusable_but_active_lock_conflicts(opts):
    from mojo.deploy.provision import handoff

    root = tempfile.mkdtemp(prefix="testit_handoff_lock.")
    topology = handoff_topology(project_root=root)
    plan = _plan(topology)
    journal = handoff._new_journal("op-one", plan, "rehearsal")
    s3 = _S3()
    store = handoff.JournalStore(_clients(topology, s3=s3), topology, "op-one")
    store.verify_bucket()
    store.prepare_local(journal)
    store.acquire_lock(plan["plan_digest"], journal)
    store.start(journal)
    store.finish_lock("rehearsed")

    changed = json.loads(json.dumps(plan))
    changed["interruption"] = "changed after rehearsal"
    changed.pop("plan_digest")
    changed["plan_digest"] = handoff.digest(changed)
    changed_operation = "op-changed-handoff"
    changed_conflict = None
    try:
        changed_store = handoff.JournalStore(
            _clients(topology, s3=s3), topology, changed_operation)
        changed_store.acquire_lock(
            changed["plan_digest"], handoff._new_journal(
                changed_operation, changed, "handoff"))
    except handoff.HandoffConflict as err:
        changed_conflict = err
    th.assert_true(changed_conflict is not None,
                   "changed plan must be re-rehearsed before live handoff")

    next_journal = handoff._new_journal("op-two", plan, "handoff")
    second = handoff.JournalStore(
        _clients(topology, s3=s3), topology, "op-two")
    second.acquire_lock(plan["plan_digest"], next_journal)
    th.assert_true(second.lock_etag is not None,
                   "a terminal rehearsal lock must CAS into a live handoff")
    conflict = None
    try:
        third = handoff.JournalStore(
            _clients(topology, s3=s3), topology, "op-three")
        third.acquire_lock(plan["plan_digest"], next_journal)
    except handoff.HandoffConflict as err:
        conflict = err
    th.assert_true(conflict is not None,
                   "an active operation must remain an exclusive remote lock")


@th.django_unit_test()
def test_terminal_operation_id_reuse_is_rejected_without_lock_change(opts):
    from mojo.deploy.provision import handoff

    root = tempfile.mkdtemp(prefix="testit_handoff_reuse.")
    topology = handoff_topology(project_root=root)
    plan = _plan(topology)
    for terminal in ("rehearsed", "complete"):
        s3 = _S3()
        operation = f"op-{terminal}"
        kind = "rehearsal" if terminal == "rehearsed" else "handoff"
        if terminal == "complete":
            _prime_rehearsal(s3, topology, plan)
        journal = handoff._new_journal(operation, plan, kind)
        store = handoff.JournalStore(
            _clients(topology, s3=s3), topology, operation)
        store.acquire_lock(plan["plan_digest"], journal)
        store.start(journal)
        store.finish_lock(terminal)
        before = s3.objects[store.lock_key]
        conflict = None
        try:
            reused = handoff.JournalStore(
                _clients(topology, s3=s3), topology, operation)
            reused.acquire_lock(plan["plan_digest"], journal)
        except handoff.HandoffConflict as err:
            conflict = err
        th.assert_true(conflict is not None,
                       f"{terminal} operation id must never be reused")
        th.assert_eq(s3.objects[store.lock_key], before,
                     "reuse refusal must not CAS the terminal lock active")


@th.django_unit_test()
def test_losing_contender_cannot_overwrite_active_local_intent(opts):
    from mojo.deploy.provision import handoff

    root = tempfile.mkdtemp(prefix="testit_handoff_local_race.")
    topology = handoff_topology(project_root=root)
    plan = _plan(topology)
    s3 = _S3()
    active_journal = handoff._new_journal("op-active", plan, "rehearsal")
    active = handoff.JournalStore(
        _clients(topology, s3=s3), topology, "op-active")
    active.acquire_lock(plan["plan_digest"], active_journal)
    active.prepare_local(active_journal)
    with open(active.local_path) as handle:
        before = handle.read()

    contender = handoff.JournalStore(
        _clients(topology, s3=s3), topology, "op-loser")
    loser_journal = handoff._new_journal("op-loser", plan, "rehearsal")
    conflict = None
    try:
        contender.acquire_lock(plan["plan_digest"], loser_journal)
    except handoff.HandoffConflict as err:
        conflict = err
    th.assert_true(conflict is not None,
                   "the fleet lock must reject the losing contender")
    th.assert_true(active.local_path != contender.local_path,
                   "local write-ahead journals must be operation-specific")
    with open(active.local_path) as handle:
        th.assert_eq(handle.read(), before,
                     "losing operation must not alter active local intent")


@th.django_unit_test()
def test_fleet_lock_blocks_overlapping_allocation_subsets(opts):
    from mojo.deploy.provision import handoff

    root = tempfile.mkdtemp(prefix="testit_handoff_overlap.")
    one = handoff_topology(single=True, project_root=root)
    two = handoff_topology(single=False, project_root=root)
    s3 = _S3()
    plan_one = _plan(one)
    first = handoff.JournalStore(_clients(one, s3=s3), one, "op-single")
    first.acquire_lock(
        plan_one["plan_digest"],
        handoff._new_journal("op-single", plan_one, "rehearsal"))
    plan_two = _plan(two)
    second = handoff.JournalStore(
        _clients(two, s3=s3), two, "op-overlap")
    conflict = None
    try:
        second.acquire_lock(
            plan_two["plan_digest"],
            handoff._new_journal("op-overlap", plan_two, "rehearsal"))
    except handoff.HandoffConflict as err:
        conflict = err
    th.assert_true(conflict is not None,
                   "{A} and {A,B} must contend on the same fleet lock")
    th.assert_eq(first.lock_key, second.lock_key,
                 "allocation subsets must not choose distinct lock objects")


@th.django_unit_test()
def test_handoff_resume_cannot_consume_a_rollback_direction_lock(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology(project_root=tempfile.mkdtemp(
        prefix="testit_handoff_direction."))
    plan = _plan(topology)
    journal = handoff._new_journal("op-direction", plan, "handoff")
    s3 = _S3()
    _prime_rehearsal(s3, topology, plan)
    store = handoff.JournalStore(
        _clients(topology, s3=s3), topology, "op-direction")
    store.acquire_lock(plan["plan_digest"], journal)
    store.start(journal)
    store.reopen_for_rollback(plan["plan_digest"])
    refused = None
    try:
        contender = handoff.JournalStore(
            _clients(topology, s3=s3), topology, "op-direction")
        contender.bind_lock(plan["plan_digest"], expected_action="handoff")
    except handoff.HandoffConflict as err:
        refused = err
    th.assert_true(refused is not None,
                   "handoff resume must not reverse an in-progress rollback")


@th.django_unit_test()
def test_lock_carries_recovery_seed_before_remote_journal_exists(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology(project_root=tempfile.mkdtemp(
        prefix="testit_handoff_seed."))
    plan = _plan(topology)
    journal = handoff._new_journal("op-seed", plan, "handoff")
    s3 = _S3()
    _prime_rehearsal(s3, topology, plan)
    store = handoff.JournalStore(_clients(topology, s3=s3), topology, "op-seed")
    store.prepare_local(journal)
    store.acquire_lock(plan["plan_digest"], journal)
    lock = json.loads(s3.objects[store.lock_key][0])
    th.assert_eq(lock["journal_seed"]["operation_id"], "op-seed",
                 "the active lock must be sufficient to recreate the journal")
    th.assert_eq(store.journal_key in s3.objects, False,
                 "this assertion pins the crash window before remote journal create")


@th.django_unit_test()
def test_bucket_controls_and_journal_etag_conflicts_fail_closed(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology(project_root=tempfile.mkdtemp(
        prefix="testit_handoff_bucket."))
    plan = _plan(topology)

    class _UnsafeBucket(_S3):
        def __init__(self, versioned=True, encrypted=True):
            super().__init__()
            self.versioned = versioned
            self.encrypted = encrypted

        def get_bucket_versioning(self, **kwargs):
            return {"Status": "Enabled" if self.versioned else "Suspended"}

        def get_bucket_encryption(self, **kwargs):
            if not self.encrypted:
                return {"ServerSideEncryptionConfiguration": {"Rules": []}}
            return super().get_bucket_encryption(**kwargs)

    for label, s3 in (("versioning", _UnsafeBucket(versioned=False)),
                      ("encryption", _UnsafeBucket(encrypted=False))):
        refused = None
        try:
            handoff.JournalStore(
                _clients(topology, s3=s3), topology, f"op-{label}").verify_bucket()
        except handoff.HandoffRefused as err:
            refused = err
        th.assert_true(refused is not None,
                       f"missing bucket {label} must block before mutation")
        th.assert_eq(s3.calls, [],
                     f"unsafe bucket {label} must have no journal writes")

    s3 = _UnsafeBucket()
    store = handoff.JournalStore(
        _clients(topology, s3=s3), topology, "op-etag")
    journal = handoff._new_journal("op-etag", plan, "rehearsal")
    store.prepare_local(journal)
    store.acquire_lock(plan["plan_digest"], journal)
    store.start(journal)
    body, ignored = s3.objects[store.journal_key]
    s3.objects[store.journal_key] = (body, "concurrent-etag")
    conflict = None
    try:
        store.advance(journal)
    except handoff.HandoffConflict as err:
        conflict = err
    th.assert_true(conflict is not None,
                   "a concurrent journal writer must fail the next CAS")


@th.django_unit_test()
def test_local_and_remote_journal_write_failures_precede_provider_mutation(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import handoff

    topology = handoff_topology(project_root=tempfile.mkdtemp(
        prefix="testit_handoff_write_failure."))
    plan = _plan(topology)
    journal = handoff._new_journal("op-write", plan, "rehearsal")

    class _NoLocal(handoff.JournalStore):
        def _write_local(self, journal):
            raise OSError("disk full")

    s3 = _S3()
    local = _NoLocal(_clients(topology, s3=s3), topology, "op-write")
    raised = None
    try:
        local.prepare_local(journal)
    except OSError as err:
        raised = err
    th.assert_true(raised is not None,
                   "local durable intent failure must abort immediately")
    th.assert_eq(s3.calls, [],
                 "local intent failure must precede every remote write")

    class _NoRemote(_S3):
        def put_object(self, **kwargs):
            self.calls.append((kwargs["Key"], dict(kwargs)))
            raise ClientError({"Error": {"Code": "AccessDenied",
                                          "Message": "denied"}}, "PutObject")

    remote = _NoRemote()
    store = handoff.JournalStore(
        _clients(topology, s3=remote), topology, "op-write")
    store.prepare_local(journal)
    refused = None
    try:
        store.acquire_lock(plan["plan_digest"], journal)
    except handoff.HandoffConflict as err:
        refused = err
    th.assert_true(refused is not None,
                   "remote lock write failure must abort before handoff")
    th.assert_eq(len(remote.calls), 1,
                 "remote failure must not continue into journal/provider writes")


@th.django_unit_test()
def test_rehearsal_write_failures_terminalize_and_future_handoff_recovers(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import handoff

    root = tempfile.mkdtemp(prefix="testit_handoff_rehearsal_failure.")
    topology = handoff_topology(project_root=root)
    plan = _plan(topology)

    class _FailJournalPut(_S3):
        def put_object(self, **kwargs):
            if "/operations/" in kwargs["Key"]:
                raise ClientError({"Error": {"Code": "AccessDenied",
                                              "Message": "denied"}},
                                  "PutObject")
            return super().put_object(**kwargs)

    for label, s3, local_failure in (
            ("local", _S3(), True),
            ("remote", _FailJournalPut(), False)):
        operation = f"op-rehearsal-{label}"
        connection = _clients(topology, s3=s3)
        call = handoff.rehearse
        patches = [mock.patch.object(handoff, "build_plan", return_value=plan)]
        if local_failure:
            patches.append(mock.patch.object(
                handoff.JournalStore, "_write_local",
                side_effect=OSError("disk full")))
        for patcher in patches:
            patcher.start()
        try:
            raised = None
            try:
                call(connection, topology, plan, plan["plan_digest"],
                     handoff.expected_confirmation(plan, "REHEARSE"),
                     operation_id=operation)
            except Exception as err:
                raised = err
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        th.assert_true(raised is not None,
                       f"{label} rehearsal write must report its failure")
        store = handoff.JournalStore(connection, topology, operation)
        lock = json.loads(s3.objects[store.lock_key][0])
        th.assert_eq(lock["state"], "rehearsal_failed",
                     f"{label} failure must not orphan an active lock")
        denied_operation = f"op-denied-handoff-after-{label}"
        denied = handoff.JournalStore(
            connection, topology, denied_operation)
        conflict = None
        try:
            denied.acquire_lock(
                plan["plan_digest"], handoff._new_journal(
                    denied_operation, plan, "handoff"))
        except handoff.HandoffConflict as err:
            conflict = err
        th.assert_true(conflict is not None,
                       "failed rehearsal must never authorize live handoff")

        retry_plan = json.loads(json.dumps(plan))
        retry_plan["interruption"] = f"fixed after {label}"
        retry_plan.pop("plan_digest")
        retry_plan["plan_digest"] = handoff.digest(retry_plan)
        retry_operation = f"op-rehearsal-retry-{label}"
        retry_journal = handoff._new_journal(
            retry_operation, retry_plan, "rehearsal")
        recovered = handoff.JournalStore(
            connection, topology, retry_operation)
        recovered.acquire_lock(retry_plan["plan_digest"], retry_journal)
        th.assert_eq(recovered.lock_body["state"], "active",
                     f"new-plan rehearsal must recover after {label} failure")
        recovered.finish_lock("rehearsed")
        handoff_operation = f"op-handoff-after-{label}"
        handoff_store = handoff.JournalStore(
            connection, topology, handoff_operation)
        handoff_store.acquire_lock(
            retry_plan["plan_digest"], handoff._new_journal(
                handoff_operation, retry_plan, "handoff"))
        th.assert_eq(handoff_store.lock_body["action"], "handoff",
                     "successful same-plan retry must authorize handoff")

    # An active lock is never silently stolen, even though rehearsal contains
    # no provider mutation. Explicit operator recovery is required after a hard
    # process death so the one-operator invariant stays auditable.
    s3 = _S3()
    connection = _clients(topology, s3=s3)
    crashed = handoff.JournalStore(
        connection, topology, "op-rehearsal-crashed")
    crashed_seed = handoff._new_journal(
        "op-rehearsal-crashed", plan, "rehearsal")
    crashed.acquire_lock(plan["plan_digest"], crashed_seed)
    next_seed = handoff._new_journal(
        "op-after-crashed-rehearsal", plan, "rehearsal")
    recovered = handoff.JournalStore(
        connection, topology, "op-after-crashed-rehearsal")
    conflict = None
    try:
        recovered.acquire_lock(plan["plan_digest"], next_seed)
    except handoff.HandoffConflict as err:
        conflict = err
    th.assert_true(conflict is not None,
                   "a new operation must never steal an active rehearsal lock")


@th.django_unit_test()
def test_write_ahead_advances_top_level_map_for_crash_resume(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    plan = _plan(topology)
    journal = handoff._new_journal("op-map", plan, "handoff")
    prior = dict(journal["expected_map"])
    after = dict(prior)
    after.pop("us-west-2a")
    store = _CaptureStore()
    handoff._record(journal, store, "before_remove_subnet", "remove one",
                    az="us-west-2a", expected_map=prior, next_map=after)
    th.assert_eq(store.snapshots[-1]["expected_map"], after,
                 "the provider-after map must be durable before SetSubnets")
    th.assert_eq(store.snapshots[-1]["prior_map"], prior,
                 "the provider-before map must remain resumable if no call ran")


@th.django_unit_test()
def test_nlb_target_state_requires_service_managed_eni_shape(opts):
    from mojo.deploy.provision import handoff

    source = _source()
    mapping = _mapping("us-west-2a", source["target_subnet_id"],
                       source["allocation_id"], source["public_ip"],
                       "172.31.0.20")
    address = {"AllocationId": source["allocation_id"],
               "AssociationId": "eipassoc-elb", "NetworkInterfaceId": "eni-elb",
               "PrivateIpAddress": "172.31.0.20"}
    provider = {"map": {"us-west-2a": mapping},
                "addresses": {source["allocation_id"]: address},
                "network_interfaces": {"eni-elb": {
                    "RequesterManaged": True,
                    "InterfaceType": "network_load_balancer",
                    "SubnetId": source["target_subnet_id"],
                    "VpcId": source["source_vpc_id"],
                    "AvailabilityZone": source["availability_zone"]}}}
    th.assert_eq(handoff.derive_state(
        source, provider, provider["map"], "handoff"), "target",
        "an NLB target EIP is associated to its service-managed ENI")
    provider["network_interfaces"]["eni-elb"]["RequesterManaged"] = False
    th.assert_eq(handoff.derive_state(
        source, provider, provider["map"], "handoff"), "unexpected",
        "an arbitrary ENI association must never masquerade as the NLB target")
    provider["network_interfaces"]["eni-elb"]["RequesterManaged"] = True
    provider["map"]["us-west-2a"]["subnet_id"] = "subnet-wrong"
    provider["network_interfaces"]["eni-elb"]["SubnetId"] = "subnet-wrong"
    th.assert_eq(handoff.derive_state(
        source, provider, provider["map"], "handoff"), "unexpected",
        "a service ENI in the wrong same-AZ subnet must never pass")
    provider["addresses"][source["allocation_id"]] = {
        "AllocationId": source["allocation_id"],
        "AssociationId": "eipassoc-restored",
        "NetworkInterfaceId": source["network_interface_id"],
        "PrivateIpAddress": source["private_ip"],
        "InstanceId": source["instance_id"]}
    provider["map"]["us-west-2a"]["allocation_id"] = None
    th.assert_eq(handoff.derive_state(
        source, provider, provider["map"], "rollback"), "unexpected",
        "rollback prepared state must bind the exact target subnet too")


@th.django_unit_test()
def test_target_gate_rejects_wrong_ports_duplicates_and_extras(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()

    class _Instances:
        def describe_instances(self, **kwargs):
            names = ("maestro-api-1", "maestro-api-2")
            rows = []
            for index, name in enumerate(names, 1):
                rows.append({"InstanceId": f"i-api-{index}", "Tags": [
                    {"Key": "Name", "Value": name}]})
            return {"Reservations": [{"Instances": rows}]}

    expected_api = [
        {"id": "i-api-1", "port": 443},
        {"id": "i-api-2", "port": 443},
        {"id": "i-0123456789abcdef0", "port": 443},
    ]
    clean = {"api": expected_api,
             "certbot": [{"id": "i-api-1", "port": 80}]}
    problems = []
    handoff._validate_targets(topology, _Instances(), clean, problems)
    th.assert_eq(problems, [], f"the exact id/port multiset must pass: {problems}")
    for label, api in (
            ("wrong port", [dict(expected_api[0], port=80)] + expected_api[1:]),
            ("duplicate", expected_api + [expected_api[0]]),
            ("extra", expected_api + [{"id": "i-extra", "port": 443}])):
        problems = []
        handoff._validate_targets(
            topology, _Instances(),
            {"api": api, "certbot": clean["certbot"]}, problems)
        th.assert_true(problems,
                       f"the exact target multiset must reject {label}")


@th.django_unit_test()
def test_listener_and_target_group_shape_gates_fail_closed(opts):
    from mojo.deploy.provision import balancer, handoff

    topology = handoff_topology()
    specs = balancer.target_group_specs(
        topology, topology.brownfield_manifest["network"]["vpc_id"])
    groups = {}
    for role, spec in specs.items():
        groups[role] = dict(spec, TargetGroupArn=f"arn:tg:{role}")
    listeners = [
        {"Port": 443, "Protocol": "TCP", "DefaultActions": [{
            "Type": "forward", "TargetGroupArn": "arn:tg:api"}]},
        {"Port": 80, "Protocol": "TCP", "DefaultActions": [{
            "Type": "forward", "TargetGroupArn": "arn:tg:certbot"}]},
    ]
    problems = []
    handoff._validate_edge(topology, groups, listeners, problems)
    th.assert_eq(problems, [], f"exact listener/TG shapes must pass: {problems}")
    bad_groups = {key: dict(value) for key, value in groups.items()}
    bad_groups["api"]["Port"] = 80
    problems = []
    handoff._validate_edge(topology, bad_groups, listeners, problems)
    th.assert_true(problems, "wrong target-group port must block handoff")
    bad_listeners = [dict(listeners[0], Protocol="TLS"), listeners[1]]
    problems = []
    handoff._validate_edge(topology, groups, bad_listeners, problems)
    th.assert_true(problems, "wrong listener protocol must block handoff")


@th.django_unit_test()
def test_owned_edge_tags_block_same_name_collision_and_runtime_drift(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    requested = ["arn:lb:shadow", "arn:tg:api", "arn:tg:certbot"]

    class _Tags:
        def __init__(self, managed_by):
            self.managed_by = managed_by

        def describe_tags(self, ResourceArns):
            th.assert_eq(ResourceArns, requested,
                         "ownership read must bind the exact NLB and TG ARNs")
            values = {
                "managed-by": self.managed_by, "mojo:project": "maestro",
                "mojo:env": "prod", "mojo:fleet": "shadow",
                "mojo:role": "balancer"}
            return {"TagDescriptions": [{
                "ResourceArn": arn,
                "Tags": [{"Key": key, "Value": value}
                         for key, value in values.items()]}
                for arn in ResourceArns]}

    problems = []
    handoff._owned_edge_tags(
        _Tags("someone-else"), topology, requested, problems=problems)
    th.assert_eq(len(problems), 3,
                 f"every unowned same-name edge resource must fail: {problems}")
    problems = []
    summaries = handoff._owned_edge_tags(
        _Tags("django-mojo"), topology, requested, problems=problems)
    th.assert_eq(problems, [], f"exact owned tags must pass: {problems}")
    th.assert_eq(sorted(summaries), requested,
                 "ownership summaries bound into the digest must cover all ARNs")


@th.django_unit_test()
def test_free_state_can_attach_target_and_roll_back_without_schema_keyerror(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    remaining = {"us-west-2b": _mapping(
        "us-west-2b", "subnet-1123456789abcdef0", None, "198.51.100.11")}
    plan = _plan(topology, mapping=remaining)

    ec2 = _EC2(source_state="free")
    elb = _ELB(ec2, remaining)
    connection = _clients(topology, ec2=ec2, elb=elb)
    handoff._bind_boundary_from_plan(connection, plan)
    journal = handoff._new_journal("op-free", plan, "handoff")
    store = _CaptureStore()
    handoff._transfer(
        connection, topology, journal, store,
        canary_runner=lambda definition, address: {"ok": True},
        sleeper=lambda seconds: None, timeout=1)
    attached = [row for call in elb.set_calls for row in call
                if row.get("AllocationId")]
    th.assert_eq(attached[0]["SubnetId"], _source()["target_subnet_id"],
                 f"free-state attach must use target_subnet_id: {attached}")

    ec2.source_state = "free"
    ec2.target = False
    elb.mapping = dict(remaining)
    rollback_plan = _plan(topology, mapping=remaining)
    rollback_journal = handoff._new_journal(
        "op-free-rollback", rollback_plan, "handoff")
    handoff._restore(connection, topology, rollback_journal, _CaptureStore(),
                     sleeper=lambda seconds: None, timeout=1)
    th.assert_eq(ec2.associate_calls[-1]["NetworkInterfaceId"], "eni-source",
                 "rollback from free must restore the exact source ENI")
    th.assert_true(any(row["SubnetId"] == _source()["target_subnet_id"]
                       and not row.get("AllocationId")
                       for row in elb.set_calls[-1]),
                   f"rollback must restore a temporary same-subnet map: {elb.set_calls}")


@th.django_unit_test()
def test_full_source_remove_disassociate_and_target_mapping_succeeds(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    mapping = {
        "us-west-2a": _mapping(
            "us-west-2a", "subnet-0123456789abcdef0", None,
            "198.51.100.10"),
        "us-west-2b": _mapping(
            "us-west-2b", "subnet-1123456789abcdef0", None,
            "198.51.100.11"),
    }
    plan = _plan(topology, mapping=mapping)
    ec2 = _EC2(source_state="source")
    elb = _ELB(ec2, mapping)
    connection = _clients(topology, ec2=ec2, elb=elb)
    handoff._bind_boundary_from_plan(connection, plan)
    journal = handoff._new_journal("op-full", plan, "handoff")
    store = _CaptureStore()
    handoff._transfer(
        connection, topology, journal, store,
        canary_runner=lambda definition, address: {"ok": True},
        sleeper=lambda seconds: None, timeout=1)
    th.assert_eq(ec2.disassociate_calls,
                 [{"AssociationId": "eipassoc-old"}],
                 "handoff must free only the exact recorded association")
    th.assert_eq(len(elb.set_calls), 2,
                 f"handoff must remove then re-add exactly one AZ: {elb.set_calls}")
    th.assert_eq([row["SubnetId"] for row in elb.set_calls[0]],
                 ["subnet-1123456789abcdef0"],
                 "the first full-map CAS must retain another AZ")
    th.assert_true(any(row.get("AllocationId") ==
                       "eipalloc-0123456789abcdef0"
                       for row in elb.set_calls[1]),
                   f"the second full-map CAS must attach the preserved EIP: "
                   f"{elb.set_calls}")
    states = [row["state"] for row in journal["transitions"]]
    for state in ("before_remove_subnet", "subnet_removed_source",
                  "before_disassociate", "free", "before_target_mapping",
                  "target"):
        th.assert_in(state, states,
                     f"the durable state machine must record {state}: {states}")


@th.django_unit_test()
def test_multi_address_order_canary_stop_and_exact_reverse_rollback(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology(single=False)
    plan = _two_source_plan(topology)
    ec2 = _MultiEC2(plan["sources"])
    elb = _MultiELB(ec2, plan["load_balancer"]["map"])
    connection = _clients(topology, ec2=ec2, elb=elb)
    handoff._bind_boundary_from_plan(connection, plan)
    journal = handoff._new_journal("op-two", plan, "handoff")
    failures = []

    def fail_first(definition, address):
        failures.append(address)
        return {"ok": False}

    raised = None
    try:
        handoff._transfer(
            connection, topology, journal, _CaptureStore(),
            canary_runner=fail_first, sleeper=lambda seconds: None, timeout=1)
    except handoff.HandoffRefused as err:
        raised = err
    th.assert_true(raised is not None,
                   "a post-address canary failure must stop the transfer")
    th.assert_eq(ec2.disassociate_calls, ["us-west-2a"],
                 "canary failure after A must stop before touching B")

    # Resume the exact partial state with a green address canary.
    handoff._transfer(
        connection, topology, journal, _CaptureStore(),
        canary_runner=lambda definition, address: {"ok": True},
        sleeper=lambda seconds: None, timeout=1)
    th.assert_eq(ec2.disassociate_calls, ["us-west-2a", "us-west-2b"],
                 "multi-address handoff must advance in deterministic AZ order")
    set_calls_after_handoff = len(elb.set_calls)
    handoff._restore(
        connection, topology, journal, _CaptureStore(),
        sleeper=lambda seconds: None, timeout=1)
    th.assert_eq(ec2.associate_calls, ["us-west-2b", "us-west-2a"],
                 "rollback must restore sources in exact reverse AZ order")
    th.assert_eq(set(ec2.states.values()), {"source"},
                 f"rollback must restore both exact original sources: {ec2.states}")
    th.assert_true(len(elb.set_calls) > set_calls_after_handoff,
                   "reverse rollback must remove target mappings before restore")


@th.django_unit_test()
def test_partial_subnet_removed_and_completed_target_states_resume_idempotently(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    source = _source()
    reduced = {"us-west-2b": _mapping(
        "us-west-2b", "subnet-1123456789abcdef0", None, "198.51.100.11")}
    for initial_state in ("source", "free"):
        plan = _plan(topology, mapping=reduced)
        ec2 = _MultiEC2({"us-west-2a": source},
                        states={"us-west-2a": initial_state})
        elb = _MultiELB(ec2, reduced)
        connection = _clients(topology, ec2=ec2, elb=elb)
        handoff._bind_boundary_from_plan(connection, plan)
        journal = handoff._new_journal(
            f"op-partial-{initial_state}", plan, "handoff")
        handoff._transfer(
            connection, topology, journal, _CaptureStore(),
            canary_runner=lambda definition, address: {"ok": True},
            sleeper=lambda seconds: None, timeout=1)
        th.assert_eq(ec2.states["us-west-2a"], "target",
                     f"{initial_state} partial state must resume to target")

    target_map = dict(reduced)
    target_map["us-west-2a"] = _mapping(
        "us-west-2a", source["target_subnet_id"], source["allocation_id"],
        source["public_ip"], "172.31.0.20")
    plan = _plan(topology, mapping=target_map)
    ec2 = _MultiEC2({"us-west-2a": source}, states={"us-west-2a": "target"})
    elb = _MultiELB(ec2, target_map)
    connection = _clients(topology, ec2=ec2, elb=elb)
    handoff._bind_boundary_from_plan(connection, plan)
    journal = handoff._new_journal("op-complete", plan, "handoff")
    handoff._transfer(
        connection, topology, journal, _CaptureStore(),
        canary_runner=lambda definition, address: {"ok": True},
        sleeper=lambda seconds: None, timeout=1)
    th.assert_eq(elb.set_calls, [],
                 "an already completed provider target must be idempotent")
    th.assert_eq(ec2.disassociate_calls, [],
                 "completed resume must never re-disassociate an EIP")


@th.django_unit_test()
def test_rollback_stops_ambiguous_association_before_setsubnets(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    mapping = {"us-west-2a": _mapping(
        "us-west-2a", "subnet-0123456789abcdef0", None, "198.51.100.10"),
        "us-west-2b": _mapping(
            "us-west-2b", "subnet-1123456789abcdef0", None, "198.51.100.11")}
    plan = _plan(topology, mapping=mapping)
    ec2 = _EC2(source_state="unexpected")
    elb = _ELB(ec2, mapping)
    connection = _clients(topology, ec2=ec2, elb=elb)
    handoff._bind_boundary_from_plan(connection, plan)
    journal = handoff._new_journal("op-ambiguous", plan, "handoff")
    raised = None
    try:
        handoff._restore(connection, topology, journal, _CaptureStore(),
                         sleeper=lambda seconds: None, timeout=1)
    except handoff.UnexpectedState as err:
        raised = err
    th.assert_true(raised is not None,
                   "an unknown association must stop rollback")
    th.assert_eq(elb.set_calls, [],
                 "rollback must make no NLB mutation from ambiguous state")


@th.django_unit_test()
def test_rollback_runtime_gate_ignores_failed_replacement_canary(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    mapping = {"us-west-2a": _mapping(
        "us-west-2a", "subnet-0123456789abcdef0", None, "198.51.100.10"),
        "us-west-2b": _mapping(
            "us-west-2b", "subnet-1123456789abcdef0", None, "198.51.100.11")}
    plan = _plan(topology, mapping=mapping)
    ec2 = _EC2(source_state="source")
    elb = _ELB(ec2, mapping)
    connection = _clients(topology, ec2=ec2, elb=elb)
    handoff._bind_boundary_from_plan(connection, plan)
    journal = handoff._new_journal("op-unhealthy", plan, "handoff")
    calls = []
    result = handoff.revalidate_runtime(
        connection, topology, plan, journal,
        canary_runner=lambda definition, address: calls.append(address),
        direction="rollback")
    th.assert_true(result["map"],
                   "exact source restorability should keep rollback available")
    th.assert_eq(calls, [],
                 "replacement canaries must not veto an emergency rollback")


@th.django_unit_test()
def test_journal_schema_requires_one_consistent_target_subnet_key(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology()
    journal = handoff._new_journal("op-schema", _plan(topology), "handoff")
    journal["sources"]["us-west-2a"].pop("target_subnet_id")
    journal["sources"]["us-west-2a"]["subnet_id"] = "ambiguous"
    raised = None
    try:
        handoff._validate_journal(journal)
    except handoff.HandoffRefused as err:
        raised = err
    th.assert_true(raised is not None,
                   "old/ambiguous source schema must fail before recovery")


@th.django_unit_test()
def test_journal_tamper_and_cross_operation_copies_fail_before_provider(opts):
    from mojo.deploy.provision import handoff

    topology = handoff_topology(project_root=tempfile.mkdtemp(
        prefix="testit_handoff_tamper."))
    plan = _plan(topology)
    for label, mutate in (
            ("map digest", lambda row: row["expected_map"].pop("us-west-2b")),
            ("pending intent", lambda row: row.update({
                "state": "before_remove_subnet",
                "pending_intent": {"state": "before_remove_subnet",
                                   "sequence": 99},
                "prior_map": row["expected_map"],
                "prior_map_digest": handoff.digest(row["expected_map"])}))):
        journal = handoff._new_journal("op-tamper", plan, "handoff")
        mutate(journal)
        refused = None
        try:
            handoff._validate_journal(journal)
        except handoff.HandoffRefused as err:
            refused = err
        th.assert_true(refused is not None,
                       f"{label} tamper must fail schema validation")

    journal = handoff._new_journal("op-source-tamper", plan, "handoff")
    journal["sources"]["us-west-2a"]["private_ip"] = "172.31.99.99"
    refused = None
    try:
        handoff._journal_matches(journal, plan)
    except handoff.HandoffRefused as err:
        refused = err
    th.assert_true(refused is not None,
                   "source/inverse identity tamper must not match the plan")

    s3 = _S3()
    operation = "op-original"
    original = handoff.JournalStore(
        _clients(topology, s3=s3), topology, operation)
    body = handoff._new_journal(operation, plan, "rehearsal")
    original.acquire_lock(plan["plan_digest"], body)
    original.start(body)
    copied = handoff.JournalStore(
        _clients(topology, s3=s3), topology, "op-copied")
    s3.objects[copied.journal_key] = s3.objects[original.journal_key]
    refused = None
    try:
        copied.load()
    except handoff.HandoffRefused as err:
        refused = err
    th.assert_true(refused is not None,
                   "copied journal body must not cross operation coordinates")

    lock_body, lock_etag = s3.objects[original.lock_key]
    tampered_lock = json.loads(lock_body)
    tampered_lock["journal_key"] = copied.journal_key
    s3.objects[original.lock_key] = (
        handoff.canonical(tampered_lock).encode("utf-8"), lock_etag)
    conflict = None
    try:
        rebound = handoff.JournalStore(
            _clients(topology, s3=s3), topology, operation)
        rebound.bind_lock(plan["plan_digest"], expected_action="rehearsal")
    except handoff.HandoffConflict as err:
        conflict = err
    th.assert_true(conflict is not None,
                   "lock journal-key tamper must fail before provider reads")
