"""Audited, resumable transfer of exact legacy EIPs onto a prepared NLB.

This module is the one narrow exception to provision's create/modify-only
rule.  It is deliberately unreachable through ``discover.Clients``: callers
must assume the exact role committed in the fleet manifest and receive the
positive-allowlist clients below.  The role and this process both omit address
release, DNS, certificate, deletion and general provisioning authority.
"""

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import tempfile
import time
import uuid

from mojo.deploy.provision import balancer, brownfield_inputs


SCHEMA = "mojo-eip-handoff/v1"
LOCK_SCHEMA = "mojo-eip-handoff-lock/v1"
TERMINAL_STATES = frozenset((
    "complete", "rolled_back", "rehearsed", "rehearsal_failed"))
STATES = frozenset((
    "source", "subnet_removed_source", "free", "target",
    "rollback_subnet_removed", "source_restored", "prepared", "complete",
    "unexpected",
))
READ_METHODS = {
    "sts": frozenset(("get_caller_identity",)),
    "ec2": frozenset((
        "describe_addresses", "describe_instances",
        "describe_network_interfaces", "describe_subnets",
    )),
    "elbv2": frozenset((
        "describe_listeners", "describe_load_balancer_attributes",
        "describe_load_balancers", "describe_tags", "describe_target_groups",
        "describe_target_health",
    )),
    "s3": frozenset((
        "get_bucket_encryption", "get_bucket_versioning", "get_object",
        "head_object",
    )),
}
MUTATION_METHODS = {
    "ec2": frozenset(("associate_address", "disassociate_address")),
    "elbv2": frozenset(("set_subnets",)),
    "s3": frozenset(("put_object",)),
}
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class HandoffError(RuntimeError):
    pass


class HandoffRefused(HandoffError):
    pass


class HandoffConflict(HandoffError):
    pass


class UnexpectedState(HandoffError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def validate_operation_id(operation_id):
    if (not isinstance(operation_id, str)
            or not OPERATION_ID_RE.fullmatch(operation_id)
            or ".." in operation_id):
        raise HandoffRefused(
            "operation id must be 1-128 safe ASCII letters/digits/._- "
            "without traversal")
    return operation_id


def validate_topology(topology):
    if getattr(topology, "manage_dns", None) is not False:
        raise HandoffRefused(
            "preserved-address work requires serialized manage_dns=false")
    allocations = dict(getattr(topology, "nlb_eip_allocations", None) or {})
    if not allocations:
        raise HandoffRefused("the fleet declares no preserved EIP mappings")
    if not getattr(topology, "eip_handoff_role_arn", None):
        raise HandoffRefused("the fleet declares no dedicated handoff role")
    if not getattr(topology, "eip_handoff_canaries", None):
        raise HandoffRefused("the fleet declares no handoff canaries")
    if not any(row.get("target", "nlb") == "nlb"
               for row in topology.eip_handoff_canaries):
        raise HandoffRefused(
            "preserved mode needs an address-specific NLB canary")
    if not getattr(topology, "eip_handoff_bucket", None):
        raise HandoffRefused("the fleet declares no exact journal bucket")
    return allocations


class CutoverClient:
    """One service client constrained to exact methods and resources."""

    def __init__(self, raw, service, boundary):
        self._raw = raw
        self._service = service
        self._boundary = boundary

    def __getattr__(self, name):
        if (name not in READ_METHODS.get(self._service, ())
                and name not in MUTATION_METHODS.get(self._service, ())):
            raise HandoffRefused(
                f"{self._service}.{name} is outside the handoff allowlist")
        operation = getattr(self._raw, name)

        def call(*args, **kwargs):
            self._boundary.authorize(self._service, name, kwargs)
            return operation(*args, **kwargs)
        return call


class HandoffClients:
    """Separate client container; it cannot construct Route 53 or IAM."""

    def __init__(self, session=None, topology=None, **overrides):
        validate_topology(topology)
        self._session = session
        self._topology = topology
        self._overrides = dict(overrides)
        self._cache = {}
        self._boundary = _Boundary(topology)
        self.region_name = getattr(session, "region_name", topology.region)

    def get(self, service):
        if service not in set(READ_METHODS) | set(MUTATION_METHODS):
            raise HandoffRefused(
                f"service {service!r} is outside the handoff allowlist")
        if service in self._cache:
            return self._cache[service]
        raw = self._overrides.get(service)
        if raw is None:
            if self._session is None:
                raise HandoffRefused(
                    f"no test override or dedicated session for {service}")
            raw = self._session.client(service)
        wrapped = CutoverClient(raw, service, self._boundary)
        self._cache[service] = wrapped
        return wrapped


class _Boundary:
    def __init__(self, topology):
        self.account_id = str(topology.account_id)
        self.region = topology.region
        self.allocations = set(topology.nlb_eip_allocations.values())
        subnet_rows = {
            row["availability_zone"]: row
            for row in topology.brownfield_manifest["network"]["public_subnets"]}
        self.subnet_allocations = {
            subnet_rows[az]["id"]: allocation
            for az, allocation in topology.nlb_eip_allocations.items()}
        self.allowed_subnets = set(
            topology.brownfield_manifest["load_balancer"]["subnet_ids"])
        self.balancer_arn = None
        self.bucket = topology.eip_handoff_bucket
        self.prefix = topology.eip_handoff_prefix.rstrip("/") + "/"
        lock_name = f"{topology.project}-{topology.env}-{topology.fleet}"
        self.lock_relative_key = f"locks/{lock_name}.json"
        self.sources_by_association = {}
        self.sources_by_allocation = {}

    def bind(self, balancer_arn=None, sources=None):
        if balancer_arn:
            if self.balancer_arn and self.balancer_arn != balancer_arn:
                raise HandoffRefused("the cutover target NLB ARN changed")
            self.balancer_arn = balancer_arn
        for source in (sources or {}).values():
            association = source.get("association_id")
            allocation = source.get("allocation_id")
            if association:
                self.sources_by_association[association] = dict(source)
            if allocation:
                self.sources_by_allocation[allocation] = dict(source)

    def authorize(self, service, method, kwargs):
        if method == "describe_addresses" and kwargs.get("AllocationIds"):
            if not set(kwargs["AllocationIds"]).issubset(self.allocations):
                raise HandoffRefused("an unconfigured EIP was requested")
        if service == "elbv2" and method == "set_subnets":
            if kwargs.get("LoadBalancerArn") != self.balancer_arn:
                raise HandoffRefused("SetSubnets does not target the bound NLB")
            if set(kwargs) != {"LoadBalancerArn", "SubnetMappings"}:
                raise HandoffRefused("SetSubnets request shape is not exact")
            rows = kwargs.get("SubnetMappings")
            if not isinstance(rows, list) or not rows:
                raise HandoffRefused("SetSubnets requires a non-empty full map")
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or "SubnetId" not in row:
                    raise HandoffRefused("SetSubnets mapping is malformed")
                subnet_id = row["SubnetId"]
                if subnet_id not in self.allowed_subnets or subnet_id in seen:
                    raise HandoffRefused("SetSubnets contains an unknown/duplicate subnet")
                seen.add(subnet_id)
                allocation = row.get("AllocationId")
                if allocation and allocation != self.subnet_allocations.get(subnet_id):
                    raise HandoffRefused(
                        "SetSubnets contains an allocation outside its exact AZ")
        if service == "ec2" and method == "disassociate_address":
            if set(kwargs) != {"AssociationId"}:
                raise HandoffRefused("DisassociateAddress request shape is not exact")
            if kwargs.get("AssociationId") not in self.sources_by_association:
                raise HandoffRefused(
                    "DisassociateAddress does not target a bound association")
        if service == "ec2" and method == "associate_address":
            if set(kwargs) != {"AllocationId", "NetworkInterfaceId",
                               "PrivateIpAddress", "AllowReassociation"}:
                raise HandoffRefused("AssociateAddress request shape is not exact")
            source = self.sources_by_allocation.get(kwargs.get("AllocationId"))
            if not source:
                raise HandoffRefused("AssociateAddress targets an unknown EIP")
            if (kwargs.get("NetworkInterfaceId") != source.get("network_interface_id")
                    or kwargs.get("PrivateIpAddress") != source.get("private_ip")):
                raise HandoffRefused("AssociateAddress does not restore the exact source")
            if kwargs.get("AllowReassociation") is not False:
                raise HandoffRefused(
                    "source restoration requires AllowReassociation=False")
        if service == "s3":
            if kwargs.get("Bucket") != self.bucket:
                raise HandoffRefused("journal access targets another bucket")
            key = kwargs.get("Key")
            if key is not None and not key.startswith(self.prefix):
                raise HandoffRefused("journal access targets another prefix")
            if key is not None:
                relative = key[len(self.prefix):]
                operation = (relative[len("operations/"):-len(".json")]
                             if relative.startswith("operations/")
                             and relative.endswith(".json") else None)
                valid_operation = False
                if operation is not None:
                    try:
                        validate_operation_id(operation)
                        valid_operation = True
                    except HandoffRefused:
                        pass
                if (relative != self.lock_relative_key
                        and not valid_operation):
                    raise HandoffRefused(
                        "journal access is not an exact operation/lock key")


def role_matches(identity_arn, role_arn):
    parsed = brownfield_inputs.parse_arn(role_arn, "handoff role", services=("iam",))
    role_path = parsed["resource"][len("role/"):]
    role_name = role_path.rsplit("/", 1)[-1]
    expected = f":assumed-role/{role_name}/"
    return str(identity_arn or "").startswith(
        f"arn:{parsed['partition']}:sts::{parsed['account']}{expected}")


def cutover_role_policy(topology, plan):
    """Return the exact dedicated-role permission boundary for one plan.

    This is an operator-facing contract, not an IAM mutation path.  Keeping it
    executable also lets tests prove that no destructive permission silently
    widens to ``*`` as the handoff implementation changes.
    """
    validate_topology(topology)
    _bind_plan(plan, plan.get("plan_digest"))
    if (str(plan.get("account_id")) != str(topology.account_id)
            or plan.get("region") != topology.region):
        raise HandoffRefused("IAM policy plan account/region does not match")
    role = brownfield_inputs.parse_arn(
        topology.eip_handoff_role_arn, "handoff role", services=("iam",))
    partition = role["partition"]
    account = str(topology.account_id)
    region = topology.region
    bucket_arn = f"arn:{partition}:s3:::{topology.eip_handoff_bucket}"
    prefix = topology.eip_handoff_prefix.strip("/")
    subnet_ids = sorted(
        topology.brownfield_manifest["load_balancer"]["subnet_ids"])
    statements = [{
        "Sid": "ReadExactHandoffEvidence",
        "Effect": "Allow",
        "Action": sorted((
            "sts:GetCallerIdentity", "ec2:DescribeAddresses",
            "ec2:DescribeInstances", "ec2:DescribeNetworkInterfaces",
            "ec2:DescribeSubnets",
            "elasticloadbalancing:DescribeLoadBalancers",
            "elasticloadbalancing:DescribeLoadBalancerAttributes",
            "elasticloadbalancing:DescribeListeners",
            "elasticloadbalancing:DescribeTags",
            "elasticloadbalancing:DescribeTargetGroups",
            "elasticloadbalancing:DescribeTargetHealth",
        )),
        "Resource": "*",
    }, {
        "Sid": "ReadJournalBucketControls",
        "Effect": "Allow",
        "Action": ["s3:GetBucketVersioning", "s3:GetEncryptionConfiguration"],
        "Resource": bucket_arn,
    }, {
        "Sid": "CASExactHandoffJournalPrefix",
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:PutObject"],
        "Resource": f"{bucket_arn}/{prefix}/*",
    }, {
        "Sid": "SetExactShadowNLBSubnets",
        "Effect": "Allow",
        "Action": "elasticloadbalancing:SetSubnets",
        "Resource": plan["load_balancer"]["arn"],
        "Condition": {"ForAllValues:StringEqualsIgnoreCase": {
            "elasticloadbalancing:Subnet": subnet_ids}},
    }]
    for az, source in sorted(plan["sources"].items()):
        allocation = source["allocation_id"]
        interface = source["network_interface_id"]
        exact_resources = [
            f"arn:{partition}:ec2:{region}:{account}:elastic-ip/{allocation}",
            f"arn:{partition}:ec2:{region}:{account}:network-interface/{interface}",
        ]
        conditions = {"StringEquals": {
            "ec2:AllocationId": allocation,
            "ec2:NetworkInterfaceID": interface,
            "ec2:Region": region,
        }}
        statements.extend(({
            "Sid": f"DisassociateExactSource{az.replace('-', '')}",
            "Effect": "Allow", "Action": "ec2:DisassociateAddress",
            "Resource": exact_resources, "Condition": conditions,
        }, {
            "Sid": f"RestoreExactSource{az.replace('-', '')}",
            "Effect": "Allow", "Action": "ec2:AssociateAddress",
            "Resource": exact_resources, "Condition": conditions,
        }))
    return {"Version": "2012-10-17", "Statement": statements}


def _validate_identity(identity, region_name, topology, problems):
    if str(identity.get("Account")) != str(topology.account_id):
        problems.append(
            f"dedicated role is in account {identity.get('Account')}, not "
            f"{topology.account_id}")
    if region_name != topology.region:
        problems.append(
            f"dedicated session region is {region_name}, not {topology.region}")
    if not role_matches(identity.get("Arn"), topology.eip_handoff_role_arn):
        problems.append("caller identity is not the exact configured handoff role")


def _validate_source_address(topology, row, declaration, compatibility,
                             problems):
    allocation_id = row.get("AllocationId")
    tags = _tags(row)
    ownership = {key: value for key, value in tags.items()
                 if key.startswith("mojo:") or key == "managed-by"}
    if ownership and ownership != {
            "managed-by": "django-mojo", "mojo:project": topology.project,
            "mojo:env": topology.env, "mojo:fleet": topology.fleet,
            "mojo:role": "preserved-ingress"}:
        problems.append(
            f"{allocation_id} has conflicting ownership tags {ownership}")
    for label, current, expected in (
            ("domain", row.get("Domain"), "vpc"),
            ("network border group", row.get("NetworkBorderGroup"),
             declaration["network_border_group"])):
        if current != expected:
            problems.append(
                f"{allocation_id} {label} is {current!r}, expected {expected!r}")
    if (row.get("CustomerOwnedIp") or row.get("CarrierIp")
            or row.get("ServiceManaged")):
        problems.append(f"{allocation_id} is not an ordinary VPC EIP")
    required = ("AssociationId", "NetworkInterfaceId", "PrivateIpAddress",
                "InstanceId", "PublicIp")
    missing = [key for key in required if not row.get(key)]
    if missing:
        problems.append(
            f"{allocation_id} source is not exactly reversible; missing "
            f"{', '.join(missing)}")
    if row.get("InstanceId") not in compatibility:
        problems.append(
            f"{allocation_id} source instance {row.get('InstanceId')} is not "
            f"a declared compatibility target")


def observe(clients, topology, canary_runner=None):
    """Observe and prove the complete source, NLB and serving state."""
    allocations = validate_topology(topology)
    canary_runner = canary_runner or run_canary
    problems = []
    sts = clients.get("sts")
    identity = sts.get_caller_identity()
    _validate_identity(identity, clients.region_name, topology, problems)

    manifest = topology.brownfield_manifest
    subnet_rows = {
        row["availability_zone"]: row for row in manifest["network"][
            "public_subnets"]
        if row["id"] in manifest["load_balancer"]["subnet_ids"]}
    ec2 = clients.get("ec2")
    subnets = ec2.describe_subnets(
        SubnetIds=manifest["load_balancer"]["subnet_ids"]).get("Subnets") or []
    actual_subnets = {row.get("AvailabilityZone"): row for row in subnets}
    for az, declaration in subnet_rows.items():
        row = actual_subnets.get(az) or {}
        if row.get("SubnetId") != declaration["id"]:
            problems.append(f"subnet {declaration['id']} did not resolve in {az}")
        if row.get("VpcId") != manifest["network"]["vpc_id"]:
            problems.append(f"subnet {declaration['id']} is in the wrong VPC")

    address_answer = ec2.describe_addresses(
        AllocationIds=sorted(allocations.values()))
    by_allocation = {
        row.get("AllocationId"): row
        for row in address_answer.get("Addresses") or []}
    sources = {}
    compatibility = set(manifest.get("compatibility_instance_ids") or ())
    for az, allocation_id in sorted(allocations.items()):
        row = by_allocation.get(allocation_id)
        if not row:
            problems.append(f"preserved allocation {allocation_id} was not found")
            continue
        declaration = subnet_rows[az]
        _validate_source_address(
            topology, row, declaration, compatibility, problems)
        sources[az] = _source(row, declaration)

    eni_ids = sorted({row["network_interface_id"] for row in sources.values()
                      if row.get("network_interface_id")})
    interfaces = {}
    if eni_ids:
        answer = ec2.describe_network_interfaces(
            NetworkInterfaceIds=eni_ids)
        interfaces = {row.get("NetworkInterfaceId"): row
                      for row in answer.get("NetworkInterfaces") or []}
    for az, source in sources.items():
        interface = interfaces.get(source["network_interface_id"])
        if not interface:
            problems.append(
                f"source ENI {source['network_interface_id']} was not found")
            continue
        attachment = interface.get("Attachment") or {}
        private = [row for row in interface.get("PrivateIpAddresses") or []
                   if row.get("PrivateIpAddress") == source["private_ip"]]
        for label, current, expected in (
                ("VPC", interface.get("VpcId"),
                 manifest["network"]["vpc_id"]),
                ("instance", attachment.get("InstanceId"),
                 source["instance_id"]),
                ("status", interface.get("Status"), "in-use"),
                ("attachment status", attachment.get("Status"), "attached")):
            if current != expected:
                problems.append(
                    f"source ENI {source['network_interface_id']} {label} is "
                    f"{current!r}, expected {expected!r}")
        if len(private) != 1:
            problems.append(
                f"source ENI {source['network_interface_id']} does not carry "
                f"the exact private IP once")
        else:
            association = private[0].get("Association") or {}
            if (association.get("AllocationId") != source["allocation_id"]
                    or association.get("AssociationId") !=
                    source["association_id"]
                    or association.get("PublicIp") != source["public_ip"]):
                problems.append(
                    f"source ENI {source['network_interface_id']} private-IP "
                    f"association is not the exact preserved EIP")
        source["source_subnet_id"] = interface.get("SubnetId")
        source["source_availability_zone"] = interface.get("AvailabilityZone")
        source["source_vpc_id"] = interface.get("VpcId")
        source["source_status"] = interface.get("Status")
        source["source_attachment_id"] = attachment.get("AttachmentId")
        source["source_device_index"] = attachment.get("DeviceIndex")
        source["source_attachment_status"] = attachment.get("Status")

    elbv2 = clients.get("elbv2")
    answer = elbv2.describe_load_balancers(
        Names=[manifest["load_balancer"]["name"]])
    rows = answer.get("LoadBalancers") or []
    if len(rows) != 1:
        raise HandoffRefused("the exact prepared NLB did not resolve once")
    load_balancer = rows[0]
    arn = load_balancer.get("LoadBalancerArn")
    clients._boundary.bind(
        balancer_arn=arn,
        sources=sources)
    if load_balancer.get("State", {}).get("Code") != "active":
        problems.append("the prepared NLB is not active")
    if load_balancer.get("VpcId") != manifest["network"]["vpc_id"]:
        problems.append("the prepared NLB is in the wrong VPC")
    nlb_map = normalize_map(load_balancer)
    if set(nlb_map) != set(subnet_rows):
        problems.append("the prepared NLB is not enabled in exactly two declared AZs")
    if len(nlb_map) != 2:
        problems.append("handoff requires a two-AZ NLB")
    for az, row in nlb_map.items():
        if row["subnet_id"] != subnet_rows.get(az, {}).get("id"):
            problems.append(f"the NLB mapping for {az} uses another subnet")

    attrs = elbv2.describe_load_balancer_attributes(
        LoadBalancerArn=arn).get("Attributes") or []
    attributes = {row.get("Key"): row.get("Value") for row in attrs}
    if attributes.get("load_balancing.cross_zone.enabled") != "true":
        problems.append("NLB cross-zone load balancing is not enabled")

    groups = {}
    for role, name in (("api", manifest["load_balancer"]["api_target_group"]),
                       ("certbot", manifest["load_balancer"][
                           "certbot_target_group"])):
        found = elbv2.describe_target_groups(Names=[name]).get("TargetGroups") or []
        if len(found) != 1:
            problems.append(f"the exact {role} target group did not resolve once")
            continue
        groups[role] = found[0]
    listeners = elbv2.describe_listeners(
        LoadBalancerArn=arn).get("Listeners") or []
    _validate_edge(topology, groups, listeners, problems)

    owned_arns = [arn]
    owned_arns.extend(group["TargetGroupArn"] for group in groups.values())
    ownership_tags = _owned_edge_tags(
        elbv2, topology, sorted(owned_arns), problems=problems)

    target_health = {}
    for role, group in groups.items():
        rows = elbv2.describe_target_health(
            TargetGroupArn=group["TargetGroupArn"]).get(
                "TargetHealthDescriptions") or []
        target_health[role] = _health(rows)
        unhealthy = [row for row in target_health[role]
                     if row["state"] != "healthy"]
        if unhealthy:
            problems.append(f"{role} target group has unhealthy targets")
    _validate_targets(topology, ec2, target_health, problems)

    canaries = []
    nlb_ips = sorted(row["public_ip"] for row in nlb_map.values()
                     if row.get("public_ip"))
    for definition in topology.eip_handoff_canaries:
        targets = (definition.get("addresses") or []
                   if definition.get("target", "nlb") == "node" else nlb_ips)
        if not targets:
            problems.append(f"canary {definition['name']} has no target IPs")
            continue
        for address in targets:
            result = canary_runner(definition, address)
            summary = {
                "name": definition["name"], "address": address,
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
                "certificate": bool(result.get("certificate", True)),
                "marker": bool(result.get("marker", True)),
            }
            canaries.append(summary)
            if not summary["ok"]:
                problems.append(
                    f"canary {definition['name']} failed through {address}")

    evidence = {
        "schema": SCHEMA,
        "account_id": str(identity.get("Account") or ""),
        "region": topology.region,
        "role_arn": topology.eip_handoff_role_arn,
        "manifest_digest": topology.manifest_digest,
        "load_balancer": {
            "arn": arn, "name": load_balancer.get("LoadBalancerName"),
            "state": load_balancer.get("State", {}).get("Code"),
            "map": nlb_map, "map_digest": digest(nlb_map),
            "cross_zone": True,
        },
        "sources": sources,
        "listeners": _listener_summary(listeners),
        "targets": target_health,
        "ownership_tags": ownership_tags,
        "canary_definitions_digest": digest(
            topology.eip_handoff_canaries),
        "canaries": sorted(canaries, key=lambda row: (
            row["name"], row["address"])),
        "manage_dns": False,
        "residual_public_edge": (
            "one fixed legacy address remains one known ingress/AZ"
            if len(allocations) == 1 else None),
    }
    if problems:
        raise HandoffRefused("; ".join(problems))
    evidence["evidence_digest"] = digest(evidence)
    return evidence


def build_plan(clients, topology, canary_runner=None):
    evidence = observe(clients, topology, canary_runner=canary_runner)
    plan = {
        "schema": SCHEMA,
        "account_id": evidence["account_id"], "region": evidence["region"],
        "role_arn": evidence["role_arn"],
        "manifest_digest": evidence["manifest_digest"],
        "environment": topology.env, "project": topology.project,
        "fleet": topology.fleet,
        "load_balancer": evidence["load_balancer"],
        "sources": evidence["sources"],
        "listeners": evidence["listeners"], "targets": evidence["targets"],
        "ownership_tags": evidence["ownership_tags"],
        "canary_definitions_digest": evidence[
            "canary_definitions_digest"],
        "canaries": evidence["canaries"], "manage_dns": False,
        "local_journal": topology.eip_handoff_local_journal,
        "remote_prefix": (
            f"s3://{topology.eip_handoff_bucket}/"
            f"{topology.eip_handoff_prefix}"),
        "inverse": {
            az: {"allocation_id": row["allocation_id"],
                 "network_interface_id": row["network_interface_id"],
                 "private_ip": row["private_ip"],
                 "source_subnet_id": row["source_subnet_id"],
                 "source_availability_zone": row["source_availability_zone"],
                 "source_vpc_id": row["source_vpc_id"],
                 "source_attachment_id": row["source_attachment_id"],
                 "source_device_index": row["source_device_index"],
                 "source_attachment_status": row["source_attachment_status"],
                 "target_subnet_id": row["target_subnet_id"]}
            for az, row in evidence["sources"].items()},
        "interruption": (
            "removing one NLB subnet terminates that AZ's connections and may "
            "take up to three minutes; clients can briefly fail between source "
            "disassociation and NLB association"),
        "residual_public_edge": evidence["residual_public_edge"],
    }
    plan["plan_digest"] = digest(plan)
    return plan


def expected_confirmation(plan, action, operation_id=None):
    action = action.upper()
    if action in ("RESUME", "ROLLBACK"):
        if not operation_id:
            raise HandoffRefused(f"{action.lower()} needs an operation id")
        return f"{action} {operation_id} {plan['plan_digest']}"
    allocations = ",".join(sorted(
        row["allocation_id"] for row in plan["sources"].values()))
    return (f"{action} {plan['environment']} {allocations} "
            f"{plan['plan_digest']}")


def default_plan_path(topology):
    return topology.eip_handoff_local_journal.rsplit(".json", 1)[0] + ".plan.json"


def save_plan(path, plan):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".handoff-plan.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(canonical(plan).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_plan(path):
    try:
        with open(path) as handle:
            plan = json.load(handle)
    except (OSError, ValueError) as err:
        raise HandoffRefused(f"cannot read immutable handoff plan {path}: {err}")
    _bind_plan(plan, plan.get("plan_digest"))
    return plan


def journal_coordinates(topology, operation_id):
    operation_id = validate_operation_id(operation_id)
    prefix = topology.eip_handoff_prefix.rstrip("/")
    lock_name = f"{topology.project}-{topology.env}-{topology.fleet}"
    journal_key = f"{prefix}/operations/{operation_id}.json"
    lock_key = f"{prefix}/locks/{lock_name}.json"
    base = f"s3://{topology.eip_handoff_bucket}/"
    local_base = topology.eip_handoff_local_journal
    if local_base.endswith(".json"):
        local_base = local_base[:-5]
    return {
        "operation_id": operation_id,
        "local_journal": f"{local_base}.{operation_id}.json",
        "journal_key": journal_key, "lock_key": lock_key,
        "remote_journal": base + journal_key,
        "remote_lock": base + lock_key,
    }


class JournalStore:
    def __init__(self, clients, topology, operation_id):
        self.s3 = clients.get("s3")
        self.topology = topology
        self.operation_id = operation_id
        self.bucket = topology.eip_handoff_bucket
        coordinates = journal_coordinates(topology, operation_id)
        self.journal_key = coordinates["journal_key"]
        # The mutex is fleet-wide.  An allocation-set-derived key would let
        # overlapping {A} and {A,B} operations race the same NLB and EIP.
        self.lock_key = coordinates["lock_key"]
        self.local_path = coordinates["local_journal"]
        self.journal_etag = None
        self.lock_etag = None
        self.lock_body = None

    def verify_bucket(self):
        versioning = self.s3.get_bucket_versioning(Bucket=self.bucket)
        if versioning.get("Status") != "Enabled":
            raise HandoffRefused("the remote journal bucket is not versioned")
        encryption = self.s3.get_bucket_encryption(Bucket=self.bucket)
        rules = (encryption.get("ServerSideEncryptionConfiguration") or {}).get(
            "Rules") or []
        algorithms = {
            (row.get("ApplyServerSideEncryptionByDefault") or {}).get(
                "SSEAlgorithm") for row in rules}
        if not algorithms.intersection(("AES256", "aws:kms")):
            raise HandoffRefused(
                "the remote journal bucket lacks approved default encryption")

    def prepare_local(self, journal):
        self._write_local(journal)

    def acquire_lock(self, plan_digest, journal_seed):
        body = {"schema": LOCK_SCHEMA, "state": "active",
                "operation_id": self.operation_id,
                "plan_digest": plan_digest, "updated_at": _now()}
        body["allocations"] = sorted(
            self.topology.nlb_eip_allocations.values())
        body["action"] = journal_seed.get("direction") or journal_seed.get("kind")
        body["journal_key"] = self.journal_key
        body["journal_seed"] = journal_seed
        existing = None
        try:
            existing = self.s3.get_object(
                Bucket=self.bucket, Key=self.lock_key)
        except Exception as err:
            if not _not_found(err):
                raise HandoffConflict(
                    f"remote handoff lock is unreadable: {_bounded_error(err)}")
        if existing:
            try:
                previous = json.loads(existing["Body"].read())
            except (KeyError, AttributeError, OSError, ValueError, TypeError):
                raise HandoffConflict("remote handoff lock is malformed")
            try:
                prior_coordinates = journal_coordinates(
                    self.topology, previous.get("operation_id"))
            except HandoffRefused:
                raise HandoffConflict(
                    "remote handoff lock operation id is invalid")
            if previous.get("journal_key") != prior_coordinates["journal_key"]:
                raise HandoffConflict(
                    "remote handoff lock journal coordinate is invalid")
            if (previous.get("schema") != LOCK_SCHEMA
                    or previous.get("state") not in TERMINAL_STATES):
                raise HandoffConflict(
                    f"remote handoff lock is active for operation "
                    f"{previous.get('operation_id')}")
            if previous.get("state") == "complete":
                raise HandoffConflict(
                    "a completed handoff lock may only enter exact rollback")
            if (body.get("action") == "handoff"
                    and previous.get("state") != "rehearsed"):
                raise HandoffConflict(
                    "live handoff requires a successful prior rehearsal")
            if (body.get("action") == "handoff"
                    and previous.get("plan_digest") != plan_digest):
                raise HandoffConflict(
                    "successful rehearsal plan differs from the handoff plan")
            if previous.get("operation_id") == self.operation_id:
                raise HandoffConflict(
                    "an existing operation id cannot be reused for a new run")
            if sorted(previous.get("allocations") or []) != body["allocations"]:
                raise HandoffConflict(
                    "terminal remote lock belongs to another allocation set")
            try:
                self.s3.head_object(Bucket=self.bucket, Key=self.journal_key)
            except Exception as err:
                if not _not_found(err):
                    raise HandoffConflict(
                        "cannot prove the new operation journal key is absent: "
                        f"{_bounded_error(err)}")
            else:
                raise HandoffConflict(
                    "operation id already has a remote journal; choose a new id")
            prior_etag = str(existing.get("ETag") or "").strip('"')
            if not prior_etag:
                raise HandoffConflict("terminal remote lock has no ETag")
            response = self.s3.put_object(
                Bucket=self.bucket, Key=self.lock_key,
                Body=canonical(body).encode("utf-8"),
                ContentType="application/json", IfMatch=prior_etag)
        else:
            if body.get("action") != "rehearsal":
                raise HandoffConflict(
                    "first use of a fleet lock must be a successful rehearsal")
            try:
                response = self.s3.put_object(
                    Bucket=self.bucket, Key=self.lock_key,
                    Body=canonical(body).encode("utf-8"),
                    ContentType="application/json", IfNoneMatch="*")
            except Exception as err:
                raise HandoffConflict(
                    f"remote handoff lock is already held or unreadable: "
                    f"{_bounded_error(err)}")
        try:
            self.lock_etag = _etag(response)
        except Exception:
            raise
        self.lock_body = body
        return body

    def start(self, journal):
        self._write_local(journal)
        try:
            response = self.s3.put_object(
                Bucket=self.bucket, Key=self.journal_key,
                Body=canonical(journal).encode("utf-8"),
                ContentType="application/json", IfNoneMatch="*")
        except Exception as err:
            raise HandoffConflict(
                f"remote journal create failed before mutation: "
                f"{_bounded_error(err)}")
        self.journal_etag = _etag(response)

    def bind_lock(self, plan_digest=None, expected_action=None):
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.lock_key)
            body = json.loads(response["Body"].read())
        except Exception as err:
            raise HandoffConflict(
                f"cannot bind the existing remote lock: {_bounded_error(err)}")
        if (body.get("schema") != LOCK_SCHEMA
                or body.get("operation_id") != self.operation_id
                or body.get("state") != "active"
                or body.get("journal_key") != self.journal_key):
            raise HandoffConflict(
                "remote lock is not active for this exact operation")
        if plan_digest and body.get("plan_digest") != plan_digest:
            raise HandoffConflict("remote lock plan digest does not match")
        if expected_action and body.get("action") != expected_action:
            raise HandoffConflict(
                f"remote lock action is {body.get('action')!r}, not "
                f"{expected_action!r}")
        if sorted(body.get("allocations") or []) != sorted(
                self.topology.nlb_eip_allocations.values()):
            raise HandoffConflict("remote lock allocation set does not match")
        self.lock_etag = str(response.get("ETag") or "").strip('"')
        self.lock_body = body
        if not self.lock_etag:
            raise HandoffConflict("remote lock has no ETag")

    def inspect_lock(self, plan_digest=None):
        """Read and bind the exact operation lock without changing it."""
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.lock_key)
            body = json.loads(response["Body"].read())
        except Exception as err:
            raise HandoffConflict(
                f"cannot inspect the existing remote lock: {_bounded_error(err)}")
        if (body.get("schema") != LOCK_SCHEMA
                or body.get("operation_id") != self.operation_id
                or body.get("state") not in set(TERMINAL_STATES) | {"active"}
                or body.get("journal_key") != self.journal_key
                or (plan_digest and body.get("plan_digest") != plan_digest)
                or sorted(body.get("allocations") or []) != sorted(
                    self.topology.nlb_eip_allocations.values())):
            raise HandoffConflict(
                "remote lock does not bind this exact operation/plan/fleet")
        self.lock_etag = str(response.get("ETag") or "").strip('"')
        self.lock_body = body
        if not self.lock_etag:
            raise HandoffConflict("remote lock has no ETag")
        return body

    def reopen_for_rollback(self, plan_digest):
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.lock_key)
            previous = json.loads(response["Body"].read())
        except Exception as err:
            raise HandoffConflict(
                f"cannot read terminal lock for rollback: {_bounded_error(err)}")
        if (previous.get("schema") != LOCK_SCHEMA
                or previous.get("operation_id") != self.operation_id
                or previous.get("state") not in ("active", "complete")
                or previous.get("journal_key") != self.journal_key
                or previous.get("plan_digest") != plan_digest
                or sorted(previous.get("allocations") or []) != sorted(
                    self.topology.nlb_eip_allocations.values())):
            raise HandoffConflict(
                "rollback requires the same exact active/completed operation lock")
        if previous.get("action") not in ("handoff", "rollback"):
            raise HandoffConflict(
                "remote lock direction cannot be switched into rollback")
        prior_etag = str(response.get("ETag") or "").strip('"')
        if not prior_etag:
            raise HandoffConflict("completed lock has no ETag")
        body = dict(previous)
        body.update({"state": "active", "action": "rollback",
                     "updated_at": _now()})
        updated = self.s3.put_object(
            Bucket=self.bucket, Key=self.lock_key,
            Body=canonical(body).encode("utf-8"),
            ContentType="application/json", IfMatch=prior_etag)
        self.lock_etag = _etag(updated)
        self.lock_body = body

    def advance(self, journal):
        if not self.journal_etag:
            raise HandoffRefused("remote journal ETag is not bound")
        self._write_local(journal)
        try:
            response = self.s3.put_object(
                Bucket=self.bucket, Key=self.journal_key,
                Body=canonical(journal).encode("utf-8"),
                ContentType="application/json", IfMatch=self.journal_etag)
        except Exception as err:
            raise HandoffConflict(
                f"remote journal CAS failed before mutation: "
                f"{_bounded_error(err)}")
        self.journal_etag = _etag(response)

    def finish_lock(self, state):
        if state not in TERMINAL_STATES:
            raise HandoffRefused("a non-terminal lock state was requested")
        if not self.lock_etag:
            raise HandoffRefused("remote lock ETag is not bound")
        body = dict(self.lock_body or {})
        body.update({"schema": LOCK_SCHEMA, "state": state,
                     "operation_id": self.operation_id,
                     "updated_at": _now()})
        response = self.s3.put_object(
            Bucket=self.bucket, Key=self.lock_key,
            Body=canonical(body).encode("utf-8"),
            ContentType="application/json", IfMatch=self.lock_etag)
        self.lock_etag = _etag(response)
        self.lock_body = body

    def load(self, recover=True):
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.journal_key)
        except Exception as err:
            if not _not_found(err) or not self.lock_body:
                raise HandoffRefused(
                    f"cannot read remote handoff journal: {_bounded_error(err)}")
            seed = self.lock_body.get("journal_seed")
            if (not isinstance(seed, dict) or seed.get("operation_id") !=
                    self.operation_id):
                raise HandoffRefused(
                    "active lock has no usable journal recovery seed")
            _validate_journal(seed)
            self._validate_loaded(seed)
            if not recover:
                return seed
            self.start(seed)
            return seed
        try:
            body = response["Body"].read()
            journal = json.loads(body)
        except (KeyError, AttributeError, OSError, ValueError, TypeError):
            raise HandoffRefused("the remote handoff journal is malformed")
        if journal.get("schema") != SCHEMA:
            raise HandoffRefused("the remote handoff journal schema is unknown")
        _validate_journal(journal)
        self._validate_loaded(journal)
        self.journal_etag = str(response.get("ETag") or "").strip('"')
        if not self.journal_etag:
            raise HandoffRefused("the remote handoff journal has no ETag")
        return journal

    def _validate_loaded(self, journal):
        if journal.get("operation_id") != self.operation_id:
            raise HandoffRefused(
                "remote journal belongs to another operation id")
        if self.lock_body and (
                self.lock_body.get("operation_id") != self.operation_id
                or self.lock_body.get("journal_key") != self.journal_key
                or self.lock_body.get("plan_digest") !=
                journal.get("plan_digest")):
            raise HandoffRefused(
                "remote lock/journal coordinates or plan do not match")

    def _write_local(self, journal):
        directory = os.path.dirname(os.path.abspath(self.local_path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=".handoff.", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            body = canonical(journal).encode("utf-8")
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = None
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.local_path)
            os.chmod(self.local_path, 0o600)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def rehearse(clients, topology, plan, supplied_digest, confirmation,
             canary_runner=None, operation_id=None):
    _bind_plan(plan, supplied_digest)
    _validate_topology_binding(topology, plan)
    expected = expected_confirmation(plan, "REHEARSE")
    if confirmation != expected:
        raise HandoffRefused(f"confirmation must equal: {expected}")
    repeated = build_plan(clients, topology, canary_runner=canary_runner)
    _same_plan(plan, repeated)
    operation_id = operation_id or str(uuid.uuid4())
    store = JournalStore(clients, topology, operation_id)
    journal = _new_journal(operation_id, plan, "rehearsal")
    store.verify_bucket()
    store.acquire_lock(plan["plan_digest"], journal)
    try:
        store.prepare_local(journal)
        journal["state"] = "rehearsed"
        journal["transitions"].append(
            _transition("rehearsed", "no provider mutation was invoked"))
        store.start(journal)
        store.finish_lock("rehearsed")
        return journal
    except Exception:
        # Rehearsal has no provider-mutation call site.  Once its fleet lock is
        # acquired, any local/S3 journal failure is therefore safe to
        # terminalize so it cannot orphan an active lock forever.
        try:
            store.finish_lock("rehearsal_failed")
        except Exception as terminal_err:
            raise HandoffConflict(
                "rehearsal failed and its mutation-free lock could not be "
                f"terminalized: {_bounded_error(terminal_err)}")
        raise


def handoff(clients, topology, plan, supplied_digest, confirmation,
            canary_runner=None, operation_id=None, sleeper=None,
            timeout=190):
    _bind_plan(plan, supplied_digest)
    _validate_topology_binding(topology, plan)
    operation_id = operation_id or str(uuid.uuid4())
    expected = expected_confirmation(plan, "HANDOFF")
    if confirmation != expected:
        raise HandoffRefused(f"confirmation must equal: {expected}")
    store = JournalStore(clients, topology, operation_id)
    journal = _new_journal(operation_id, plan, "handoff")
    store.verify_bucket()
    store.acquire_lock(plan["plan_digest"], journal)
    store.prepare_local(journal)
    repeated = build_plan(clients, topology, canary_runner=canary_runner)
    _same_plan(plan, repeated)
    store.start(journal)
    try:
        _transfer(clients, topology, journal, store,
                  canary_runner=canary_runner, sleeper=sleeper,
                  timeout=timeout)
        journal["state"] = "complete"
        _record(journal, store, "complete", "all configured EIPs mapped")
        store.finish_lock("complete")
        return journal
    except Exception as err:
        _record_error(journal, store, err, clients)
        raise


def resume(clients, topology, plan, supplied_digest, confirmation,
           operation_id, canary_runner=None, sleeper=None, timeout=190):
    _bind_plan(plan, supplied_digest)
    _validate_topology_binding(topology, plan)
    expected = expected_confirmation(plan, "RESUME", operation_id)
    if confirmation != expected:
        raise HandoffRefused(f"confirmation must equal: {expected}")
    store = JournalStore(clients, topology, operation_id)
    store.verify_bucket()
    store.bind_lock(plan["plan_digest"], expected_action="handoff")
    journal = store.load()
    _journal_matches(journal, plan)
    if journal.get("direction") != "handoff":
        raise HandoffConflict(
            "handoff resume cannot consume a rollback-direction journal")
    _bind_boundary_from_plan(clients, plan)
    revalidate_runtime(clients, topology, plan, journal,
                       canary_runner=canary_runner, direction="handoff")
    try:
        _transfer(clients, topology, journal, store,
                  canary_runner=canary_runner, sleeper=sleeper,
                  timeout=timeout)
        journal["state"] = "complete"
        _record(journal, store, "complete", "resume reached target state")
        store.finish_lock("complete")
        return journal
    except Exception as err:
        _record_error(journal, store, err, clients)
        raise


def rollback(clients, topology, plan, supplied_digest, confirmation,
             operation_id, canary_runner=None, sleeper=None, timeout=190):
    _bind_plan(plan, supplied_digest)
    _validate_topology_binding(topology, plan)
    expected = expected_confirmation(plan, "ROLLBACK", operation_id)
    if confirmation != expected:
        raise HandoffRefused(f"confirmation must equal: {expected}")
    store = JournalStore(clients, topology, operation_id)
    store.verify_bucket()
    store.reopen_for_rollback(plan["plan_digest"])
    journal = store.load()
    _journal_matches(journal, plan)
    if journal.get("direction") not in ("handoff", "rollback"):
        raise HandoffConflict("journal direction cannot enter rollback")
    journal["direction"] = "rollback"
    journal["updated_at"] = _now()
    store.advance(journal)
    _bind_boundary_from_plan(clients, plan)
    revalidate_runtime(clients, topology, plan, journal,
                       canary_runner=canary_runner, direction="rollback")
    try:
        _restore(clients, topology, journal, store, sleeper=sleeper,
                 timeout=timeout)
        journal["state"] = "rolled_back"
        _record(journal, store, "rolled_back", "all sources restored")
        store.finish_lock("rolled_back")
        return journal
    except Exception as err:
        _record_error(journal, store, err, clients)
        raise


def revalidate_runtime(clients, topology, plan, journal,
                       canary_runner=None, direction="handoff"):
    """Fresh role, fleet and canary gate for resume and rollback.

    A partial operation cannot satisfy the original source-associated preview,
    so this gate validates the same serving plane while accepting only journaled
    provider states.  It performs no mutation.
    """
    validate_topology(topology)
    _validate_topology_binding(topology, plan)
    canary_runner = canary_runner or run_canary
    identity = clients.get("sts").get_caller_identity()
    if (str(identity.get("Account")) != str(plan["account_id"])
            or clients.region_name != plan["region"]
            or not role_matches(identity.get("Arn"), plan["role_arn"])):
        raise HandoffRefused(
            "fresh caller identity/account/region does not match the plan")
    current = provider_state(clients, journal)
    _accepted_map(journal, current["map"])
    if (direction == "handoff"
            and current["load_balancer"].get("State", {}).get("Code") != "active"):
        raise HandoffRefused("the journaled NLB is not active")
    if not current["map"]:
        raise HandoffRefused("the journaled NLB has no remaining enabled AZ")
    for az, source in journal["sources"].items():
        state = derive_state(source, current, journal["expected_map"], direction)
        if state == "unexpected":
            raise UnexpectedState(
                f"{az} is not a recognized journal/provider state")
    _validate_source_interfaces(clients.get("ec2"), topology, journal["sources"])
    expected_owned = plan.get("ownership_tags") or {}
    if not expected_owned:
        raise HandoffRefused("immutable plan lacks owned edge tag evidence")
    ownership_problems = []
    actual_owned = _owned_edge_tags(
        clients.get("elbv2"), topology, sorted(expected_owned),
        problems=ownership_problems)
    if ownership_problems or actual_owned != expected_owned:
        raise HandoffRefused(
            "; ".join(ownership_problems or ["owned edge tags drifted"]))

    # Replacement failure is the reason rollback exists.  Its gate proves the
    # exact identity, lock/journal state, NLB map classification, and original
    # ENI/private-IP restorability above; target health and replacement
    # canaries must never veto restoration of the working source.
    if direction == "rollback":
        return current

    elbv2 = clients.get("elbv2")
    arn = journal["load_balancer_arn"]
    attrs = elbv2.describe_load_balancer_attributes(
        LoadBalancerArn=arn).get("Attributes") or []
    attributes = {row.get("Key"): row.get("Value") for row in attrs}
    if attributes.get("load_balancing.cross_zone.enabled") != "true":
        raise HandoffRefused("NLB cross-zone behavior drifted")
    manifest = topology.brownfield_manifest
    groups, problems = {}, []
    for role, name in (("api", manifest["load_balancer"]["api_target_group"]),
                       ("certbot", manifest["load_balancer"][
                           "certbot_target_group"])):
        rows = elbv2.describe_target_groups(Names=[name]).get("TargetGroups") or []
        if len(rows) != 1:
            problems.append(f"{role} target group does not resolve exactly")
        else:
            groups[role] = rows[0]
    listeners = elbv2.describe_listeners(
        LoadBalancerArn=arn).get("Listeners") or []
    _validate_edge(topology, groups, listeners, problems)
    health = {}
    for role, group in groups.items():
        rows = elbv2.describe_target_health(
            TargetGroupArn=group["TargetGroupArn"]).get(
                "TargetHealthDescriptions") or []
        health[role] = _health(rows)
        if any(row["state"] != "healthy" for row in health[role]):
            problems.append(f"{role} target group has unhealthy targets")
    _validate_targets(topology, clients.get("ec2"), health, problems)
    if problems:
        raise HandoffRefused("; ".join(problems))

    nlb_ips = sorted(row["public_ip"] for row in current["map"].values()
                     if row.get("public_ip"))
    nlb_runs = 0
    for definition in topology.eip_handoff_canaries:
        if definition.get("target", "nlb") == "nlb":
            targets = nlb_ips
            nlb_runs += len(targets)
        else:
            targets = definition.get("addresses") or []
        for address in targets:
            if not canary_runner(definition, address).get("ok"):
                raise HandoffRefused(
                    f"fresh canary {definition['name']} failed through {address}")
    if not nlb_runs:
        raise HandoffRefused(
            "no address-specific NLB canary ran during fresh validation")
    return current


def _validate_source_interfaces(ec2, topology, sources):
    ids = sorted(source["network_interface_id"] for source in sources.values())
    rows = ec2.describe_network_interfaces(
        NetworkInterfaceIds=ids).get("NetworkInterfaces") or []
    by_id = {row.get("NetworkInterfaceId"): row for row in rows}
    for source in sources.values():
        row = by_id.get(source["network_interface_id"])
        if not row:
            raise HandoffRefused(
                f"source ENI {source['network_interface_id']} disappeared")
        attachment = row.get("Attachment") or {}
        private = [entry for entry in row.get("PrivateIpAddresses") or []
                   if entry.get("PrivateIpAddress") == source["private_ip"]]
        if (row.get("VpcId") != source.get("source_vpc_id")
                or row.get("VpcId") !=
                topology.brownfield_manifest["network"]["vpc_id"]
                or row.get("SubnetId") != source.get("source_subnet_id")
                or row.get("AvailabilityZone") != source.get(
                    "source_availability_zone")
                or attachment.get("InstanceId") != source.get("instance_id")
                or row.get("Status") != source.get("source_status")
                or attachment.get("AttachmentId") != source.get(
                    "source_attachment_id")
                or attachment.get("DeviceIndex") != source.get(
                    "source_device_index")
                or attachment.get("Status") != source.get(
                    "source_attachment_status")
                or len(private) != 1):
            raise HandoffRefused(
                f"source ENI {source['network_interface_id']} drifted")


def _transfer(clients, topology, journal, store, canary_runner=None,
              sleeper=None, timeout=190):
    canary_runner = canary_runner or run_canary
    expected_map = dict(journal["expected_map"])
    for az in sorted(journal["sources"]):
        source = journal["sources"][az]
        current = provider_state(clients, journal)
        expected_map = _accepted_map(journal, current["map"])
        state = derive_state(source, current, expected_map, "handoff")
        if state == "target":
            expected_map = current["map"]
            _record(journal, store, "target",
                    "recovered provider target state", az=az,
                    expected_map=expected_map)
            continue
        if state == "source":
            _cas_map(current["map"], expected_map)
            without = dict(expected_map)
            without.pop(az)
            _record(journal, store, "before_remove_subnet",
                    f"remove only {az}", az=az,
                    expected_map=expected_map, next_map=without)
            _set_map(clients, journal, without)
            _wait_map(clients, journal, without, sleeper, timeout)
            expected_map = without
            _record(journal, store, "subnet_removed_source",
                    "selected NLB subnet is absent; source remains exact",
                    az=az, expected_map=expected_map)
            current = provider_state(clients, journal)
            state = derive_state(source, current, expected_map, "handoff")
        if state == "subnet_removed_source":
            _exact_source(source, current["addresses"].get(
                source["allocation_id"]))
            _record(journal, store, "before_disassociate",
                    "free exact source association", az=az,
                    association_id=source["association_id"])
            clients.get("ec2").disassociate_address(
                AssociationId=source["association_id"])
            _wait_address(clients, source["allocation_id"], None,
                          sleeper, timeout)
            _record(journal, store, "free",
                    "preserved allocation is provider-confirmed free", az=az,
                    expected_map=expected_map)
            current = provider_state(clients, journal)
            state = derive_state(source, current, expected_map, "handoff")
        if state == "free":
            _cas_map(current["map"], expected_map)
            target_map = dict(expected_map)
            target_map[az] = {
                "availability_zone": az,
                "subnet_id": source["target_subnet_id"],
                "allocation_id": source["allocation_id"],
                "public_ip": source["public_ip"]}
            _record(journal, store, "before_target_mapping",
                    "map preserved allocation to the same subnet", az=az,
                    expected_map=expected_map, next_map=target_map)
            _set_map(clients, journal, target_map)
            _wait_map(clients, journal, target_map, sleeper, timeout,
                      compare_public=False)
            current = provider_state(clients, journal)
            state = derive_state(source, current, target_map, "handoff")
            if state != "target":
                raise UnexpectedState(
                    f"{az} did not reach target state; observed {state}")
            for definition in topology.eip_handoff_canaries:
                if definition.get("target", "nlb") != "nlb":
                    continue
                result = canary_runner(definition, source["public_ip"])
                if not result.get("ok"):
                    raise HandoffRefused(
                        f"post-transfer canary {definition['name']} failed "
                        f"through {source['public_ip']}")
            expected_map = current["map"]
            _record(journal, store, "target",
                    "provider truth and preserved-IP canary are green", az=az,
                    expected_map=expected_map)
            continue
        if state != "target":
            raise UnexpectedState(
                f"{az} is {state}; refusing to guess or advance another AZ")
    journal["expected_map"] = expected_map


def _restore(clients, topology, journal, store, sleeper=None, timeout=190):
    initial = provider_state(clients, journal)
    expected_map = _accepted_map(journal, initial["map"])
    for az in reversed(sorted(journal["sources"])):
        source = journal["sources"][az]
        current = provider_state(clients, journal)
        expected_map = _accepted_map(journal, current["map"])
        state = derive_state(source, current, expected_map, "rollback")
        if state == "unexpected":
            raise UnexpectedState(
                f"{az} is ambiguous; rollback stops before NLB mutation")
        if state in ("source", "prepared"):
            continue
        mapping = current["map"].get(az)
        if mapping and mapping.get("allocation_id") == source["allocation_id"]:
            without = dict(current["map"])
            without.pop(az)
            _record(journal, store, "before_rollback_remove",
                    "remove preserved target mapping", az=az,
                    expected_map=current["map"], next_map=without)
            _set_map(clients, journal, without)
            _wait_map(clients, journal, without, sleeper, timeout)
            _wait_address(clients, source["allocation_id"], None,
                          sleeper, timeout)
            _record(journal, store, "rollback_subnet_removed",
                    "preserved target mapping is absent and allocation free",
                    az=az, expected_map=without)
            current = provider_state(clients, journal)
        address = current["addresses"].get(source["allocation_id"]) or {}
        if not address.get("AssociationId"):
            _record(journal, store, "before_source_restore",
                    "restore exact ENI/private IP without reassociation", az=az)
            clients.get("ec2").associate_address(
                AllocationId=source["allocation_id"],
                NetworkInterfaceId=source["network_interface_id"],
                PrivateIpAddress=source["private_ip"],
                AllowReassociation=False)
            _wait_address(clients, source["allocation_id"], source,
                          sleeper, timeout)
            _record(journal, store, "source_restored",
                    "exact source ENI/private IP is associated", az=az,
                    expected_map=current["map"])
        current = provider_state(clients, journal)
        if az not in current["map"]:
            prepared = dict(current["map"])
            prepared[az] = {"availability_zone": az,
                            "subnet_id": source["target_subnet_id"],
                            "allocation_id": None, "public_ip": None}
            _record(journal, store, "before_temporary_mapping",
                    "restore same NLB subnet with an AWS temporary address",
                    az=az, expected_map=current["map"], next_map=prepared)
            _set_map(clients, journal, prepared)
            _wait_map(clients, journal, prepared, sleeper, timeout,
                      compare_public=False)
        current = provider_state(clients, journal)
        state = derive_state(source, current, current["map"], "rollback")
        if state != "prepared":
            raise UnexpectedState(
                f"{az} did not reach prepared rollback state; observed {state}")
        expected_map = current["map"]
        _record(journal, store, "prepared",
                "source restored and temporary NLB mapping healthy", az=az,
                expected_map=expected_map)
    journal["expected_map"] = expected_map


def provider_state(clients, journal):
    elbv2 = clients.get("elbv2")
    arn = journal["load_balancer_arn"]
    rows = elbv2.describe_load_balancers(
        LoadBalancerArns=[arn]).get("LoadBalancers") or []
    if len(rows) != 1:
        raise UnexpectedState("the journaled NLB no longer resolves exactly")
    allocations = sorted(row["allocation_id"]
                         for row in journal["sources"].values())
    ec2 = clients.get("ec2")
    addresses = ec2.describe_addresses(
        AllocationIds=allocations).get("Addresses") or []
    eni_ids = sorted({row.get("NetworkInterfaceId") for row in addresses
                      if row.get("NetworkInterfaceId")
                      and not row.get("InstanceId")})
    interfaces = []
    if eni_ids:
        interfaces = ec2.describe_network_interfaces(
            NetworkInterfaceIds=eni_ids).get("NetworkInterfaces") or []
    return {"load_balancer": rows[0], "map": normalize_map(rows[0]),
            "addresses": {row.get("AllocationId"): row for row in addresses},
            "network_interfaces": {
                row.get("NetworkInterfaceId"): row for row in interfaces}}


def derive_state(source, provider, expected_map, direction):
    mapping = provider["map"].get(source["availability_zone"])
    address = provider["addresses"].get(source["allocation_id"]) or {}
    source_exact = (_restored_source_matches(source, address)
                    if direction == "rollback"
                    else _source_matches(source, address))
    free = not address.get("AssociationId")
    on_target = (mapping and
                 mapping.get("allocation_id") == source["allocation_id"])
    temporary = (mapping and not mapping.get("allocation_id")
                 and mapping.get("subnet_id") == source.get(
                     "target_subnet_id"))
    if on_target and _target_matches(
            source, mapping, address,
            provider.get("network_interfaces") or {}):
        return "target"
    if source_exact and temporary:
        return "prepared" if direction == "rollback" else "source"
    if source_exact and not mapping:
        return ("source_restored" if direction == "rollback"
                else "subnet_removed_source")
    if free and not mapping:
        return ("rollback_subnet_removed" if direction == "rollback"
                else "free")
    return "unexpected"


def _target_matches(source, mapping, address, interfaces):
    """NLB EIPs are associated to an ELB service-managed ENI, not free."""
    if (not mapping or not address
            or mapping.get("subnet_id") != source.get("target_subnet_id")):
        return False
    if address.get("AllocationId") != source.get("allocation_id"):
        return False
    if not address.get("AssociationId") or not address.get("NetworkInterfaceId"):
        return False
    if address.get("InstanceId"):
        return False
    interface = interfaces.get(address.get("NetworkInterfaceId")) or {}
    if (not interface.get("RequesterManaged")
            or interface.get("SubnetId") != mapping.get("subnet_id")
            or interface.get("VpcId") != source.get("source_vpc_id")
            or interface.get("AvailabilityZone") != source.get(
                "availability_zone")
            or interface.get("InterfaceType") not in (
                "network_load_balancer", "load_balancer")):
        return False
    private = mapping.get("private_ipv4")
    if private and address.get("PrivateIpAddress") != private:
        return False
    return True


def normalize_map(load_balancer):
    answer = {}
    for zone in load_balancer.get("AvailabilityZones") or []:
        addresses = zone.get("LoadBalancerAddresses") or []
        if len(addresses) != 1:
            raise HandoffRefused(
                f"NLB AZ {zone.get('ZoneName')} does not have one IPv4 mapping")
        address = addresses[0]
        answer[zone.get("ZoneName")] = {
            "availability_zone": zone.get("ZoneName"),
            "subnet_id": zone.get("SubnetId"),
            "allocation_id": address.get("AllocationId"),
            "public_ip": address.get("IpAddress"),
            "private_ipv4": address.get("PrivateIPv4Address"),
            "ipv6": address.get("IPv6Address"),
            "source_nat_ipv6_prefix": address.get("SourceNatIpv6Prefix"),
        }
    return {key: answer[key] for key in sorted(answer)}


def set_subnet_mappings(mapping):
    rows = []
    for az in sorted(mapping):
        current = mapping[az]
        row = {"SubnetId": current["subnet_id"]}
        for source, target in (
                ("allocation_id", "AllocationId"),
                ("private_ipv4", "PrivateIPv4Address"),
                ("ipv6", "IPv6Address"),
                ("source_nat_ipv6_prefix", "SourceNatIpv6Prefix")):
            if current.get(source):
                row[target] = current[source]
        rows.append(row)
    return rows


def _set_map(clients, journal, mapping):
    clients.get("elbv2").set_subnets(
        LoadBalancerArn=journal["load_balancer_arn"],
        SubnetMappings=set_subnet_mappings(mapping))


def _wait_map(clients, journal, expected, sleeper, timeout,
              compare_public=True):
    sleeper = sleeper or time.sleep
    deadline = time.monotonic() + timeout
    while True:
        current = provider_state(clients, journal)["map"]
        if _map_matches(current, expected, compare_public=compare_public,
                        allow_provider_values=True):
            return current
        if time.monotonic() >= deadline:
            raise HandoffRefused(
                "timed out waiting for the complete NLB mapping transition")
        sleeper(5)


def _wait_has_az(clients, journal, az, sleeper, timeout):
    sleeper = sleeper or time.sleep
    deadline = time.monotonic() + timeout
    while True:
        current = provider_state(clients, journal)["map"]
        if az in current:
            return current
        if time.monotonic() >= deadline:
            raise HandoffRefused("timed out waiting for the NLB subnet")
        sleeper(5)


def _wait_address(clients, allocation_id, source, sleeper, timeout):
    sleeper = sleeper or time.sleep
    deadline = time.monotonic() + timeout
    while True:
        rows = clients.get("ec2").describe_addresses(
            AllocationIds=[allocation_id]).get("Addresses") or []
        row = rows[0] if len(rows) == 1 else {}
        if source is None and row and not row.get("AssociationId"):
            return row
        if source is not None and _restored_source_matches(source, row):
            return row
        if time.monotonic() >= deadline:
            raise HandoffRefused(
                f"timed out waiting for allocation {allocation_id}")
        sleeper(5)


def run_canary(definition, address):
    protocol = definition["protocol"].lower()
    timeout = float(definition["timeout"])
    try:
        raw = socket.create_connection((address, definition["port"]), timeout)
        certificate = True
        if protocol == "https":
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=definition["tls_sni"])
            certificate = bool(raw.getpeercert())
        if protocol == "tcp" and not definition.get("request"):
            raw.close()
            return {"ok": True, "certificate": certificate}
        request = definition.get("request")
        if request is None:
            path = definition.get("path") or "/"
            request = (f"GET {path} HTTP/1.1\r\nHost: {definition['host']}\r\n"
                       f"Connection: close\r\n\r\n")
        raw.sendall(request.encode("utf-8"))
        response = http.client.HTTPResponse(raw)
        response.begin()
        body = response.read(1024 * 1024)
        raw.close()
        marker = definition.get("expected_marker")
        marker_ok = (not marker or marker.encode("utf-8") in body)
        status_ok = (definition.get("expected_status") is None
                     or response.status == definition["expected_status"])
        return {"ok": certificate and marker_ok and status_ok,
                "certificate": certificate, "marker": marker_ok,
                "status": response.status}
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        return {"ok": False, "certificate": False, "marker": False,
                "status": None}


def _new_journal(operation_id, plan, kind):
    return {
        "schema": SCHEMA, "operation_id": operation_id, "kind": kind,
        "direction": kind,
        "state": "source", "sequence": 0,
        "account_id": plan["account_id"], "region": plan["region"],
        "role_arn": plan["role_arn"], "environment": plan["environment"],
        "project": plan["project"], "fleet": plan["fleet"],
        "manifest_digest": plan["manifest_digest"],
        "plan_digest": plan["plan_digest"],
        "remote_prefix": plan["remote_prefix"],
        "local_journal_base": plan["local_journal"],
        "load_balancer_arn": plan["load_balancer"]["arn"],
        "expected_map": json.loads(canonical(plan["load_balancer"]["map"])),
        "initial_map_digest": plan["load_balancer"]["map_digest"],
        "expected_map_digest": plan["load_balancer"]["map_digest"],
        "prior_map": None, "prior_map_digest": None,
        "pending_intent": None,
        "sources": json.loads(canonical(plan["sources"])),
        "canaries": json.loads(canonical(plan["canaries"])),
        "canary_definitions_digest": plan[
            "canary_definitions_digest"],
        "inverse": json.loads(canonical(plan["inverse"])),
        "transitions": [], "errors": [],
        "created_at": _now(), "updated_at": _now(),
    }


def _record(journal, store, state, message, **facts):
    prior = facts.get("expected_map", journal.get("expected_map"))
    next_map = facts.get("next_map")
    if state.startswith("before_"):
        journal["prior_map"] = prior
        journal["prior_map_digest"] = digest(prior)
        journal["expected_map"] = next_map if next_map is not None else prior
        journal["expected_map_digest"] = digest(journal["expected_map"])
        journal["pending_intent"] = {
            "state": state, "az": facts.get("az"),
            "at": _now(), "sequence": journal["sequence"] + 1,
        }
    else:
        if facts.get("expected_map") is not None:
            journal["expected_map"] = facts["expected_map"]
        journal["expected_map_digest"] = digest(journal["expected_map"])
        journal["prior_map"] = None
        journal["prior_map_digest"] = None
        journal["pending_intent"] = None
    journal["sequence"] += 1
    journal["state"] = state
    journal["updated_at"] = _now()
    journal["transitions"].append(_transition(state, message, **facts))
    store.advance(journal)


def _record_error(journal, store, err, clients):
    journal["errors"].append({"at": _now(), "error": _bounded_error(err)})
    journal["state"] = "unexpected"
    journal["updated_at"] = _now()
    try:
        current = provider_state(clients, journal)
        journal["provider_truth"] = {
            "observed_at": _now(), "map": current["map"],
            "addresses": {
                allocation: {
                    "allocation_id": row.get("AllocationId"),
                    "public_ip": row.get("PublicIp"),
                    "association_id": row.get("AssociationId"),
                    "network_interface_id": row.get("NetworkInterfaceId"),
                    "private_ip": row.get("PrivateIpAddress"),
                    "instance_id": row.get("InstanceId"),
                } for allocation, row in current["addresses"].items()},
            "derived_states": {
                az: derive_state(source, current,
                                 journal.get("expected_map") or {},
                                 "rollback" if journal.get("direction") ==
                                 "rollback" else "handoff")
                for az, source in journal["sources"].items()},
        }
    except Exception as observe_err:
        journal["provider_truth"] = {
            "observed_at": _now(), "unavailable": _bounded_error(observe_err)}
    try:
        store.advance(journal)
    except Exception:
        pass


def _transition(state, message, **facts):
    return {"at": _now(), "state": state, "message": message,
            "facts": facts}


def _bind_plan(plan, supplied_digest):
    if plan.get("schema") != SCHEMA:
        raise HandoffRefused("the plan schema is unknown")
    actual = digest({key: value for key, value in plan.items()
                     if key != "plan_digest"})
    if actual != plan.get("plan_digest"):
        raise HandoffRefused("the plan document was modified after preview")
    if supplied_digest != actual:
        raise HandoffRefused("--plan-digest does not match the exact preview")
    if plan.get("manage_dns") is not False:
        raise HandoffRefused("the immutable plan does not bind manage_dns=false")
    load_balancer = plan.get("load_balancer") or {}
    if load_balancer.get("map_digest") != digest(
            load_balancer.get("map") or {}):
        raise HandoffRefused("the immutable plan mapping digest is invalid")
    if not re.fullmatch(
            r"[0-9a-f]{64}", plan.get("canary_definitions_digest") or ""):
        raise HandoffRefused(
            "the immutable plan lacks an exact canary definitions digest")
    inverse_keys = (
        "allocation_id", "network_interface_id", "private_ip",
        "source_subnet_id", "source_availability_zone", "source_vpc_id",
        "source_attachment_id", "source_device_index",
        "source_attachment_status", "target_subnet_id")
    expected_inverse = {
        az: {key: source.get(key) for key in inverse_keys}
        for az, source in (plan.get("sources") or {}).items()}
    if plan.get("inverse") != expected_inverse:
        raise HandoffRefused("the immutable plan inverse is incomplete")
    if not isinstance(plan.get("ownership_tags"), dict) or not plan[
            "ownership_tags"]:
        raise HandoffRefused(
            "the immutable plan lacks exact owned edge tag evidence")
    owned_arns = {plan.get("load_balancer", {}).get("arn")}
    owned_arns.update(row.get("target_group_arn")
                      for row in plan.get("listeners") or [])
    owned_arns.discard(None)
    if set(plan["ownership_tags"]) != owned_arns:
        raise HandoffRefused(
            "owned edge tag evidence does not match the exact NLB/TG ARNs")


def _validate_topology_binding(topology, plan):
    validate_topology(topology)
    scalar_pairs = (
        ("account", str(topology.account_id), str(plan.get("account_id"))),
        ("region", topology.region, plan.get("region")),
        ("project", topology.project, plan.get("project")),
        ("environment", topology.env, plan.get("environment")),
        ("fleet", topology.fleet, plan.get("fleet")),
        ("manifest digest", topology.manifest_digest,
         plan.get("manifest_digest")),
        ("handoff role", topology.eip_handoff_role_arn,
         plan.get("role_arn")),
        ("local journal base", topology.eip_handoff_local_journal,
         plan.get("local_journal")),
        ("remote journal prefix",
         f"s3://{topology.eip_handoff_bucket}/"
         f"{topology.eip_handoff_prefix}", plan.get("remote_prefix")),
    )
    drift = [label for label, current, expected in scalar_pairs
             if current != expected]
    allocations = {az: source.get("allocation_id")
                   for az, source in (plan.get("sources") or {}).items()}
    if allocations != dict(topology.nlb_eip_allocations):
        drift.append("preserved allocation map")
    if digest(topology.eip_handoff_canaries) != plan.get(
            "canary_definitions_digest"):
        drift.append("handoff canary definitions")
    if drift:
        raise HandoffConflict(
            "current fleet manifest no longer matches immutable plan: "
            + ", ".join(drift))


def _same_plan(first, second):
    if first.get("plan_digest") != second.get("plan_digest"):
        raise HandoffConflict(
            "provider state changed after preview/lock; generate a fresh plan")


def _journal_matches(journal, plan):
    _validate_journal(journal)
    for key in ("account_id", "region", "role_arn", "manifest_digest",
                "plan_digest", "load_balancer_arn"):
        expected = (plan["load_balancer"]["arn"] if key == "load_balancer_arn"
                    else plan[key])
        if journal.get(key) != expected:
            raise HandoffRefused(f"journal {key} does not match the plan")
    for key in ("sources", "inverse", "canaries",
                "canary_definitions_digest", "remote_prefix"):
        if journal.get(key) != plan.get(key):
            raise HandoffRefused(f"journal immutable {key} does not match plan")
    if journal.get("local_journal_base") != plan.get("local_journal"):
        raise HandoffRefused(
            "journal immutable local coordinate does not match plan")
    if journal.get("initial_map_digest") != plan["load_balancer"][
            "map_digest"]:
        raise HandoffRefused("journal initial map digest does not match plan")


def _validate_journal(journal):
    required = ("schema", "operation_id", "kind", "direction", "state",
                "sequence", "account_id",
                "region", "role_arn", "manifest_digest", "plan_digest",
                "load_balancer_arn", "expected_map", "sources", "transitions",
                "errors", "initial_map_digest", "expected_map_digest",
                "prior_map_digest", "remote_prefix", "local_journal_base",
                "canary_definitions_digest")
    missing = [key for key in required if key not in journal]
    if missing:
        raise HandoffRefused(
            f"handoff journal is missing {', '.join(missing)}")
    if journal.get("schema") != SCHEMA or not isinstance(
            journal.get("sources"), dict) or not journal["sources"]:
        raise HandoffRefused("handoff journal shape is invalid")
    if not re.fullmatch(
            r"[0-9a-f]{64}", journal.get(
                "canary_definitions_digest") or ""):
        raise HandoffRefused(
            "handoff journal canary definitions digest is invalid")
    validate_operation_id(journal.get("operation_id"))
    if journal.get("direction") not in ("handoff", "rollback", "rehearsal"):
        raise HandoffRefused("handoff journal direction is invalid")
    if journal.get("expected_map_digest") != digest(journal["expected_map"]):
        raise HandoffRefused("handoff journal expected-map digest is invalid")
    if (journal.get("prior_map") is not None
            and journal.get("prior_map_digest") != digest(journal["prior_map"])):
        raise HandoffRefused("handoff journal prior-map digest is invalid")
    pending = journal.get("pending_intent")
    if pending:
        if (not isinstance(pending, dict)
                or not str(pending.get("state") or "").startswith("before_")
                or journal.get("state") not in (
                    pending.get("state"), "unexpected")
                or journal.get("prior_map") is None
                or pending.get("sequence") != journal.get("sequence")):
            raise HandoffRefused(
                "handoff journal pending intent is inconsistent")
    elif str(journal.get("state") or "").startswith("before_"):
        raise HandoffRefused(
            "handoff journal transition state lacks pending intent")
    elif (journal.get("prior_map") is not None
          or journal.get("prior_map_digest") is not None):
        raise HandoffRefused(
            "handoff journal has prior map without pending intent")
    source_keys = ("allocation_id", "public_ip", "association_id",
                   "network_interface_id", "private_ip", "instance_id",
                   "target_subnet_id", "availability_zone",
                   "source_subnet_id", "source_availability_zone",
                   "source_vpc_id", "source_status", "source_attachment_id",
                   "source_device_index", "source_attachment_status")
    for az, source in journal["sources"].items():
        absent = [key for key in source_keys
                  if source.get(key) is None or source.get(key) == ""]
        if absent or source.get("availability_zone") != az:
            raise HandoffRefused(
                f"handoff journal source {az} is invalid: {', '.join(absent)}")


def _bind_boundary_from_plan(clients, plan):
    clients._boundary.bind(
        balancer_arn=plan["load_balancer"]["arn"],
        sources=plan["sources"])


def _cas_map(current, expected):
    if not _map_matches(current, expected):
        raise HandoffConflict(
            "the complete NLB mapping drifted from the journal")


def _accepted_map(journal, current):
    expected = journal.get("expected_map") or {}
    if _map_matches(current, expected):
        return dict(expected)
    if (journal.get("pending_intent")
            and _map_matches(current, expected, allow_provider_values=True)):
        return dict(current)
    prior = journal.get("prior_map") or {}
    if journal.get("pending_intent") and _map_matches(current, prior):
        return dict(prior)
    raise HandoffConflict(
        "provider NLB map matches neither side of the write-ahead transition")


def _map_matches(current, expected, compare_public=True,
                 allow_provider_values=False):
    if set(current) != set(expected):
        return False
    for az in current:
        keys = ["availability_zone", "subnet_id", "allocation_id",
                "private_ipv4", "ipv6", "source_nat_ipv6_prefix"]
        if compare_public:
            keys.append("public_ip")
        for key in keys:
            if (allow_provider_values and expected[az].get(key) is None
                    and key in ("public_ip", "private_ipv4", "ipv6",
                                "source_nat_ipv6_prefix")):
                continue
            if current[az].get(key) != expected[az].get(key):
                return False
    return True


def _source(row, subnet):
    return {
        "allocation_id": row.get("AllocationId"),
        "public_ip": row.get("PublicIp"),
        "association_id": row.get("AssociationId"),
        "network_interface_id": row.get("NetworkInterfaceId"),
        "private_ip": row.get("PrivateIpAddress"),
        "instance_id": row.get("InstanceId"),
        "target_subnet_id": subnet["id"],
        "availability_zone": subnet["availability_zone"],
        "network_border_group": row.get("NetworkBorderGroup"),
        "tags": _tags(row),
    }


def _source_matches(source, row):
    if not row:
        return False
    return all((
        row.get("AllocationId") == source.get("allocation_id"),
        row.get("AssociationId") == source.get("association_id"),
        row.get("NetworkInterfaceId") == source.get("network_interface_id"),
        row.get("PrivateIpAddress") == source.get("private_ip"),
        row.get("InstanceId") == source.get("instance_id"),
    ))


def _restored_source_matches(source, row):
    if not row:
        return False
    return all((
        row.get("AllocationId") == source.get("allocation_id"),
        bool(row.get("AssociationId")),
        row.get("NetworkInterfaceId") == source.get("network_interface_id"),
        row.get("PrivateIpAddress") == source.get("private_ip"),
        row.get("InstanceId") == source.get("instance_id"),
    ))


def _exact_source(source, row):
    if not _source_matches(source, row):
        raise HandoffConflict(
            f"source association for {source['allocation_id']} changed")


def _tags(row):
    return {tag.get("Key"): tag.get("Value")
            for tag in row.get("Tags") or [] if tag.get("Key")}


def _validate_edge(topology, groups, listeners, problems):
    specs = balancer.target_group_specs(
        topology, topology.brownfield_manifest["network"]["vpc_id"])
    for role, wanted in specs.items():
        row = groups.get(role) or {}
        for key in ("Protocol", "Port", "VpcId", "TargetType",
                    "HealthCheckProtocol", "HealthCheckPort",
                    "HealthCheckPath", "HealthCheckIntervalSeconds",
                    "HealthyThresholdCount", "UnhealthyThresholdCount",
                    "Matcher"):
            if key in wanted and row.get(key) != wanted.get(key):
                problems.append(f"{role} target group differs on {key}")
    expected_by_port = {
        443: (groups.get("api") or {}).get("TargetGroupArn"),
        80: (groups.get("certbot") or {}).get("TargetGroupArn"),
    }
    if set(row.get("Port") for row in listeners) != set(expected_by_port):
        problems.append("listeners are not exactly TCP 80 and 443")
    for row in listeners:
        actions = row.get("DefaultActions") or []
        if (row.get("Protocol") != "TCP" or len(actions) != 1
                or actions[0].get("Type") != "forward"
                or actions[0].get("TargetGroupArn") != expected_by_port.get(
                    row.get("Port"))):
            problems.append(f"listener {row.get('Port')} does not match")


def _owned_edge_tags(elbv2, topology, arns, problems=None):
    problems = problems if problems is not None else []
    expected = {
        "managed-by": "django-mojo", "mojo:project": topology.project,
        "mojo:env": topology.env, "mojo:fleet": topology.fleet,
        "mojo:role": "balancer",
    }
    descriptions = elbv2.describe_tags(
        ResourceArns=arns).get("TagDescriptions") or []
    by_arn = {
        row.get("ResourceArn"): _tags(row) for row in descriptions
        if row.get("ResourceArn")}
    summaries = {}
    expected_summary = {key: expected[key] for key in sorted(expected)}
    for arn in arns:
        tags = by_arn.get(arn) or {}
        summary = {key: tags.get(key) for key in sorted(expected)}
        summaries[arn] = summary
        if summary != expected_summary:
            problems.append(f"owned edge {arn} tags do not match the fleet")
    extras = sorted(set(by_arn) - set(arns))
    if extras:
        problems.append("DescribeTags returned undeclared edge resources")
    return summaries


def _validate_targets(topology, ec2, health, problems):
    manifest = topology.brownfield_manifest
    response = ec2.describe_instances(Filters=[
        {"Name": "tag:mojo:project", "Values": [topology.project]},
        {"Name": "tag:mojo:env", "Values": [topology.env]},
        {"Name": "tag:mojo:fleet", "Values": [topology.fleet]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instances = []
    for reservation in response.get("Reservations") or []:
        instances.extend(reservation.get("Instances") or [])
    by_name = {_tags(row).get("Name"): row.get("InstanceId")
               for row in instances}
    declared = [row["name"] for row in manifest["nodes"]["items"]
                if row["serving_target"]]
    missing = [name for name in declared if name not in by_name]
    if missing:
        problems.append(
            f"declared serving nodes are not running: {', '.join(missing)}")
    expected_api = [by_name[name] for name in declared if name in by_name]
    expected_api.extend(value for value in
                        manifest.get("compatibility_instance_ids") or []
                        if value not in expected_api)
    actual_api = [(row["id"], row["port"])
                  for row in health.get("api") or []]
    wanted_api = [(instance_id, 443) for instance_id in expected_api]
    if sorted(actual_api) != sorted(wanted_api):
        problems.append(
            "API target registration/ports are not the exact declared fleet")
    expected_certbot = expected_api[:1]
    actual_certbot = [(row["id"], row["port"])
                      for row in health.get("certbot") or []]
    wanted_certbot = [(instance_id, 80) for instance_id in expected_certbot]
    if actual_certbot != wanted_certbot:
        problems.append(
            "certbot target registration/ports are not the exact primary")


def _health(rows):
    return sorted(({
        "id": (row.get("Target") or {}).get("Id"),
        "port": (row.get("Target") or {}).get("Port"),
        "state": (row.get("TargetHealth") or {}).get("State"),
        "reason": (row.get("TargetHealth") or {}).get("Reason"),
    } for row in rows), key=lambda row: (str(row["id"]), row["port"] or 0))


def _listener_summary(rows):
    return sorted(({
        "arn": row.get("ListenerArn"), "port": row.get("Port"),
        "protocol": row.get("Protocol"),
        "target_group_arn": ((row.get("DefaultActions") or [{}])[0]).get(
            "TargetGroupArn"),
    } for row in rows), key=lambda row: row["port"] or 0)


def _etag(response):
    etag = str((response or {}).get("ETag") or "").strip('"')
    if not etag:
        raise HandoffRefused("S3 conditional write returned no ETag")
    return etag


def _bounded_error(err):
    value = str(err).replace("\n", " ")[:300]
    for marker in ("AccessKeyId", "SecretAccessKey", "SessionToken",
                   "Authorization"):
        value = value.replace(marker, "[redacted]")
    return f"{err.__class__.__name__}: {value}"


def bounded_error(err):
    return _bounded_error(err)


def _not_found(err):
    response = getattr(err, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    return code in ("404", "NoSuchKey", "NotFound")


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
