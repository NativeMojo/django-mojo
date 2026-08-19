"""How the nodes are reached, and what they are allowed to do.

Two things live here and they fail in opposite ways.

The KEY PAIR is the one resource in this package whose private material AWS
returns exactly once and never again. Combined with this package's no-delete
rule, a generated key whose material is lost is permanently unrecoverable: the
key pair sits in the account, the nodes trust it, and nobody can log in. So the
preferred path is `ImportKeyPair` from a public key the operator already has —
no private material ever touches AWS, and there is nothing that can only be read
once. When a key IS generated here, writing it into the bootstrap secrets object
happens in the same step, and a failed write fails the step rather than leaving
a key pair whose only copy just went to the garbage collector.

The NODE ROLE is what the box uses to do everything else. It is the terraform
`django-mojo-setup` policy, carried across so an account built by this tool and
one built by tofu look identical to an auditor, plus one named block for the
CloudWatch agent.
"""

import json

from mojo.deploy.provision import report
from mojo.deploy.provision import spec as spec_module


KEY_STEP = "key_pair"
ROLE_STEP = "node_role"

ASSUME_ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

# Verbatim from aws/terraform/modules/nodes/main.tf. The node is the django-mojo
# administration plane during environment setup: it buys domains, provisions
# fleet resources, configures storage and email, and installs monitoring. Keep
# this credential attached until the environment is converged; a later hardening
# pass can narrow it to the runtime subset without rebuilding the instances.
SETUP_ACTIONS = (
    "route53domains:*",
    "route53:*",
    "s3:*",
    "ec2:*",
    "elasticloadbalancing:*",
    "autoscaling:*",
    "rds:*",
    "elasticache:*",
    "acm:*",
    "iam:*",
    "sts:AssumeRole",
    "kms:*",
    "cloudwatch:*",
    "logs:*",
    "cloudtrail:*",
    "ce:GetCostAndUsage",
    "sns:*",
    "ses:*",
    "guardduty:*",
    "events:*",
)

# The CloudWatch agent's minimum, scoped to this environment's log groups.
#
# THIS IS THE DELTA from the terraform policy, and it is deliberately redundant
# today: the setup statement above already grants `logs:*` on `*`, so the agent
# would work without this block. It is here because the setup statement is
# explicitly temporary — the hardening pass that narrows it to the runtime
# subset must not silently take the node's ability to ship its own logs with it.
# Written as a separate Sid so that pass can delete one statement and keep the
# other.
AGENT_LOG_ACTIONS = (
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    "logs:DescribeLogStreams",
    "logs:DescribeLogGroups",
)
AGENT_METRIC_ACTIONS = ("cloudwatch:PutMetricData",)


def node_policy_document(spec):
    """The inline policy attached to the node role.

    `account_id` comes from `sts:GetCallerIdentity` during observation. If it
    could not be read, the account segment falls back to the ARN wildcard, which
    IAM accepts — a policy that is one segment broader is a far better outcome
    than a step that refuses to run because a describe was denied.
    """
    names = spec_module.names(spec)
    account = spec.account_id or "*"
    log_group_arn = (f"arn:aws:logs:{spec.region}:{account}:log-group:"
                     f"{names['log_group_prefix']}/*")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DjangoMojoEnvironmentSetup",
                "Effect": "Allow",
                "Action": list(SETUP_ACTIONS),
                "Resource": "*",
            },
            {
                "Sid": "DjangoMojoAgentLogs",
                "Effect": "Allow",
                "Action": list(AGENT_LOG_ACTIONS),
                "Resource": log_group_arn,
            },
            {
                "Sid": "DjangoMojoAgentMetrics",
                "Effect": "Allow",
                "Action": list(AGENT_METRIC_ACTIONS),
                # PutMetricData cannot be scoped by resource — the only lever
                # AWS offers is a cloudwatch:namespace condition, and the agent
                # publishes into several.
                "Resource": "*",
            },
        ],
    }


def ensure_key_pair(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    key_name = names["key_pair"]
    ec2 = clients.get("ec2")

    secrets = dict(observed.get("secrets") or {})
    existing = observed.get("key_pair")

    if existing:
        result.set("key_pair_name", existing.get("KeyName"))
        result.set("key_pair_created", False)
        if secrets.get("ssh_private_key") or spec.public_key:
            findings.append(report.existing(
                KEY_STEP, "key_pair.ok",
                f"key pair {existing.get('KeyName')} is in place"))
        else:
            # The one case where doing nothing is clearly right and clearly
            # unhelpful. Recreating would mean deleting, which this package
            # does not do, and would lock out every node already trusting it.
            findings.append(report.manual(
                KEY_STEP, "key_pair.no_private_material",
                f"key pair {existing.get('KeyName')} exists in AWS but its "
                f"private key is not in {names['secrets_object']} and no "
                f"public key was supplied — this tool cannot recover it and "
                f"will not replace it",
                "if you still hold the private key, store it in the bootstrap "
                "secrets object; otherwise create a second key pair by hand and "
                "add its public half to the nodes' authorized_keys"))
        return findings, actions, result

    if spec.public_key:
        findings.append(report.missing(
            KEY_STEP, "key_pair.missing",
            f"key pair {key_name} does not exist",
            "apply imports the public key you supplied — no private material "
            "ever reaches AWS"))
        actions.append(report.Action(KEY_STEP, "create", key_name, "import"))
        if not apply:
            return findings, actions, result
        imported = report.safe(
            findings, KEY_STEP, "ec2.import_key_pair",
            lambda: ec2.import_key_pair(
                KeyName=key_name,
                PublicKeyMaterial=spec.public_key.encode("utf-8")
                if isinstance(spec.public_key, str) else spec.public_key,
                TagSpecifications=spec_module.tag_specifications(
                    spec, "identity", "key-pair", name=key_name)))
        if imported:
            result.set("key_pair_name", key_name)
            result.set("key_pair_created", True)
        return findings, actions, result

    findings.append(report.missing(
        KEY_STEP, "key_pair.missing",
        f"key pair {key_name} does not exist",
        f"apply generates one and stores the private key in "
        f"s3://{names['config_bucket']}/{names['secrets_object']}"))
    actions.append(report.Action(KEY_STEP, "create", key_name, "generate"))
    if not apply:
        return findings, actions, result

    bucket = observed.get("config_bucket") or names["config_bucket"]
    created = report.safe(
        findings, KEY_STEP, "ec2.create_key_pair",
        lambda: ec2.create_key_pair(
            KeyName=key_name, KeyType="ed25519",
            TagSpecifications=spec_module.tag_specifications(
                spec, "identity", "key-pair", name=key_name)))
    if not created:
        return findings, actions, result

    # From here to the put_object there is exactly one copy of this key in
    # existence and it is in memory. Nothing between these two lines may fail
    # silently, and nothing may log the value.
    material = created.get("KeyMaterial")
    secrets["ssh_private_key"] = material
    secrets["ssh_key_name"] = key_name
    s3 = clients.get("s3")
    stored = report.safe(
        findings, KEY_STEP, "s3.put_object",
        lambda: s3.put_object(
            Bucket=bucket, Key=names["secrets_object"],
            Body=json.dumps(secrets, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256"))
    # `is None` rather than a truthiness check: `report.safe` returns its
    # default (None) on failure, and a successful S3 response can legitimately
    # be an empty mapping. Treating that as a failure would report a key as
    # lost that was in fact written.
    if stored is None:
        findings.append(report.Finding(
            KEY_STEP, report.BLIND, "key_pair.material_lost",
            f"key pair {key_name} was created but its private key could not be "
            f"written to s3://{bucket}/{names['secrets_object']}; AWS returns "
            f"that material once and it is now gone",
            "grant s3:PutObject on the config bucket, then create a second key "
            "pair by hand — this tool will not delete the orphaned one"))
        return findings, actions, result

    actions.append(report.Action(
        KEY_STEP, "write", f"s3://{bucket}/{names['secrets_object']}",
        "ssh_private_key"))
    result.set("key_pair_name", key_name)
    result.set("key_pair_created", True)
    result.set("secrets", secrets)
    return findings, actions, result


def ensure_node_role(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    iam = clients.get("iam")
    role_name = names["node_role"]
    profile_name = names["instance_profile"]
    wanted_policy = node_policy_document(spec)

    role = observed.get("node_role")
    if role:
        findings.append(report.existing(
            ROLE_STEP, "role.ok", f"role {role_name} is in place"))
        result.set("node_role_arn", role.get("Arn"))
    else:
        findings.append(report.missing(
            ROLE_STEP, "role.missing", f"role {role_name} does not exist",
            "apply creates it, trusted by ec2.amazonaws.com"))
        actions.append(report.Action(ROLE_STEP, "create", role_name))
        if apply:
            created = report.safe(
                findings, ROLE_STEP, "iam.create_role",
                lambda: iam.create_role(
                    RoleName=role_name,
                    AssumeRolePolicyDocument=json.dumps(ASSUME_ROLE_POLICY),
                    Description="django-mojo application node",
                    Tags=spec_module.tag_list(spec, "identity",
                                              name=role_name)))
            if not created:
                return findings, actions, result
            role = created.get("Role") or {}
            result.set("node_role_arn", role.get("Arn"))
    result.set("node_role_name", role_name)

    current_policy = observed.get("node_role_policy")
    if _policy_matches(current_policy, wanted_policy):
        findings.append(report.existing(
            ROLE_STEP, "policy.ok",
            f"inline policy {names['node_policy']} matches"))
    else:
        findings.append(report.drift(
            ROLE_STEP, "policy.drift" if current_policy else "policy.missing",
            f"inline policy {names['node_policy']} on {role_name} "
            f"{'differs from' if current_policy else 'is not attached to'} "
            f"what this topology declares",
            "apply writes it — put_role_policy replaces the document whole, so "
            "this is safe to re-run"))
        actions.append(report.Action(
            ROLE_STEP, "modify", f"{role_name}/{names['node_policy']}"))
        if apply:
            report.safe(
                findings, ROLE_STEP, "iam.put_role_policy",
                lambda: iam.put_role_policy(
                    RoleName=role_name, PolicyName=names["node_policy"],
                    PolicyDocument=json.dumps(wanted_policy)))

    profile = observed.get("instance_profile")
    if profile:
        findings.append(report.existing(
            ROLE_STEP, "profile.ok",
            f"instance profile {profile_name} is in place"))
        result.set("instance_profile_arn", profile.get("Arn"))
        attached = {row.get("RoleName")
                    for row in (profile.get("Roles") or [])}
        if role_name not in attached:
            findings.append(report.drift(
                ROLE_STEP, "profile.role_missing",
                f"instance profile {profile_name} does not carry {role_name}",
                "apply attaches it"))
            actions.append(report.Action(
                ROLE_STEP, "attach", profile_name, role_name))
            if apply:
                report.safe(
                    findings, ROLE_STEP, "iam.add_role_to_instance_profile",
                    lambda: iam.add_role_to_instance_profile(
                        InstanceProfileName=profile_name, RoleName=role_name))
    else:
        findings.append(report.missing(
            ROLE_STEP, "profile.missing",
            f"instance profile {profile_name} does not exist",
            "apply creates it and attaches the node role"))
        actions.append(report.Action(ROLE_STEP, "create", profile_name))
        if apply:
            created = report.safe(
                findings, ROLE_STEP, "iam.create_instance_profile",
                lambda: iam.create_instance_profile(
                    InstanceProfileName=profile_name,
                    Tags=spec_module.tag_list(spec, "identity",
                                              name=profile_name)))
            if created:
                result.set("instance_profile_arn",
                           (created.get("InstanceProfile") or {}).get("Arn"))
                report.safe(
                    findings, ROLE_STEP, "iam.add_role_to_instance_profile",
                    lambda: iam.add_role_to_instance_profile(
                        InstanceProfileName=profile_name, RoleName=role_name))
    result.set("instance_profile_name", profile_name)
    return findings, actions, result


def _policy_matches(current, wanted):
    """Compare two policy documents by content, not by their JSON text.

    IAM normalises whitespace, may return a single-element Action as a string
    rather than a list, and does not preserve statement order. Comparing the
    serialized form would report drift on every run and rewrite a policy that
    was already correct.
    """
    if not current:
        return False
    return _normalise(current) == _normalise(wanted)


def _normalise(document):
    statements = document.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    rows = []
    for statement in statements:
        row = {}
        for key, value in statement.items():
            if isinstance(value, str) and key in ("Action", "Resource",
                                                  "NotAction", "NotResource"):
                value = [value]
            if isinstance(value, list):
                value = sorted(value)
            row[key] = value
        rows.append(tuple(sorted(row.items(), key=lambda pair: pair[0])))
    return (document.get("Version"), sorted(rows, key=repr))


def ensure_identity(clients, spec, observed, apply=False):
    """Key pair and node role, for a caller converging this area alone."""
    findings, actions, result = ensure_key_pair(clients, spec, observed, apply)
    merged = dict(observed or {})
    merged.update(result.as_dict())
    more_findings, more_actions, more_result = ensure_node_role(
        clients, spec, merged, apply)
    findings.extend(more_findings)
    actions.extend(more_actions)
    result.update(**more_result.as_dict())
    return findings, actions, result
