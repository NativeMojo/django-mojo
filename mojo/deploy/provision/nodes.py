"""The EC2 instances, their image, and the addresses that reach them.

The base image is resolved through the public SSM parameter Amazon maintains for
Amazon Linux 2023, not through `describe_images`. A filter over `Owners=["self"]`
cannot work on an empty account — there are no images of your own yet — and a
name glob against Amazon's images is a race with whatever they published this
morning. The resolved id is reported, every run, so an operator can answer "what
was this fleet built from?" months later without having written it down.

`spec.ami_override` pins one explicitly. That is the escape hatch for an account
that has to run a hardened or golden image, and it skips the SSM lookup entirely.

INSTANCES ARE CREATED INDIVIDUALLY, not through an autoscaling group. At this
size that is deliberate: the fleet is a handful of long-lived boxes that hold a
Let's Encrypt lineage, so "replace on a whim" is not the behaviour you want.
"""

import time

from mojo.deploy.provision import discover, report
from mojo.deploy.provision import spec as spec_module


STEP = "nodes"

ROOT_DEVICE = "/dev/xvda"

# IAM is eventually consistent, and a freshly created instance profile is the
# canonical place it bites: `run_instances` rejects it for a few seconds with a
# validation error that names neither IAM nor consistency.
PROFILE_RETRY_ATTEMPTS = 6
PROFILE_RETRY_SLEEP = 5
PROFILE_ERROR_MARKERS = ("Invalid IAM Instance Profile",
                         "iamInstanceProfile.name is invalid")

RUNNING_STATES = ("pending", "running")


def stage0_user_data(spec, hostname):
    """The first thing the box runs, and almost nothing.

    Identity and swap only. The real provisioning payload arrives separately —
    putting it here would mean an instance's configuration could only ever be
    changed by replacing the instance, which is exactly the property this
    topology is arranged to avoid.
    """
    return "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        "",
        f"hostnamectl set-hostname {hostname}",
        f"echo '{hostname}' > /etc/hostname",
        f"sed -i \"s/^127.0.0.1.*/127.0.0.1   localhost {hostname}/\" /etc/hosts",
        "",
        "# Swap. Small instances run close enough to their memory ceiling that",
        "# a traffic spike turns into the OOM killer reaping the app rather",
        "# than a slow request. Guarded so re-running is a no-op.",
        "if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then",
        "  [ -f /swapfile ] || fallocate -l 2G /swapfile",
        "  chmod 600 /swapfile",
        "  mkswap /swapfile",
        "  swapon /swapfile",
        "fi",
        "grep -qxF '/swapfile none swap sw 0 0' /etc/fstab || \\",
        "  echo '/swapfile none swap sw 0 0' >> /etc/fstab",
        "sysctl -w vm.swappiness=10",
        "",
        f"# project={spec.project} env={spec.env} region={spec.region}",
        "# stage 1 reads s3://<config bucket>/bootstrap/stage1.json",
        "",
    ])


def _instance_by_name(observed, hostname):
    for instance in observed.get("instances") or []:
        if (instance.get("State") or {}).get("Name") not in RUNNING_STATES:
            continue
        if discover.tags_of(instance).get("Name") == hostname:
            return instance
    return None


def ensure_nodes(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    ec2 = clients.get("ec2")

    image_id = spec.ami_override or observed.get("ami_id")
    if image_id:
        findings.append(report.existing(
            STEP, "ami.resolved",
            f"base image {image_id} "
            f"({'pinned by spec.ami_override' if spec.ami_override else 'from the AL2023 SSM parameter'})"))
        result.set("ami_id", image_id)
    else:
        findings.append(report.missing(
            STEP, "ami.unresolved",
            "no base image could be resolved",
            f"grant ssm:GetParameter on "
            f"{spec_module.SSM_AMI_PARAMETERS.get(spec_module.architecture_for(spec.node_type))}, "
            f"or set spec.ami_override"))

    subnet_ids = list(observed.get("public_subnet_ids") or [])
    sg_id = observed.get("node_sg_id")
    profile_name = observed.get("instance_profile_name") or names["instance_profile"]
    key_name = observed.get("key_pair_name") or names["key_pair"]

    instance_ids = []
    for index, hostname in enumerate(names["nodes"]):
        existing = _instance_by_name(observed, hostname)
        if existing:
            findings.append(report.existing(
                STEP, "node.ok",
                f"{hostname} is {existing.get('InstanceId')} "
                f"({(existing.get('State') or {}).get('Name')})"))
            instance_ids.append(existing.get("InstanceId"))
            _report_node_drift(hostname, existing, spec, image_id, findings)
            continue

        findings.append(report.missing(
            STEP, "node.missing", f"{hostname} does not exist",
            f"apply launches a {spec.node_type}"))
        actions.append(report.Action(STEP, "create", hostname, spec.node_type))
        if not apply:
            continue
        if not image_id or not subnet_ids or not sg_id:
            findings.append(report.missing(
                STEP, "node.prerequisites",
                f"{hostname} cannot be launched: the image, the public subnets "
                f"or the node security group are not resolved yet",
                "let the network and identity steps run first"))
            continue
        launched = _launch(ec2, spec, names, hostname, image_id,
                           subnet_ids[index % len(subnet_ids)], sg_id,
                           profile_name, key_name, findings)
        if launched:
            instance_ids.append(launched)

    result.set("instance_ids", instance_ids)

    addresses = _ensure_addresses(ec2, spec, observed, names, instance_ids,
                                  findings, actions, apply)
    result.set("node_addresses", addresses)
    return findings, actions, result


def _report_node_drift(hostname, instance, spec, image_id, findings):
    """An instance's type and image are fixed once it is running.

    Changing either means stopping or replacing the box, which is a decision
    with downtime attached — so it is reported with the command, never done.
    """
    if instance.get("InstanceType") and instance.get("InstanceType") != spec.node_type:
        findings.append(report.manual(
            STEP, "node.instance_type",
            f"{hostname} is a {instance.get('InstanceType')}, not a "
            f"{spec.node_type}",
            f"resize it deliberately: stop the instance, change the type, "
            f"start it — this tool will not take a node down"))
    if image_id and instance.get("ImageId") and instance.get("ImageId") != image_id:
        findings.append(report.existing(
            STEP, "node.image_age",
            f"{hostname} runs {instance.get('ImageId')}; the current AL2023 "
            f"image is {image_id} — replacing a node is how that changes"))


def _launch(ec2, spec, names, hostname, image_id, subnet_id, sg_id,
            profile_name, key_name, findings):
    request = {
        "ImageId": image_id,
        "InstanceType": spec.node_type,
        "MinCount": 1,
        "MaxCount": 1,
        "KeyName": key_name,
        "SubnetId": subnet_id,
        "SecurityGroupIds": [sg_id],
        "IamInstanceProfile": {"Name": profile_name},
        # IMDSv2 required, because the node receives its AWS credential through
        # that instance profile and IMDSv1 hands it to anything that can make
        # an outbound request from the box.
        "MetadataOptions": {"HttpEndpoint": "enabled", "HttpTokens": "required"},
        "BlockDeviceMappings": [{
            "DeviceName": ROOT_DEVICE,
            "Ebs": {"VolumeSize": spec.node_volume_gb, "VolumeType": "gp3",
                    "Encrypted": True, "DeleteOnTermination": True},
        }],
        "UserData": stage0_user_data(spec, hostname),
        # Instance AND volume tagged in the launch call. A run_instances that
        # succeeds followed by a create_tags that does not would leave an
        # instance this package cannot see and cannot remove, and the next run
        # would launch a second one.
        "TagSpecifications": (
            spec_module.tag_specifications(spec, "node", "instance",
                                           name=hostname)
            + spec_module.tag_specifications(spec, "node", "volume",
                                             name=hostname)),
    }
    response = _run_with_profile_retry(ec2, request, findings)
    if not response:
        return None
    launched = (response.get("Instances") or [{}])[0]
    return launched.get("InstanceId")


def _run_with_profile_retry(ec2, request, findings):
    """run_instances, retried while IAM catches up with its own instance profile.

    Only that one error is retried. Anything else is handed to `report.safe` as
    the exception that was already raised — never by calling `run_instances` a
    second time, which for a launch is the difference between reporting a
    failure and quietly starting a second machine.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    def reraise(error):
        def raiser():
            raise error
        return raiser

    for attempt in range(PROFILE_RETRY_ATTEMPTS):
        try:
            return ec2.run_instances(**request)
        except ClientError as err:
            retryable = any(marker in str(err)
                            for marker in PROFILE_ERROR_MARKERS)
            if retryable and attempt < PROFILE_RETRY_ATTEMPTS - 1:
                time.sleep(PROFILE_RETRY_SLEEP)
                continue
            return report.safe(findings, STEP, "ec2.run_instances",
                               reraise(err))
        except BotoCoreError as err:
            return report.safe(findings, STEP, "ec2.run_instances",
                               reraise(err))
    return None


def _ensure_addresses(ec2, spec, observed, names, instance_ids,
                      findings, actions, apply):
    """A stable public address per node — but only where one is needed.

    With an NLB in front, the balancer holds the addresses and the nodes reach
    the internet through the auto-assigned public IPs their subnets hand out.
    Without one, the node IS the address DNS points at, so it needs an elastic
    IP that survives a stop/start.

    An existing elastic IP is adopted ONLY when it already carries this
    project's and environment's tags. An untagged address that happens to be
    unassociated may well be someone else's reservation.
    """
    if spec_module.wants_balancer(spec):
        return []

    resolved = []
    for index, hostname in enumerate(names["nodes"]):
        instance_id = instance_ids[index] if index < len(instance_ids) else None
        existing = None
        for address in observed.get("addresses") or []:
            tags = discover.tags_of(address)
            if not spec_module.owns(tags, spec):
                continue
            if tags.get("Name") == hostname:
                existing = address
                break

        if existing:
            resolved.append(existing.get("PublicIp"))
            if existing.get("InstanceId"):
                findings.append(report.existing(
                    STEP, "address.ok",
                    f"{hostname} holds {existing.get('PublicIp')}"))
                continue
            findings.append(report.drift(
                STEP, "address.unattached",
                f"{existing.get('PublicIp')} is reserved for {hostname} but "
                f"attached to nothing",
                "apply associates it"))
            actions.append(report.Action(STEP, "attach",
                                         existing.get("PublicIp"), hostname))
            if apply and instance_id:
                report.safe(
                    findings, STEP, "ec2.associate_address",
                    lambda a=existing, i=instance_id: ec2.associate_address(
                        AllocationId=a.get("AllocationId"), InstanceId=i))
            continue

        findings.append(report.missing(
            STEP, "address.missing",
            f"{hostname} has no elastic IP reserved for it",
            "apply allocates one and associates it"))
        actions.append(report.Action(STEP, "create", f"{hostname} elastic IP"))
        if not apply:
            continue
        allocated = report.safe(
            findings, STEP, "ec2.allocate_address",
            lambda n=hostname: ec2.allocate_address(
                Domain="vpc",
                TagSpecifications=spec_module.tag_specifications(
                    spec, "node", "elastic-ip", name=n)))
        if not allocated:
            continue
        resolved.append(allocated.get("PublicIp"))
        if instance_id:
            report.safe(
                findings, STEP, "ec2.associate_address",
                lambda a=allocated, i=instance_id: ec2.associate_address(
                    AllocationId=a.get("AllocationId"), InstanceId=i))
    return resolved
