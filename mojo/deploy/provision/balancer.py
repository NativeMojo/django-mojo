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

import json

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

HEALTH_PATH = spec_module.HEALTH_PATH_DEFAULT


def target_group_specs(spec, vpc_id):
    """The two groups, as the exact shape `CreateTargetGroup` takes."""
    api_health_path = getattr(spec, "api_health_path", None) or HEALTH_PATH
    certbot_health_path = (
        getattr(spec, "certbot_health_path", None) or HEALTH_PATH)
    return {
        "api": {
            "Name": spec_module.names(spec)["api_target_group"],
            "Protocol": "TCP",
            "Port": 443,
            "VpcId": vpc_id,
            "TargetType": "instance",
            "HealthCheckProtocol": "HTTPS",
            "HealthCheckPort": "traffic-port",
            "HealthCheckPath": api_health_path,
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
            "HealthCheckPath": certbot_health_path,
            "HealthCheckIntervalSeconds": 30,
            "HealthyThresholdCount": 3,
            "UnhealthyThresholdCount": 3,
            # A node that answers :80 with a redirect to :443 is healthy for
            # this purpose — it is up, and the challenge will reach it.
            "Matcher": {"HttpCode": "200-399"},
        },
    }


def target_group_attributes(spec):
    """Desired brownfield-only attributes, absent from managed topology."""
    if not spec.fleet:
        return {}
    answer = {}
    for role, field in (("api", "api_preserve_client_ip"),
                        ("certbot", "certbot_preserve_client_ip")):
        value = getattr(spec, field, None)
        if value is not None:
            answer[role] = {
                "preserve_client_ip.enabled": "true" if value else "false"}
    return answer


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
    subnet_ids = (list(spec.nlb_subnet_ids) if spec.fleet else
                  list(observed.get("public_subnet_ids") or []))
    if spec.fleet:
        instance_ids = [
            row.get("instance_id") for row in observed.get("node_records") or []
            if row.get("instance_id") and row.get("serving_target")]
        compatibility_ids = list(spec.compatibility_instance_ids or ())
        instance_ids.extend(value for value in compatibility_ids
                            if value not in instance_ids)
        result.set("compatibility_target_ids", compatibility_ids)
        result.set("serving_instance_ids", list(instance_ids))
    else:
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
        preserved_mode = bool(getattr(spec, "nlb_eip_allocations", None))
        findings.append(report.missing(
            STEP, "balancer.missing",
            f"network load balancer {names['balancer']} does not exist",
            f"apply creates it across {len(subnet_ids) or spec_module.AZ_COUNT} "
            f"public subnet(s) with "
            f"{'temporary AWS addresses' if preserved_mode else 'a fixed address in each'}"))
        nlb_security_group_id = getattr(
            spec, "nlb_security_group_id", None) if spec.fleet else None
        create_detail = (json.dumps(
            {"SecurityGroups": [nlb_security_group_id]}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=True)
            if nlb_security_group_id else None)
        actions.append(report.Action(
            STEP, "create", names["balancer"], create_detail))
        if not apply:
            if spec.fleet:
                if not preserved_mode:
                    _subnet_mappings(ec2, spec, observed, subnet_ids,
                                     findings, actions, apply=False)
                _preview_new_balancer(spec, observed, instance_ids,
                                      findings, actions)
            return findings, actions, result
        if not subnet_ids:
            findings.append(report.missing(
                STEP, "balancer.no_subnets",
                "the public subnets are not resolved yet",
                "let the network step run first"))
            return findings, actions, result
        mappings = []
        if not preserved_mode:
            mappings = _subnet_mappings(ec2, spec, observed, subnet_ids,
                                        findings, actions)
            if len(mappings) != len(subnet_ids):
                findings.append(report.Finding(
                    STEP, report.BLIND, "balancer.address_mappings",
                    f"resolved {len(mappings)} of {len(subnet_ids)} exact NLB "
                    f"address mappings",
                    "fix the failed allocation and re-run; the successfully "
                    "tagged address is retained, but no one-AZ NLB was created"))
                return findings, actions, result
        placement = ({"Subnets": list(subnet_ids)} if preserved_mode else
                     {"SubnetMappings": mappings})
        security_groups = ({"SecurityGroups": [nlb_security_group_id]}
                           if nlb_security_group_id else {})
        created = report.safe(
            findings, STEP, "elbv2.create_load_balancer",
            lambda: elbv2.create_load_balancer(
                Name=names["balancer"], Type="network",
                Scheme="internet-facing", **placement, **security_groups,
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
        _ensure_attributes(elbv2, spec, observed, balancer_arn, findings,
                           actions, apply)
        _ensure_listeners(elbv2, spec, observed, balancer_arn, group_arns,
                          findings, actions, apply)
    _ensure_targets(elbv2, spec, observed, group_arns, instance_ids,
                    findings, actions, apply)
    return findings, actions, result


def _ensure_target_groups(elbv2, spec, observed, wanted, findings, actions,
                          apply):
    arns = {}
    existing = observed.get("target_groups") or {}
    wanted_attributes = target_group_attributes(spec)
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
            attributes = wanted_attributes.get(role)
            if attributes:
                actions.append(report.Action(
                    STEP, "modify", request["Name"], json.dumps(
                        attributes, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=True)))
            if apply and request.get("VpcId"):
                created = report.safe(
                    findings, STEP, "elbv2.create_target_group",
                    lambda r=dict(request), name=request["Name"]:
                        elbv2.create_target_group(
                            Tags=spec_module.tag_list(spec, "balancer",
                                                      name=name), **r))
                if created:
                    arn = (created.get("TargetGroups")
                           or [{}])[0].get("TargetGroupArn")
                    if arn and attributes:
                        changed = report.safe(
                            findings, STEP,
                            "elbv2.modify_target_group_attributes",
                            lambda arn=arn, attributes=dict(attributes):
                            elbv2.modify_target_group_attributes(
                                TargetGroupArn=arn,
                                Attributes=[
                                    {"Key": key, "Value": value}
                                    for key, value in sorted(
                                        attributes.items())]))
                        if changed is not None:
                            arns[role] = arn
                    elif arn:
                        arns[role] = arn
            continue

        if not spec.fleet:
            arns[role] = found.get("TargetGroupArn")
        frozen = [field for field in IMMUTABLE_TARGET_GROUP_FIELDS
                  if field in request
                  and found.get(field) is not None
                  and str(found.get(field)) != str(request[field])]
        if frozen:
            message = (
                f"{request['Name']} differs on {', '.join(frozen)}, which AWS "
                f"fixes at creation — "
                + "; ".join(f"{field} is {found.get(field)!r}, this topology "
                            f"declares {request[field]!r}" for field in frozen))
            remedy = (
                "create a replacement target group under a different name and "
                "move the listener to it; this tool will not delete the old one")
            if spec.fleet:
                findings.append(report.Finding(
                    STEP, report.BLIND, f"target_group.{role}.immutable",
                    message, remedy))
            else:
                findings.append(report.manual(
                    STEP, f"target_group.{role}.immutable", message, remedy))
            continue

        arns[role] = found.get("TargetGroupArn")

        changes = {field: request[field]
                   for field in MUTABLE_TARGET_GROUP_FIELDS
                   if field in request and found.get(field) != request[field]}
        if changes:
            findings.append(report.drift(
                STEP, f"target_group.{role}.health_check",
                f"{request['Name']} differs on {', '.join(sorted(changes))}",
                "apply modifies it in place"))
            detail = json.dumps(changes, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True)
            actions.append(report.Action(
                STEP, "modify", request["Name"], detail))
            if apply:
                report.safe(
                    findings, STEP, "elbv2.modify_target_group",
                    lambda arn=found.get("TargetGroupArn"), c=dict(changes):
                        elbv2.modify_target_group(TargetGroupArn=arn, **c))
        else:
            findings.append(report.existing(
                STEP, f"target_group.{role}.ok",
                f"{request['Name']} matches the topology"))
        desired_attributes = wanted_attributes.get(role)
        if desired_attributes:
            current_attributes = found.get("TargetGroupAttributes") or {}
            attribute_changes = {
                key: value for key, value in desired_attributes.items()
                if current_attributes.get(key) != value}
            if attribute_changes:
                findings.append(report.drift(
                    STEP, f"target_group.{role}.attributes",
                    f"{request['Name']} differs on "
                    f"{', '.join(sorted(attribute_changes))}",
                    "apply modifies the exact owned target group in place"))
                actions.append(report.Action(
                    STEP, "modify", request["Name"], json.dumps(
                        attribute_changes, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=True)))
                if apply:
                    report.safe(
                        findings, STEP,
                        "elbv2.modify_target_group_attributes",
                        lambda arn=found.get("TargetGroupArn"),
                        changes=dict(attribute_changes):
                        elbv2.modify_target_group_attributes(
                            TargetGroupArn=arn,
                            Attributes=[
                                {"Key": key, "Value": value}
                                for key, value in sorted(changes.items())]))
    return arns


def _subnet_mappings(ec2, spec, observed, subnet_ids, findings, actions,
                     apply=True):
    """One fixed address per subnet.

    An NLB without elastic IPs gets an address AWS may change; DNS pointed at
    the name is fine, but anything holding an IP allowlist upstream is not.
    Existing addresses are adopted only when already tagged for this project and
    environment.

    The two modes key that adoption differently and must not share one pool.
    Managed adopts an owned unassociated balancer address BY POSITION and tags
    a new one `<balancer>`: every managed address carries that same bare Name,
    so a Name-keyed pool collapses the pair a two-AZ environment holds and
    silently replaces one of its public IPs. A brownfield fleet adopts by the
    exact `<balancer>:<subnet-id>` Name `brownfield_discover` builds, because
    there the address-to-subnet pairing is part of the discovered contract.
    Ownership is decided by `spec_module.owns` on tags alone in both modes and
    never by the Name, so an address left behind by either naming generation is
    still adoptable.
    """
    mappings = []
    balancer_name = spec_module.names(spec)["balancer"]
    owned = []
    for address in observed.get("addresses") or []:
        tags = discover.tags_of(address)
        if not spec_module.owns(tags, spec, role="balancer"):
            continue
        if address.get("AssociationId"):
            continue
        # `owns` only checks `mojo:fleet` when this spec has one, so a managed
        # spec matches a brownfield fleet's balancer EIP on project and env
        # alone — and the handoff leaves exactly those unassociated on purpose.
        if not spec.fleet and tags.get("mojo:fleet"):
            continue
        owned.append(address)

    available = {discover.tags_of(row).get("Name"): row
                 for row in owned} if spec.fleet else {}
    subnet_rows = {row["id"]: row for row in spec.brownfield_manifest[
        "network"]["public_subnets"]} if spec.fleet else {}
    for index, subnet_id in enumerate(subnet_ids):
        if spec.fleet:
            address_name = f"{balancer_name}:{subnet_id}"
            adopted = available.get(address_name)
        else:
            address_name = balancer_name
            adopted = owned[index] if index < len(owned) else None
        if adopted:
            mappings.append({"SubnetId": subnet_id,
                             "AllocationId": adopted.get("AllocationId")})
            continue
        actions.append(report.Action(STEP, "create",
                                     f"{subnet_id} elastic IP"))
        if not apply:
            continue
        allocated = report.safe(
            findings, STEP, "ec2.allocate_address",
            lambda name=address_name: ec2.allocate_address(
                Domain="vpc",
                **({"NetworkBorderGroup": subnet_rows[subnet_id][
                    "network_border_group"]} if spec.fleet else {}),
                TagSpecifications=spec_module.tag_specifications(
                    spec, "balancer", "elastic-ip",
                    name=name)))
        if allocated:
            mappings.append({"SubnetId": subnet_id,
                             "AllocationId": allocated.get("AllocationId")})
    return mappings


def _preview_new_balancer(spec, observed, instance_ids, findings, actions):
    """Describe mutations hidden behind an NLB that does not exist yet.

    The normal managed planner historically stops after previewing NLB
    creation.  Brownfield apply must be stricter: its reviewed preview is the
    mutation boundary, so listeners and target registration must be visible
    before the first call is authorized even when their AWS parents will only
    exist later in the same apply.
    """
    for port, role in ((443, "api"), (80, "certbot")):
        actions.append(report.Action(STEP, "create", f"TCP:{port}", role))
    actions.append(report.Action(
        STEP, "modify", spec_module.names(spec)["balancer"],
        "cross-zone load balancing and deletion protection"))

    declarations = [row for row in spec.node_declarations
                    if row.get("serving_target")]
    api_targets = list(instance_ids) or [row["name"] for row in declarations]
    certbot_targets = api_targets[:1]
    for role, targets in (("api", api_targets),
                          ("certbot", certbot_targets)):
        if targets:
            actions.append(report.Action(
                STEP, "register", role, ", ".join(targets)))


def _ensure_attributes(elbv2, spec, observed, balancer_arn, findings, actions,
                       apply):
    wanted = {"load_balancing.cross_zone.enabled": "true"}
    # Deletion protection on production only. On a dev environment it is the
    # thing that makes tearing down a test account annoying, and this package
    # cannot delete the balancer either way.
    if spec.env in ("prod", "production"):
        wanted["deletion_protection.enabled"] = "true"

    current = observed.get("balancer_attributes") or {}
    changed = {key: value for key, value in wanted.items()
               if current.get(key) != value}
    if not changed:
        findings.append(report.existing(
            STEP, "balancer.attributes.ok",
            "cross-zone load balancing and deletion protection match"))
        return

    findings.append(report.drift(
        STEP, "balancer.attributes",
        f"the balancer differs on {', '.join(sorted(changed))}",
        "apply sets them in place"))
    actions.append(report.Action(STEP, "modify",
                                 spec_module.names(spec)["balancer"],
                                 ", ".join(sorted(changed))))
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
    present = {listener.get("Port"): listener
               for listener in (observed.get("listeners") or [])}
    for port, role in ((443, "api"), (80, "certbot")):
        listener = present.get(port)
        if listener:
            default_actions = listener.get("DefaultActions") or []
            target_arn = group_arns.get(role)
            exact = (
                listener.get("Protocol") == "TCP"
                and len(default_actions) == 1
                and default_actions[0].get("Type") == "forward"
                and default_actions[0].get("TargetGroupArn") == target_arn)
            if exact:
                findings.append(report.existing(
                    STEP, f"listener.{port}.ok",
                    f"TCP:{port} forwards to the {role} target group"))
            else:
                # MANUAL on purpose, and not an exit-code failure: MANUAL is
                # not in `report.BLOCKING_STATUSES`, so this reports without
                # failing the run. No mode of this package calls
                # modify_listener or delete_listener, so a drifted listener is
                # exactly "it exists, it differs, and apply does not touch it".
                findings.append(report.manual(
                    STEP, f"listener.{port}.mismatch",
                    f"listener {port} does not exactly forward TCP to the "
                    f"owned {role} target group",
                    "repair or replace the listener explicitly; brownfield "
                    "mode does not adopt or rewrite a mismatched listener"
                    if spec.fleet else
                    "repair or replace the listener explicitly; this tool "
                    "creates a listener on an empty port but never rewrites "
                    "one that is already there"))
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
            if spec.fleet and not apply:
                declarations = [
                    row["name"] for row in spec.node_declarations
                    if row.get("serving_target")]
                preview_targets = declarations if role == "api" else declarations[:1]
                preview_targets.extend(
                    value for value in wanted_ids
                    if value not in preview_targets)
                if preview_targets:
                    actions.append(report.Action(
                        STEP, "register", role,
                        ", ".join(preview_targets)))
            continue
        current = {(row.get("Target") or {}).get("Id")
                   for row in (registered.get(role) or [])}
        if spec.fleet:
            for row in registered.get(role) or []:
                target_id = (row.get("Target") or {}).get("Id")
                state = (row.get("TargetHealth") or {}).get("State")
                if target_id in wanted_ids and state not in (
                        None, "initial", "healthy"):
                    findings.append(report.manual(
                        STEP, f"targets.{role}.unhealthy",
                        f"declared target {target_id} is {state} in the "
                        f"{role} target group",
                        "fix node readiness before cutover; brownfield apply "
                        "does not replace or deregister targets"))
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
