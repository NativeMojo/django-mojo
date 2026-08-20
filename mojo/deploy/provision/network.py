"""VPC, subnets, gateway, routes, the S3 endpoint, and the three security groups.

Everything downstream sits inside what this builds, which makes it the step where
a wrong decision is most expensive: subnet CIDRs and a security group's name are
immutable after creation, and nothing in this package deletes. So the two
choices that cannot be taken back are made carefully and reported as MANUAL when
they turn out to have been made differently before.

The availability zones are not "the first two the account lists". They are the
first two that actually OFFER the instance type this preset asked for. An
account can see `us-east-1e` and be unable to launch an m6i there; picking it
would surface as a failure at `run_instances`, four steps later, against a
subnet this tool has no way to remove.
"""

from mojo.deploy.provision import discover, report
from mojo.deploy.provision import spec as spec_module


STEP = "network"
SG_STEP = "security_groups"

# What we will modify in place on a resource that already exists. Everything
# else is immutable in AWS: attempting `modify_subnet_attribute` on a CIDR or
# renaming a security group does not fail gracefully, it raises — so a
# difference on any other field is a MANUAL finding, never an attempt.
MUTABLE_VPC_FIELDS = ("EnableDnsSupport", "EnableDnsHostnames")
MUTABLE_SUBNET_FIELDS = ("MapPublicIpOnLaunch",)
MUTABLE_SG_FIELDS = ("IpPermissions",)

ANYWHERE = "0.0.0.0/0"


def usable_azs(spec, observed):
    """The zones this environment may use, in a stable order.

    Stable matters: subnet 1 must land in the same zone on every run, or a
    re-apply builds a second subnet in a different zone and the DB subnet group
    ends up spanning three.
    """
    offered = set(observed.get("offered_zone_names") or [])
    zones = [z.get("ZoneName") for z in (observed.get("azs") or [])
             if z.get("ZoneName")]
    if offered:
        zones = [z for z in zones if z in offered]
    return sorted(zones)


def ensure_vpc(clients, spec, observed, apply=False):
    """The `network` DAG step: VPC, subnets, gateway, routes, S3 endpoint.

    Named for what it builds rather than for the step, because `ensure_network`
    below is the whole-area composition and the two are easy to confuse.
    """
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    ec2 = clients.get("ec2")

    zones = usable_azs(spec, observed)
    if len(zones) < spec_module.AZ_COUNT:
        findings.append(report.manual(
            STEP, "az.insufficient",
            f"only {len(zones)} availability zone(s) in {spec.region} offer "
            f"{spec.node_type}; {spec_module.AZ_COUNT} are required because an "
            f"Aurora subnet group spans two",
            f"choose an instance type offered more widely in {spec.region}, or "
            f"choose a different region"))
        return findings, actions, result
    zones = zones[:spec_module.AZ_COUNT]
    result.set("azs", zones)

    vpc_id = _ensure_vpc(ec2, spec, observed, names, findings, actions, apply)
    if not vpc_id:
        return findings, actions, result
    result.set("vpc_id", vpc_id)

    public_ids, private_ids = _ensure_subnets(
        ec2, spec, observed, names, zones, vpc_id, findings, actions, apply)
    result.set("public_subnet_ids", public_ids)
    result.set("private_subnet_ids", private_ids)

    gateway_id = _ensure_gateway(
        ec2, spec, observed, names, vpc_id, findings, actions, apply)
    result.set("internet_gateway_id", gateway_id)

    public_rt, private_rt = _ensure_routes(
        ec2, spec, observed, names, vpc_id, gateway_id, public_ids,
        private_ids, findings, actions, apply)
    result.set("public_route_table_id", public_rt)
    result.set("private_route_table_id", private_rt)

    _ensure_s3_endpoint(ec2, spec, observed, vpc_id,
                        [rt for rt in (public_rt, private_rt) if rt],
                        findings, actions, apply)
    return findings, actions, result


def _ensure_vpc(ec2, spec, observed, names, findings, actions, apply):
    existing = observed.get("vpc")
    if existing:
        if existing.get("CidrBlock") != spec_module.VPC_CIDR:
            findings.append(report.manual(
                STEP, "vpc.cidr",
                f"VPC {existing.get('VpcId')} is {existing.get('CidrBlock')}, "
                f"not {spec_module.VPC_CIDR}; a VPC's primary CIDR cannot be "
                f"changed after creation",
                "keep the existing range, or build this environment in a new "
                "VPC under a different project/env slug"))
        else:
            findings.append(report.existing(
                STEP, "vpc.ok", f"VPC {existing.get('VpcId')} is in place"))
        return existing.get("VpcId")

    findings.append(report.missing(
        STEP, "vpc.missing", f"no VPC tagged for {names['base']}",
        f"apply creates {spec_module.VPC_CIDR}"))
    actions.append(report.Action(STEP, "create", names["vpc"],
                                spec_module.VPC_CIDR))
    if not apply:
        return None

    created = report.safe(findings, STEP, "ec2.create_vpc", lambda: ec2.create_vpc(
        CidrBlock=spec_module.VPC_CIDR,
        TagSpecifications=spec_module.tag_specifications(
            spec, "network", "vpc", name=names["vpc"])))
    if not created:
        return None
    vpc_id = created["Vpc"]["VpcId"]

    # DNS hostnames are what let a node resolve the Aurora and Valkey endpoints
    # at all. These are attribute writes on a resource that already exists and
    # already carries its tags, so they are not the create-then-tag hazard the
    # TagSpecifications rule is about.
    for attribute in ("EnableDnsSupport", "EnableDnsHostnames"):
        report.safe(
            findings, STEP, f"ec2.modify_vpc_attribute.{attribute}",
            lambda name=attribute: ec2.modify_vpc_attribute(
                VpcId=vpc_id, **{name: {"Value": True}}))
    return vpc_id


def _subnet_by_name(observed, name):
    for subnet in observed.get("subnets") or []:
        if discover.tags_of(subnet).get("Name") == name:
            return subnet
    return None


def _ensure_subnets(ec2, spec, observed, names, zones, vpc_id,
                    findings, actions, apply):
    public_ids, private_ids = [], []
    plan = [
        ("public", names["public_subnets"], spec_module.PUBLIC_SUBNET_CIDRS,
         public_ids, True),
        ("private", names["private_subnets"], spec_module.PRIVATE_SUBNET_CIDRS,
         private_ids, False),
    ]
    for kind, subnet_names, cidrs, collected, public in plan:
        for index, subnet_name in enumerate(subnet_names):
            cidr = cidrs[index]
            zone = zones[index]
            existing = _subnet_by_name(observed, subnet_name)
            if existing:
                if existing.get("CidrBlock") != cidr:
                    findings.append(report.manual(
                        STEP, f"subnet.{kind}.cidr",
                        f"subnet {subnet_name} is {existing.get('CidrBlock')}, "
                        f"not {cidr}; a subnet's CIDR is fixed at creation",
                        "leave it as it is, or rebuild the environment under a "
                        "different project/env slug"))
                else:
                    findings.append(report.existing(
                        STEP, f"subnet.{kind}.ok",
                        f"{subnet_name} ({existing.get('AvailabilityZone')}) "
                        f"is in place"))
                collected.append(existing.get("SubnetId"))
                continue

            findings.append(report.missing(
                STEP, f"subnet.{kind}.missing",
                f"{subnet_name} does not exist",
                f"apply creates {cidr} in {zone}"))
            actions.append(report.Action(
                STEP, "create", subnet_name, f"{cidr} in {zone}"))
            if not apply:
                continue
            created = report.safe(
                findings, STEP, "ec2.create_subnet",
                lambda n=subnet_name, c=cidr, z=zone, k=kind: ec2.create_subnet(
                    VpcId=vpc_id, CidrBlock=c, AvailabilityZone=z,
                    TagSpecifications=spec_module.tag_specifications(
                        spec, "network", "subnet", name=n)))
            if not created:
                continue
            subnet_id = created["Subnet"]["SubnetId"]
            collected.append(subnet_id)
            if public:
                report.safe(
                    findings, STEP, "ec2.modify_subnet_attribute",
                    lambda sid=subnet_id: ec2.modify_subnet_attribute(
                        SubnetId=sid,
                        MapPublicIpOnLaunch={"Value": True}))
    return public_ids, private_ids


def _ensure_gateway(ec2, spec, observed, names, vpc_id, findings, actions, apply):
    existing = observed.get("internet_gateway")
    if existing:
        findings.append(report.existing(
            STEP, "igw.ok",
            f"internet gateway {existing.get('InternetGatewayId')} is attached"))
        return existing.get("InternetGatewayId")

    findings.append(report.missing(
        STEP, "igw.missing", "the VPC has no internet gateway attached",
        "apply creates and attaches one"))
    actions.append(report.Action(STEP, "create", names["internet_gateway"]))
    if not apply:
        return None

    created = report.safe(
        findings, STEP, "ec2.create_internet_gateway",
        lambda: ec2.create_internet_gateway(
            TagSpecifications=spec_module.tag_specifications(
                spec, "network", "internet-gateway",
                name=names["internet_gateway"])))
    if not created:
        return None
    gateway_id = created["InternetGateway"]["InternetGatewayId"]
    report.safe(findings, STEP, "ec2.attach_internet_gateway",
                lambda: ec2.attach_internet_gateway(
                    InternetGatewayId=gateway_id, VpcId=vpc_id))
    return gateway_id


def _route_table_by_name(observed, name):
    for table in observed.get("route_tables") or []:
        if discover.tags_of(table).get("Name") == name:
            return table
    return None


def _ensure_routes(ec2, spec, observed, names, vpc_id, gateway_id,
                   public_ids, private_ids, findings, actions, apply):
    resolved = []
    plan = [
        ("public", names["public_route_table"], public_ids, True),
        # Private subnets carry the database and the cache. They get no default
        # route on purpose: there is no NAT gateway in this topology, and the
        # one thing inside that genuinely needs AWS is S3, which the gateway
        # endpoint below reaches without leaving the VPC — and without the
        # 32 dollars a month and single point of failure a NAT would add.
        ("private", names["private_route_table"], private_ids, False),
    ]
    for kind, table_name, subnet_ids, wants_default in plan:
        existing = _route_table_by_name(observed, table_name)
        table_id = existing.get("RouteTableId") if existing else None
        if existing:
            findings.append(report.existing(
                STEP, f"routes.{kind}.ok", f"{table_name} is in place"))
        else:
            findings.append(report.missing(
                STEP, f"routes.{kind}.missing", f"{table_name} does not exist",
                "apply creates it and associates its subnets"))
            actions.append(report.Action(STEP, "create", table_name))
            if apply:
                created = report.safe(
                    findings, STEP, "ec2.create_route_table",
                    lambda n=table_name: ec2.create_route_table(
                        VpcId=vpc_id,
                        TagSpecifications=spec_module.tag_specifications(
                            spec, "network", "route-table", name=n)))
                table_id = created["RouteTable"]["RouteTableId"] if created else None

        if table_id and wants_default and gateway_id:
            has_default = any(
                route.get("DestinationCidrBlock") == ANYWHERE
                for route in ((existing or {}).get("Routes") or []))
            if not has_default:
                actions.append(report.Action(
                    STEP, "create", f"{table_name} default route", ANYWHERE))
                if apply:
                    report.safe(
                        findings, STEP, "ec2.create_route",
                        lambda tid=table_id: ec2.create_route(
                            RouteTableId=tid, DestinationCidrBlock=ANYWHERE,
                            GatewayId=gateway_id))

        if table_id and apply:
            associated = {
                assoc.get("SubnetId")
                for assoc in ((existing or {}).get("Associations") or [])}
            for subnet_id in subnet_ids:
                if not subnet_id or subnet_id in associated:
                    continue
                actions.append(report.Action(
                    STEP, "attach", table_name, subnet_id))
                report.safe(
                    findings, STEP, "ec2.associate_route_table",
                    lambda sid=subnet_id, tid=table_id:
                        ec2.associate_route_table(RouteTableId=tid,
                                                  SubnetId=sid))
        resolved.append(table_id)
    return resolved[0], resolved[1]


def _ensure_s3_endpoint(ec2, spec, observed, vpc_id, route_table_ids,
                        findings, actions, apply):
    service = f"com.amazonaws.{spec.region}.s3"
    for endpoint in observed.get("vpc_endpoints") or []:
        if endpoint.get("ServiceName") == service:
            findings.append(report.existing(
                STEP, "endpoint.s3.ok",
                f"S3 gateway endpoint {endpoint.get('VpcEndpointId')} is in place"))
            return endpoint.get("VpcEndpointId")

    findings.append(report.missing(
        STEP, "endpoint.s3.missing", "no S3 gateway endpoint in the VPC",
        "apply creates one so config and secrets reads never leave the VPC"))
    actions.append(report.Action(STEP, "create", service))
    if not apply or not route_table_ids:
        return None
    created = report.safe(
        findings, STEP, "ec2.create_vpc_endpoint",
        lambda: ec2.create_vpc_endpoint(
            VpcId=vpc_id, ServiceName=service, VpcEndpointType="Gateway",
            RouteTableIds=list(route_table_ids),
            TagSpecifications=spec_module.tag_specifications(
                spec, "network", "vpc-endpoint")))
    return created["VpcEndpoint"]["VpcEndpointId"] if created else None


# ── security groups ─────────────────────────────────────────────────────────

def wanted_rules(spec, group_ids):
    """The ingress every group should have, as IpPermissions.

    Rule descriptions are ASCII only. EC2 accepts a narrow character set there
    and rejects the rest with a validation error that names neither the field
    nor the character, which is a genuinely unpleasant twenty minutes.
    """
    node_id = group_ids.get("node")
    rules = {
        "node": [
            _cidr_rule("tcp", 443, ANYWHERE, "https from anywhere"),
            _cidr_rule("tcp", 80, ANYWHERE,
                       "http from anywhere for acme http-01"),
        ],
        "rds": [],
        "cache": [],
    }
    for cidr in spec.admin_cidrs or ():
        rules["node"].append(
            _cidr_rule("tcp", 22, cidr, "ssh from an operator network"))
    if node_id:
        rules["rds"].append(_group_rule(
            "tcp", spec_module.DB_PORT, node_id, "postgres from the nodes"))
        rules["cache"].append(_group_rule(
            "tcp", spec_module.CACHE_PORT, node_id, "valkey from the nodes"))
    return rules


def _cidr_rule(protocol, port, cidr, description):
    return {"IpProtocol": protocol, "FromPort": port, "ToPort": port,
            "IpRanges": [{"CidrIp": cidr, "Description": description}]}


def _group_rule(protocol, port, group_id, description):
    return {"IpProtocol": protocol, "FromPort": port, "ToPort": port,
            "UserIdGroupPairs": [{"GroupId": group_id,
                                  "Description": description}]}


def _rule_keys(permissions):
    """A comparable identity for an ingress rule: protocol, ports, and source.

    Descriptions are deliberately excluded. AWS lets a description be edited
    and an operator may well have improved one; treating that as drift would
    make every run report a change it then could not make without revoking the
    rule, which this package does not do.
    """
    keys = set()
    for rule in permissions or []:
        base = (rule.get("IpProtocol"), rule.get("FromPort"), rule.get("ToPort"))
        for row in rule.get("IpRanges") or []:
            keys.add(base + ("cidr", row.get("CidrIp")))
        for row in rule.get("Ipv6Ranges") or []:
            keys.add(base + ("cidr6", row.get("CidrIpv6")))
        for row in rule.get("UserIdGroupPairs") or []:
            keys.add(base + ("group", row.get("GroupId")))
    return keys


def ensure_security_groups(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    ec2 = clients.get("ec2")

    vpc_id = observed.get("vpc_id") or (observed.get("vpc") or {}).get("VpcId")
    if not vpc_id and apply:
        findings.append(report.missing(
            SG_STEP, "sg.no_vpc", "no VPC to put security groups in",
            "the network step has to succeed first"))
        return findings, actions, result
    # With no VPC and no intent to create anything, keep going: a dry run
    # against an empty account should still say the three groups are missing
    # and what apply would build. The ingress rules are skipped further down
    # because they reference the node group's id, which does not exist yet.

    existing = observed.get("security_groups") or {}
    group_ids = {}
    wanted_names = {"node": names["node_sg"], "rds": names["rds_sg"],
                    "cache": names["cache_sg"]}
    descriptions = {
        "node": "django-mojo application nodes",
        "rds": "django-mojo aurora cluster",
        "cache": "django-mojo valkey replication group",
    }

    # Two passes. The node group's id is the source for the database and cache
    # rules, so it has to exist before their rules can be described at all.
    for role in ("node", "rds", "cache"):
        found = existing.get(role)
        if found:
            if found.get("GroupName") != wanted_names[role]:
                findings.append(report.manual(
                    SG_STEP, f"sg.{role}.name",
                    f"security group {found.get('GroupId')} is named "
                    f"{found.get('GroupName')!r}, not {wanted_names[role]!r}; "
                    f"a security group cannot be renamed",
                    "leave the name as it is — it is cosmetic — or create a "
                    "replacement group and move its members by hand"))
            group_ids[role] = found.get("GroupId")
            continue

        findings.append(report.missing(
            SG_STEP, f"sg.{role}.missing",
            f"{wanted_names[role]} does not exist",
            "apply creates it with the ingress this topology needs"))
        actions.append(report.Action(SG_STEP, "create", wanted_names[role]))
        if not apply:
            continue
        created = report.safe(
            findings, SG_STEP, "ec2.create_security_group",
            lambda r=role: ec2.create_security_group(
                GroupName=wanted_names[r], Description=descriptions[r],
                VpcId=vpc_id,
                TagSpecifications=spec_module.tag_specifications(
                    spec, r, "security-group", name=wanted_names[r])))
        if created:
            group_ids[role] = created["GroupId"]

    result.update(**{f"{role}_sg_id": group_id
                     for role, group_id in group_ids.items()})

    wanted = wanted_rules(spec, group_ids)
    for role, permissions in wanted.items():
        group_id = group_ids.get(role)
        if not group_id:
            continue
        current = ((existing.get(role) or {}).get("IpPermissions")) or []
        have = _rule_keys(current)
        want = _rule_keys(permissions)
        adding = [rule for rule in permissions
                  if not _rule_keys([rule]).issubset(have)]
        if adding:
            findings.append(report.drift(
                SG_STEP, f"sg.{role}.rules",
                f"{wanted_names[role]} is missing {len(adding)} ingress rule(s)",
                "apply adds them"))
            actions.append(report.Action(
                SG_STEP, "modify", wanted_names[role],
                f"{len(adding)} ingress rule(s)"))
            if apply:
                report.safe(
                    findings, SG_STEP,
                    "ec2.authorize_security_group_ingress",
                    lambda gid=group_id, rules=adding:
                        ec2.authorize_security_group_ingress(
                            GroupId=gid, IpPermissions=rules))
        extra = have - want
        if extra:
            # Removing an ingress rule is `revoke_security_group_ingress`, which
            # this package will not call. Say what is there and let a human
            # decide — an extra rule is often deliberate.
            findings.append(report.manual(
                SG_STEP, f"sg.{role}.extra",
                f"{wanted_names[role]} carries {len(extra)} ingress rule(s) "
                f"this topology does not define",
                "review them and remove any that are not wanted — this tool "
                "never revokes a rule"))
        if not adding and not extra:
            findings.append(report.existing(
                SG_STEP, f"sg.{role}.ok",
                f"{wanted_names[role]} ingress matches the topology"))

    return findings, actions, result


def ensure_network(clients, spec, observed, apply=False):
    """Both network steps in order, for a caller converging this area alone.

    The DAG runs the two separately so a security-group failure does not read as
    a VPC failure; the portal's per-section converge wants them as one unit.
    """
    findings, actions, result = ensure_vpc(clients, spec, observed, apply)
    merged = dict(observed or {})
    merged.update(result.as_dict())
    more_findings, more_actions, more_result = ensure_security_groups(
        clients, spec, merged, apply)
    findings.extend(more_findings)
    actions.extend(more_actions)
    result.update(**more_result.as_dict())
    return findings, actions, result
