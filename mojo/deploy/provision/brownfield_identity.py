"""Least-privilege runtime identities for migration-owned fleet nodes."""

import json

from mojo.deploy.provision import report


STEP = "identity"
SSM_CORE_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
ASSUME_ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def policy_document(topology):
    manifest = topology.brownfield_manifest
    storage = manifest["storage"]
    bootstrap = manifest["bootstrap"]
    get_objects = []
    versioned_objects = []
    list_prefixes = {}
    for reference in storage.values():
        get_objects.append(
            f"arn:aws:s3:::{reference['bucket']}/{reference['prefix'].rstrip('/')}/*")
        list_prefixes.setdefault(reference["bucket"], set()).add(
            f"{reference['prefix'].rstrip('/')}/*")
    for reference in bootstrap.values():
        versioned_objects.append(
            f"arn:aws:s3:::{reference['bucket']}/{reference['key']}")
    for plane in ("database", "cache"):
        credential = manifest[plane].get("credential")
        if credential:
            reference = credential["object"]
            versioned_objects.append(
                f"arn:aws:s3:::{reference['bucket']}/{reference['key']}")
    statements = [{
        "Sid": "ReadDeclaredFleetPrefixes",
        "Effect": "Allow", "Action": ["s3:GetObject"],
        "Resource": sorted(set(get_objects)),
    }, {
        "Sid": "ReadPinnedFleetArtifacts",
        "Effect": "Allow", "Action": ["s3:GetObjectVersion"],
        "Resource": sorted(set(versioned_objects)),
    }]
    for bucket, prefixes in sorted(list_prefixes.items()):
        statements.append({
            "Sid": "List" + _sid(bucket),
            "Effect": "Allow", "Action": ["s3:ListBucket"],
            "Resource": f"arn:aws:s3:::{bucket}",
            "Condition": {"StringLike": {"s3:prefix": sorted(prefixes)}},
        })
    fleet_config = storage["fleet_config"]
    statements.extend(({
        "Sid": "WriteFleetOwnedConfig",
        "Effect": "Allow", "Action": ["s3:PutObject"],
        "Resource": (f"arn:aws:s3:::{fleet_config['bucket']}/"
                     f"{fleet_config['prefix'].rstrip('/')}/*"),
    }, {
        "Sid": "DecryptDeclaredKey",
        "Effect": "Allow", "Action": ["kms:Decrypt"],
        "Resource": manifest["kms_key_arn"],
    }, {
        "Sid": "WriteFleetLogs",
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:DescribeLogStreams",
                   "logs:PutLogEvents"],
        "Resource": (f"arn:aws:logs:{topology.region}:{topology.account_id}:"
                     f"log-group:/mojo/{topology.project}-{topology.fleet}/*:*"),
    }, {
        "Sid": "PublishFleetMetrics",
        "Effect": "Allow", "Action": ["cloudwatch:PutMetricData"],
        "Resource": "*",
        "Condition": {"StringEquals": {
            "cloudwatch:namespace": topology.metrics_namespace}},
    }))
    return {"Version": "2012-10-17", "Statement": statements}


def ensure_identity(clients, topology, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    iam = clients.get("iam")
    current = dict(observed.get("brownfield_profiles") or {})
    resolved = {}
    wanted_policy = policy_document(topology)
    session_manager = bool(
        topology.brownfield_manifest["nodes"].get("session_manager"))

    for role, declaration in topology.brownfield_manifest[
            "nodes"]["profiles"].items():
        if not declaration.get("managed"):
            profile = current.get(role) or {}
            if profile.get("profile_arn") != declaration.get("profile_arn"):
                findings.append(report.Finding(
                    STEP, report.BLIND, "profile.exact_mismatch",
                    f"existing profile for {role} was not validated exactly",
                    "fix the dependency observation; no IAM resource was changed"))
                continue
            findings.append(report.existing(
                STEP, "profile.existing",
                f"role {role} reuses exact profile {profile.get('profile_arn')}"))
            resolved[role] = profile
            continue

        wanted = declaration["managed"]
        profile_name = wanted["profile_name"]
        role_name = wanted["role_name"]
        found = current.get(role) or {}
        if found.get("role_collision") or found.get("profile_collision"):
            findings.append(report.Finding(
                STEP, report.BLIND, "identity.name_collision",
                f"{role_name} or {profile_name} exists without the exact "
                f"fleet ownership contract",
                "rename the migration-owned declaration or resolve the "
                "unowned resource; no IAM mutation was attempted"))
            continue
        if not found.get("role_arn"):
            findings.append(report.missing(
                STEP, "role.missing", f"migration role {role_name} is absent",
                "apply creates it with the fleet ownership tags"))
            actions.append(report.Action(STEP, "create", role_name, role))
            if apply:
                answer = report.safe(
                    findings, STEP, "iam.create_role",
                    lambda rn=role_name: iam.create_role(
                        RoleName=rn,
                        AssumeRolePolicyDocument=json.dumps(ASSUME_ROLE_POLICY),
                        Description=(f"{topology.project} {topology.fleet} "
                                     f"{role} runtime role"),
                        Tags=_iam_tags(topology, role))) or {}
                found["role_arn"] = (answer.get("Role") or {}).get("Arn")
        else:
            findings.append(report.existing(
                STEP, "role.ok", f"migration role {role_name} exists"))

        if apply and not found.get("role_arn"):
            # A failed create must never fall through into calls that could
            # mutate a pre-existing name collision AWS refused to create over.
            continue

        policy_name = f"{role_name}-runtime"
        if found.get("policy_document") != wanted_policy:
            findings.append(report.drift(
                STEP, "policy.drift",
                f"runtime policy {policy_name} is absent or differs",
                "apply replaces the migration-owned inline policy"))
            actions.append(report.Action(STEP, "modify", role_name,
                                         "least-privilege runtime policy"))
            if apply:
                report.safe(
                    findings, STEP, "iam.put_role_policy",
                    lambda rn=role_name, pn=policy_name:
                    iam.put_role_policy(
                        RoleName=rn, PolicyName=pn,
                        PolicyDocument=json.dumps(wanted_policy)))

        if session_manager and not found.get("ssm_core_attached"):
            findings.append(report.drift(
                STEP, "role.ssm_core", f"{role_name} needs Session Manager",
                "apply attaches AmazonSSMManagedInstanceCore"))
            actions.append(report.Action(STEP, "attach", role_name,
                                         "AmazonSSMManagedInstanceCore"))
            if apply:
                report.safe(
                    findings, STEP, "iam.attach_role_policy",
                    lambda rn=role_name: iam.attach_role_policy(
                        RoleName=rn, PolicyArn=SSM_CORE_POLICY))

        if not found.get("profile_arn"):
            findings.append(report.missing(
                STEP, "profile.missing",
                f"migration instance profile {profile_name} is absent",
                "apply creates it and attaches only its declared role"))
            actions.append(report.Action(STEP, "create", profile_name, role))
            if apply:
                answer = report.safe(
                    findings, STEP, "iam.create_instance_profile",
                    lambda pn=profile_name: iam.create_instance_profile(
                        InstanceProfileName=pn,
                        Tags=_iam_tags(topology, role))) or {}
                profile = answer.get("InstanceProfile") or {}
                found["profile_arn"] = profile.get("Arn")
                if found.get("profile_arn"):
                    report.safe(
                        findings, STEP, "iam.add_role_to_instance_profile",
                        lambda pn=profile_name, rn=role_name:
                        iam.add_role_to_instance_profile(
                            InstanceProfileName=pn, RoleName=rn))
        else:
            findings.append(report.existing(
                STEP, "profile.ok",
                f"migration instance profile {profile_name} exists"))

        found["profile_name"] = profile_name
        found["role_name"] = role_name
        resolved[role] = found

    result.set("brownfield_profiles", resolved)
    return findings, actions, result


def _iam_tags(topology, role):
    return [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": topology.project},
        {"Key": "mojo:env", "Value": topology.env},
        {"Key": "mojo:fleet", "Value": topology.fleet},
        {"Key": "mojo:role", "Value": "identity"},
        {"Key": "mojo:application-role", "Value": role},
    ]


def _sid(value):
    return "".join(char for char in value.title() if char.isalnum())[:48]
