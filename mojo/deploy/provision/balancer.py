"""The network load balancer, its target groups, and its listeners.

Built only when the topology actually wants one — `spec.wants_balancer` is false
on the micro preset, and an NLB in front of a single node buys an extra hop and
twenty dollars a month for no availability at all. Growing out of micro is a
re-run of `apply` at the new preset: this step creates the balancer and registers
the nodes that already exist, so the single-node shape is not a dead end.

THE PROTOCOL SPLIT IS THE THING TO GET RIGHT. A target group has a `Protocol`
(how traffic is forwarded) and a `HealthCheckProtocol` (how health is measured),
and they are separate fields with separate legal values. Behind an NLB the
forwarding protocol is TCP — for both listeners, including the one carrying
HTTPS — while the health check speaks HTTPS on 443 and HTTP on 80. Writing
`Protocol="HTTPS"` on an NLB target group is not a stricter version of the same
thing; `CreateTargetGroup` rejects it outright.

The :80 group holds ONE node. Certbot's HTTP-01 challenge has to land on the box
that asked for the certificate, and spreading :80 across the fleet means it lands
somewhere else four times out of five.
"""

from mojo.deploy.provision import discover, report
from mojo.deploy.provision import spec as spec_module


STEP = "balancer"

# Fixed at creation by AWS. A difference on any of these is MANUAL: there is no
# ModifyTargetGroup call that accepts them, and sending one raises.
IMMUTABLE_TARGET_GROUP_FIELDS = ("Protocol", "Port", "TargetType", "VpcId")
# What ModifyTargetGroup does accept.
MUTABLE_TARGET_GROUP_FIELDS = ("HealthCheckProtocol", "HealthCheckPort",
                               "HealthCheckPath", "HealthCheckIntervalSeconds",
                               "HealthyThresholdCount",
                               "UnhealthyThresholdCount", "Matcher")

HEALTH_PATH = "/api/version"


def target_group_specs(spec, vpc_id):
    """The two groups, as the exact shape `CreateTargetGroup` takes."""
    return {
        "api": {
            "Name": spec_module.names(spec)["api_target_group"],
            "Protocol": "TCP",
            "Port": 443,
            "VpcId": vpc_id,
            "TargetType": "instance",
            "HealthCheckProtocol": "HTTPS",
            "HealthCheckPort": "traffic-port",
            "HealthCheckPath": HEALTH_PATH,
            "HealthCheckIntervalSeconds": 30,
            "HealthyThresholdCount": 3,
            "UnhealthyThresholdCount": 3,
        },
        "certbot": {
            "Name": spec_module.names(spec)["certbot_target_group"],
            "Protocol": "TCP",
            "Port": 80,
            "VpcId": vpc_id,
            "TargetType": "instance",
            "HealthCheckProtocol": "HTTP",
            "HealthCheckPort": "traffic-port",
            "HealthCheckPath": HEALTH_PATH,
            "HealthCheckIntervalSeconds": 30,
            "HealthyThresholdCount": 3,
            "UnhealthyThresholdCount": 3,
            # A node that answers :80 with a redirect to :443 is healthy for
            # this purpose — it is up, and the challenge will reach it.
            "Matcher": {"HttpCode": "200-399"},
        },
    }


def ensure_balancer(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)

    if not spec_module.wants_balancer(spec):
        findings.append(report.existing(
            STEP, "balancer.not_wanted",
            f"the {spec.preset} preset runs {spec.node_count} node(s) and does "
            f"not use a load balancer"))
        return findings, actions, result

    elbv2 = clients.get("elbv2")
    ec2 = clients.get("ec2")
    vpc_id = observed.get("vpc_id") or (observed.get("vpc") or {}).get("VpcId")
    subnet_ids = list(observed.get("public_subnet_ids") or [])
    instance_ids = [i for i in (observed.get("instance_ids") or []) if i]

    wanted = target_group_specs(spec, vpc_id)
    group_arns = _ensure_target_groups(
        elbv2, spec, observed, wanted, findings, actions, apply)
    result.set("target_group_arns", group_arns)

    balancer = observed.get("balancer")
    if balancer:
        findings.append(report.existing(
            STEP, "balancer.ok",
            f"{names['balancer']} is {balancer.get('State', {}).get('Code')} "
            f"at {balancer.get('DNSName')}"))
        if (balancer.get("State") or {}).get("Code") == "provisioning":
            findings.append(report.pending(
                STEP, "balancer.provisioning",
                f"{names['balancer']} is still provisioning"))
    else:
        findings.append(report.missing(
            STEP, "balancer.missing",
            f"network load balancer {names['balancer']} does not exist",
            f"apply creates it across {len(subnet_ids) or spec_module.AZ_COUNT} "
            f"public subnet(s) with a fixed address in each"))
        actions.append(report.Action(STEP, "create", names["balancer"]))
        if not apply:
            return findings, actions, result
        if not subnet_ids:
            findings.append(report.missing(
                STEP, "balancer.no_subnets",
                "the public subnets are not resolved yet",
                "let the network step run first"))
            return findings, actions, result
        mappings = _subnet_mappings(ec2, spec, observed, subnet_ids,
                                    findings, actions)
        created = report.safe(
            findings, STEP, "elbv2.create_load_balancer",
            lambda: elbv2.create_load_balancer(
                Name=names["balancer"], Type="network",
                Scheme="internet-facing", SubnetMappings=mappings,
                Tags=spec_module.tag_list(spec, "balancer",
                                          name=names["balancer"])))
        if not created:
            return findings, actions, result
        balancer = (created.get("LoadBalancers") or [{}])[0]
        findings.append(report.pending(
            STEP, "balancer.provisioning",
            f"{names['balancer']} was created and is still provisioning"))

    balancer_arn = (balancer or {}).get("LoadBalancerArn")
    result.set("balancer_arn", balancer_arn)
    result.set("balancer_dns", (balancer or {}).get("DNSName"))
    result.set("balancer_zone_id", (balancer or {}).get("CanonicalHostedZoneId"))

    if balancer_arn:
        _ensure_attributes(elbv2, spec, balancer_arn, findings, actions, apply)
        _ensure_listeners(elbv2, spec, observed, balancer_arn, group_arns,
                          findings, actions, apply)
    _ensure_targets(elbv2, spec, observed, group_arns, instance_ids,
                    findings, actions, apply)
    return findings, actions, result


def _ensure_target_groups(elbv2, spec, observed, wanted, findings, actions,
                          apply):
    arns = {}
    existing = observed.get("target_groups") or {}
    for role, request in wanted.items():
        found = existing.get(role)
        if not found:
            findings.append(report.missing(
                STEP, f"target_group.{role}.missing",
                f"target group {request['Name']} does not exist",
                f"apply creates it: {request['Protocol']} on port "
                f"{request['Port']}, health checked over "
                f"{request['HealthCheckProtocol']}"))
            actions.append(report.Action(
                STEP, "create", request["Name"],
                f"{request['Protocol']}:{request['Port']} health "
                f"{request['HealthCheckProtocol']}{request['HealthCheckPath']}"))
            if apply and request.get("VpcId"):
                created = report.safe(
                    findings, STEP, "elbv2.create_target_group",
                    lambda r=dict(request), name=request["Name"]:
                        elbv2.create_target_group(
                            Tags=spec_module.tag_list(spec, "balancer",
                                                      name=name), **r))
                if created:
                    arns[role] = (created.get("TargetGroups")
                                  or [{}])[0].get("TargetGroupArn")
            continue

        arns[role] = found.get("TargetGroupArn")
        frozen = [field for field in IMMUTABLE_TARGET_GROUP_FIELDS
                  if field in request
                  and found.get(field) is not None
                  and str(found.get(field)) != str(request[field])]
        if frozen:
            findings.append(report.manual(
                STEP, f"target_group.{role}.immutable",
                f"{request['Name']} differs on {', '.join(frozen)}, which AWS "
                f"fixes at creation — "
                + "; ".join(f"{field} is {found.get(field)!r}, this topology "
                            f"declares {request[field]!r}" for field in frozen),
                "create a replacement target group under a different name and "
                "move the listener to it; this tool will not delete the old "
                "one"))
            continue

        changes = {field: request[field]
                   for field in MUTABLE_TARGET_GROUP_FIELDS
                   if field in request and found.get(field) != request[field]}
        if changes:
            findings.append(report.drift(
                STEP, f"target_group.{role}.health_check",
                f"{request['Name']} differs on {', '.join(sorted(changes))}",
                "apply modifies it in place"))
            actions.append(report.Action(STEP, "modify", request["Name"],
                                         ", ".join(sorted(changes))))
            if apply:
                report.safe(
                    findings, STEP, "elbv2.modify_target_group",
                    lambda arn=found.get("TargetGroupArn"), c=dict(changes):
                        elbv2.modify_target_group(TargetGroupArn=arn, **c))
        else:
            findings.append(report.existing(
                STEP, f"target_group.{role}.ok",
                f"{request['Name']} matches the topology"))
    return arns


def _subnet_mappings(ec2, spec, observed, subnet_ids, findings, actions):
    """One fixed address per subnet.

    An NLB without elastic IPs gets an address AWS may change; DNS pointed at
    the name is fine, but anything holding an IP allowlist upstream is not.
    Existing addresses are adopted only when already tagged for this project and
    environment.
    """
    mappings = []
    available = []
    for address in observed.get("addresses") or []:
        tags = discover.tags_of(address)
        if spec_module.owns(tags, spec, role="balancer") and not address.get(
                "AssociationId"):
            available.append(address)

    for index, subnet_id in enumerate(subnet_ids):
        if index < len(available):
            mappings.append({"SubnetId": subnet_id,
                             "AllocationId": available[index].get("AllocationId")})
            continue
        actions.append(report.Action(STEP, "create",
                                     f"{subnet_id} elastic IP"))
        allocated = report.safe(
            findings, STEP, "ec2.allocate_address",
            lambda: ec2.allocate_address(
                Domain="vpc",
                TagSpecifications=spec_module.tag_specifications(
                    spec, "balancer", "elastic-ip",
                    name=spec_module.names(spec)["balancer"])))
        if allocated:
            mappings.append({"SubnetId": subnet_id,
                             "AllocationId": allocated.get("AllocationId")})
    return mappings


def _ensure_attributes(elbv2, spec, balancer_arn, findings, actions, apply):
    wanted = {"load_balancing.cross_zone.enabled": "true"}
    # Deletion protection on production only. On a dev environment it is the
    # thing that makes tearing down a test account annoying, and this package
    # cannot delete the balancer either way.
    if spec.env in ("prod", "production"):
        wanted["deletion_protection.enabled"] = "true"
    actions.append(report.Action(STEP, "modify",
                                 spec_module.names(spec)["balancer"],
                                 ", ".join(sorted(wanted))))
    if not apply:
        return
    report.safe(
        findings, STEP, "elbv2.modify_load_balancer_attributes",
        lambda: elbv2.modify_load_balancer_attributes(
            LoadBalancerArn=balancer_arn,
            Attributes=[{"Key": key, "Value": value}
                        for key, value in sorted(wanted.items())]))


def _ensure_listeners(elbv2, spec, observed, balancer_arn, group_arns,
                      findings, actions, apply):
    present = {listener.get("Port")
               for listener in (observed.get("listeners") or [])}
    for port, role in ((443, "api"), (80, "certbot")):
        if port in present:
            findings.append(report.existing(
                STEP, f"listener.{port}.ok",
                f"TCP:{port} forwards to the {role} target group"))
            continue
        arn = group_arns.get(role)
        findings.append(report.missing(
            STEP, f"listener.{port}.missing",
            f"the balancer has no TCP:{port} listener",
            f"apply creates one forwarding to the {role} target group"))
        actions.append(report.Action(STEP, "create", f"TCP:{port}", role))
        if not apply or not arn:
            continue
        report.safe(
            findings, STEP, "elbv2.create_listener",
            lambda p=port, a=arn: elbv2.create_listener(
                LoadBalancerArn=balancer_arn, Protocol="TCP", Port=p,
                DefaultActions=[{"Type": "forward", "TargetGroupArn": a}]))


def _ensure_targets(elbv2, spec, observed, group_arns, instance_ids,
                    findings, actions, apply):
    """Register what is missing, and only what is missing.

    Deregistering is `deregister_targets`, which this package will not call, so
    a node that has left the fleet is reported rather than removed.
    """
    registered = observed.get("targets") or {}
    plan = [("api", instance_ids, 443),
            # Certbot's challenge must land on one predictable node.
            ("certbot", instance_ids[:1], 80)]
    for role, wanted_ids, port in plan:
        arn = group_arns.get(role)
        if not arn:
            continue
        current = {(row.get("Target") or {}).get("Id")
                   for row in (registered.get(role) or [])}
        adding = [value for value in wanted_ids if value not in current]
        if adding:
            findings.append(report.drift(
                STEP, f"targets.{role}.missing",
                f"{len(adding)} node(s) are not registered in the {role} "
                f"target group",
                "apply registers them"))
            actions.append(report.Action(
                STEP, "register", role, ", ".join(adding)))
            if apply:
                report.safe(
                    findings, STEP, "elbv2.register_targets",
                    lambda a=arn, ids=list(adding), p=port:
                        elbv2.register_targets(
                            TargetGroupArn=a,
                            Targets=[{"Id": value, "Port": p}
                                     for value in ids]))
        extra = current - set(wanted_ids)
        if extra:
            findings.append(report.manual(
                STEP, f"targets.{role}.extra",
                f"the {role} target group carries {len(extra)} target(s) this "
                f"topology does not declare: {', '.join(sorted(extra))}",
                "deregister them yourself if they are gone — this tool never "
                "removes a target"))
        if not adding and not extra and wanted_ids:
            findings.append(report.existing(
                STEP, f"targets.{role}.ok",
                f"the {role} target group carries the expected node(s)"))
