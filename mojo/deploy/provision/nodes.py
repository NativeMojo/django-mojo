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

import json
import shlex
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

# "That instance is not ready for this yet", as opposed to "that request was
# wrong". EC2 models no error shapes at all, so these are matched on the code
# string boto3 lifts out of the response body:
#
#   InvalidInstanceID       what AssociateAddress returns for an instance still
#                           in `pending` — "The pending instance 'i-...' is not
#                           in a valid state for this operation". Observed on a
#                           real first run against an empty account, seconds
#                           after run_instances.
#   IncorrectInstanceState  the same refusal for an instance that is stopping,
#                           stopped or otherwise mid-transition.
#
# InvalidInstanceID.NotFound is deliberately NOT here. It is a different
# sentence — the instance is not there at all — and treating that as "wait a
# bit" would turn a terminated node into a run that quietly reports progress.
NOT_READY_CODES = ("InvalidInstanceID", "IncorrectInstanceState")

# The project root every node script agrees on. Not configurable here: it is
# baked into the skeleton's ec2_bootstrap.sh, into config_sync's defaults and
# into check_node's audit, so a fifth opinion would only be a way to disagree.
PROJ_PATH = "/opt/api"

# EC2's own ceiling on user data, before base64. This script stays FAR under
# it on purpose — `ec2_bootstrap.sh` alone is ~20KB, which is exactly why the
# real payload is downloaded rather than embedded. The assertion below is a
# guard against someone growing this back toward the cliff, not a close call.
USER_DATA_LIMIT = 16384
USER_DATA_BUDGET = 4096

# The bootstrap credential contract `check_node.check_config_plane` audits: the
# app user writes var/django.conf, the web user reads it.
CONFIG_SYNC_OWNER = "ec2-user:www"
REQUEST_SERVICE_PATH = "/etc/mojo/request-service.conf"
REQUEST_SERVICE_MARKER = "/etc/mojo/request-service.enabled"
REQUEST_SERVICE_DROPIN_DIR = "/etc/systemd/system/mojo-asgi.service.d"
REQUEST_SERVICE_DROPIN = (
    f"{REQUEST_SERVICE_DROPIN_DIR}/00-request-service.conf")


def _request_service(declaration):
    """The strict brownfield-only framework request-service selection."""
    if "request_service" not in declaration:
        return True
    value = declaration["request_service"]
    if not isinstance(value, bool):
        raise ValueError(
            "request_service must be a JSON boolean; refusing to infer "
            "request-serving authority")
    return value


def _create_action_detail(spec, declaration):
    """Bind explicit request authority without changing omitted previews."""
    if not spec.fleet or "request_service" not in declaration:
        return spec.node_type
    return json.dumps({
        "instance_type": spec.node_type,
        "request_service": _request_service(declaration),
    }, sort_keys=True, separators=(",", ":"))


def stage0_user_data(spec, hostname, declaration=None):
    """The first thing the box runs, and almost nothing.

    Identity, swap, `var/bootstrap.conf`, and then it hands over: it downloads
    the packaged `stage1.sh` from the config bucket and execs it. Everything of
    any size lives in that script, because user data cannot be edited on a
    running instance — putting the real payload here would mean a node's
    provisioning could only ever be changed by replacing the node, which is
    exactly the property this topology is arranged to avoid. It also does not
    fit: `ec2_bootstrap.sh` is roughly 20KB against a 16KB ceiling.

    NO CREDENTIALS APPEAR HERE. User data is readable by anything on the box
    that can reach IMDS, and it is echoed back by `describe-instance-attribute`
    to anyone with EC2 read access. The node reads S3 with its instance role;
    `bootstrap.conf` carries endpoints and names only.
    """
    names = spec_module.names(spec)
    bucket = names["config_bucket"]
    declaration = declaration or {"role": "default",
                                  "serving_target": True}
    if not spec.fleet and "request_service" in declaration:
        raise ValueError(
            "request_service is a brownfield-only node declaration")
    stage1 = (spec.bootstrap_objects.get("stage1")
              if spec.fleet else None)
    live_config = (spec.bootstrap_objects.get("live_config")
                   if spec.fleet else None)
    role_document = (spec.role_document if spec.fleet else None)
    script_uri = f"s3://{bucket}/{names['stage1_script_object']}"
    config_sync_restart = "true"
    if spec.fleet:
        request_service = _request_service(declaration)
        if not request_service:
            config_sync_restart = "false"
        hostname_lines = [
            f"hostnamectl set-hostname {_shell(hostname)}",
            f"echo {_shell(hostname)} > /etc/hostname",
            f"sed -i {_shell(f's/^127.0.0.1.*/127.0.0.1   localhost {hostname}/')} "
            f"/etc/hosts",
        ]
        config_lines = [
            f"AWS_REGION={_shell(spec.region)}",
            f"AWS_CONFIG_BUCKET={_shell(bucket)}",
            f"AWS_CONFIG_PREFIX={_shell(names['config_prefix'])}",
            f"MOJO_NODE_ROLE={_shell(declaration.get('role'))}",
            f"CONFIG_SYNC_OWNER={_shell(CONFIG_SYNC_OWNER)}",
        ]
    else:
        # Managed stage 0 is an existing compatibility contract. Fleet-only
        # quoting/role metadata must not change its rendered bytes.
        hostname_lines = [
            f"hostnamectl set-hostname {hostname}",
            f"echo '{hostname}' > /etc/hostname",
            f"sed -i \"s/^127.0.0.1.*/127.0.0.1   localhost {hostname}/\" "
            f"/etc/hosts",
        ]
        config_lines = [
            f"AWS_REGION={spec.region}",
            f"AWS_CONFIG_BUCKET={bucket}",
            f"AWS_CONFIG_PREFIX={names['config_prefix']}",
            f"CONFIG_SYNC_OWNER={CONFIG_SYNC_OWNER}",
        ]

    request_service_lines = []
    if spec.fleet and "request_service" in declaration:
        selection = "true" if request_service else "false"
        request_service_lines = [
            "# Root-sealed request-service authority and a durable systemd",
            "# refusal that survives older framework unit replacement.",
            "install -d -o root -g root -m 0755 /etc/mojo",
            f"install -o root -g root -m 0600 /dev/null {REQUEST_SERVICE_PATH}",
            f"printf '%s\\n' 'MOJO_REQUEST_SERVICE={selection}' > "
            f"{REQUEST_SERVICE_PATH}",
            f"install -d -o root -g root -m 0755 {REQUEST_SERVICE_DROPIN_DIR}",
            f"cat > {REQUEST_SERVICE_DROPIN} <<'MOJOREQUEST'",
            "[Unit]",
            f"ConditionPathExists={REQUEST_SERVICE_MARKER}",
            "MOJOREQUEST",
            f"chown root:root {REQUEST_SERVICE_DROPIN}",
            f"chmod 0644 {REQUEST_SERVICE_DROPIN}",
        ]
        if request_service:
            request_service_lines.extend((
                f"install -o root -g root -m 0400 /dev/null "
                f"{REQUEST_SERVICE_MARKER}",))
        else:
            request_service_lines.extend((
                f"rm -f -- {REQUEST_SERVICE_MARKER}",))

    script = "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        "exec >> /var/log/mojo-stage0.log 2>&1",
        f"echo \"stage0 $(date -Is) {hostname}\"",
        "",
    ] + hostname_lines + [
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
        "# The config plane's own config. install(1) creates it at 0600 and the",
        "# redirect below truncates rather than recreates, so the mode holds.",
        f"mkdir -p {PROJ_PATH}/var",
        f"install -m 0600 /dev/null {PROJ_PATH}/var/bootstrap.conf",
        f"cat > {PROJ_PATH}/var/bootstrap.conf <<'MOJOCONF'",
    ] + config_lines + [
        f"CONFIG_SYNC_RESTART={config_sync_restart}",
        "MOJOCONF",
        "",
    ] + request_service_lines + ([""] if request_service_lines else []) + [
        "# Stage 1. Downloaded with the instance role, never with a key.",
    ] + (_pinned_download_lines(
        stage1, f"{PROJ_PATH}/var/stage1.sh", spec.region)
        if stage1 else [
            f"aws s3 cp --region {spec.region} \\",
            f"  {script_uri} {PROJ_PATH}/var/stage1.sh",
        ]) + ([
        "# Seed the exact live application config without copying or",
        "# republishing its S3 dependency. Future config-sync convergence",
        "# reads only the separate migration-owned fleet_config prefix.",
    ] + _pinned_download_lines(
        live_config, f"{PROJ_PATH}/var/django.conf", spec.region) + [
        f"chown {CONFIG_SYNC_OWNER} {PROJ_PATH}/var/django.conf",
        f"chmod 0640 {PROJ_PATH}/var/django.conf",
    ] if live_config else []) + ([
        "# Opaque application role. Root owns the immutable document; the",
        "# application decides what its contents mean.",
    ] + _pinned_download_lines(
        role_document, "/etc/mojo/node-role.json", spec.region,
        directory="/etc/mojo") + [
        "chown root:root /etc/mojo/node-role.json",
        "chmod 0600 /etc/mojo/node-role.json",
    ] if role_document else []) + [
        f"chmod 0700 {PROJ_PATH}/var/stage1.sh",
        f"exec bash {PROJ_PATH}/var/stage1.sh",
        "",
    ])

    if len(script.encode("utf-8")) > USER_DATA_BUDGET:
        # Refused here rather than by EC2 at launch: a `run_instances` that
        # fails on user-data size after the network, the roles and the database
        # already exist is a bill plus a manual cleanup.
        raise ValueError(
            f"stage-0 user data is {len(script.encode('utf-8'))} bytes, past "
            f"this package's own {USER_DATA_BUDGET}-byte budget (EC2's hard "
            f"limit is {USER_DATA_LIMIT}). Move whatever grew into "
            f"mojo/deploy/provision/scripts/stage1.sh, which is downloaded, not embedded")
    return script


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
    node_records = []
    declarations = (list(spec.node_declarations) if spec.fleet else [
        {"name": hostname, "role": "default", "serving_target": True}
        for hostname in names["nodes"]])
    profiles = observed.get("brownfield_profiles") or {}
    for index, declaration in enumerate(declarations):
        hostname = declaration["name"]
        existing = _instance_by_name(observed, hostname)
        if existing:
            findings.append(report.existing(
                STEP, "node.ok",
                f"{hostname} is {existing.get('InstanceId')} "
                f"({(existing.get('State') or {}).get('Name')})"))
            instance_ids.append(existing.get("InstanceId"))
            record = {"instance_id": existing.get("InstanceId"),
                      "hostname": hostname,
                      "role": declaration.get("role"),
                      "serving_target": bool(
                          declaration.get("serving_target"))}
            if spec.fleet:
                observed_selection = discover.tags_of(existing).get(
                    "mojo:request-service")
                record["request_service"] = observed_selection != "false"
            node_records.append(record)
            _report_node_drift(hostname, existing, spec, image_id, findings)
            continue

        findings.append(report.missing(
            STEP, "node.missing", f"{hostname} does not exist",
            f"apply launches a {spec.node_type}"))
        actions.append(report.Action(
            STEP, "create", hostname,
            _create_action_detail(spec, declaration)))
        if not apply:
            continue
        if not image_id or not subnet_ids or not sg_id:
            findings.append(report.missing(
                STEP, "node.prerequisites",
                f"{hostname} cannot be launched: the image, the public subnets "
                f"or the node security group are not resolved yet",
                "let the network and identity steps run first"))
            continue
        if not observed.get("bootstrap_payload"):
            # An instance whose stage-1 payload is not published boots, bills,
            # and does nothing until somebody SSHes in to find out why. Not
            # launching it is strictly better than launching it blind.
            findings.append(report.missing(
                STEP, "node.payload",
                f"{hostname} was not launched: the stage-1 payload is not "
                f"published yet",
                "fix what the bootstrap_payload step reported — usually an "
                "unpublished version pin — and re-run"))
            continue
        subnet_id = declaration.get("subnet_id") or subnet_ids[
            index % len(subnet_ids)]
        selected_profile = profile_name
        if spec.fleet:
            profile = profiles.get(declaration.get("role")) or {}
            selected_profile = (profile.get("profile_arn")
                                or profile.get("profile_name"))
        launched = _launch(ec2, spec, names, hostname, image_id,
                           subnet_id, sg_id, selected_profile, key_name,
                           findings, declaration=declaration)
        if launched:
            instance_ids.append(launched)
            record = {"instance_id": launched,
                      "hostname": hostname,
                      "role": declaration.get("role"),
                      "serving_target": bool(
                          declaration.get("serving_target"))}
            if spec.fleet:
                record["request_service"] = _request_service(declaration)
            node_records.append(record)

    result.set("instance_ids", instance_ids)
    result.set("node_records", node_records)
    if spec.fleet:
        proofs = [
            {"role": declaration.get("role"), "node": declaration.get("name"),
             "database": "SELECT 1", "cache": "PING",
             "request_service": {
                 "authority": REQUEST_SERVICE_PATH,
                 "expected": _request_service(declaration),
                 "systemd": ("active and enabled"
                             if _request_service(declaration)
                             else "inactive and disabled; enable marker absent")}}
            for declaration in declarations]
        result.set("required_canary_proofs", proofs)
        findings.append(report.manual(
            STEP, "node.data_plane_canary",
            "every declared node role still requires a redacted database "
            "SELECT 1, cache PING, and sealed request-service/systemd proof "
            "from the launched node",
            "record those proofs without secret values before any DNS or EIP "
            "cutover; the provisioner intentionally has metadata-only access"))

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
            profile_name, key_name, findings, declaration=None):
    request = {
        "ImageId": image_id,
        "InstanceType": spec.node_type,
        "MinCount": 1,
        "MaxCount": 1,
        "KeyName": key_name,
        "SubnetId": subnet_id,
        "SecurityGroupIds": [sg_id],
        "IamInstanceProfile": ({"Arn": profile_name}
                               if str(profile_name).startswith("arn:")
                               else {"Name": profile_name}),
        # IMDSv2 required, because the node receives its AWS credential through
        # that instance profile and IMDSv1 hands it to anything that can make
        # an outbound request from the box.
        "MetadataOptions": {"HttpEndpoint": "enabled", "HttpTokens": "required"},
        "BlockDeviceMappings": [{
            "DeviceName": ROOT_DEVICE,
            "Ebs": {"VolumeSize": spec.node_volume_gb, "VolumeType": "gp3",
                    "Encrypted": True, "DeleteOnTermination": True},
        }],
        "UserData": stage0_user_data(spec, hostname, declaration),
        # Instance AND volume tagged in the launch call. A run_instances that
        # succeeds followed by a create_tags that does not would leave an
        # instance this package cannot see and cannot remove, and the next run
        # would launch a second one.
        "TagSpecifications": (
            (spec_module.node_tag_specifications(
                spec, declaration, "instance", name=hostname)
             + spec_module.node_tag_specifications(
                 spec, declaration, "volume", name=hostname))
            if spec.fleet else
            (spec_module.tag_specifications(spec, "node", "instance",
                                            name=hostname)
             + spec_module.tag_specifications(spec, "node", "volume",
                                              name=hostname))),
    }
    response = _run_with_profile_retry(ec2, request, findings)
    if not response:
        return None
    launched = (response.get("Instances") or [{}])[0]
    return launched.get("InstanceId")


def _pinned_download_lines(reference, destination, region, directory=None):
    """S3 object-version download plus an exact SHA-256 gate."""
    lines = []
    if directory:
        lines.append(f"mkdir -p {_shell(directory)}")
    lines.extend((
        f"aws s3api get-object --region={_shell(region)} \\",
        f"  --bucket={_shell(reference['bucket'])} "
        f"--key={_shell(reference['key'])} \\",
        f"  --version-id={_shell(reference['version_id'])} "
        f"{_shell(destination)} >/dev/null",
        f"printf '%s  %s\\n' {_shell(reference['sha256'])} "
        f"{_shell(destination)} | sha256sum -c -",
    ))
    return lines


def _shell(value):
    """One canonical quoting seam for every manifest-derived shell value."""
    return shlex.quote(str(value))


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


def _associate_address(ec2, allocation_id, instance_id, hostname, findings):
    """Attach one elastic IP — and treat a not-yet-running instance as PENDING.

    `report.safe` turns every ClientError it does not recognise into BLIND, and
    a BLIND finding fails this step and BLOCKS `dns`. But an instance that is
    still `pending` a second after `run_instances` is not a failure at all: it
    is the ordinary state of the run that just launched it, and the next `apply`
    associates the address in one call. That is precisely what PENDING exists
    for — the same treatment `data.ensure_database` gives a still-creating
    Aurora cluster.

    Everything else still goes through `report.safe` as BLIND. The unrecognised
    error is RE-RAISED inside the callable rather than handled here, so denials,
    throttles and rejected credentials keep the classification and the remedy
    they already have in one place.
    """
    from botocore.exceptions import ClientError

    def attempt():
        try:
            return ec2.associate_address(AllocationId=allocation_id,
                                         InstanceId=instance_id)
        except ClientError as err:
            code = (err.response.get("Error") or {}).get("Code", "")
            if code not in NOT_READY_CODES:
                raise
            findings.append(report.pending(
                STEP, "address.instance_not_ready",
                f"{hostname} ({instance_id}) is not running yet, so its "
                f"elastic IP was not associated ({code})",
                "the address is allocated and reserved — re-run `apply` once "
                "the instance reports running and it will be attached"))
            return None

    return report.safe(findings, STEP, "ec2.associate_address", attempt)


def _ensure_addresses(ec2, spec, observed, names, instance_ids,
                      findings, actions, apply):
    """A stable public address per node — but only where one is needed.

    With an NLB in front, the balancer holds the addresses and the nodes reach
    the internet through the auto-assigned public IPs their subnets hand out.
    Without one, the node IS the address DNS points at, so it needs an elastic
    IP that survives a stop/start. `spec.stable_node_ips` asks for node
    addresses even behind a balancer — outbound pinned for providers that
    allowlist caller IPs, while DNS keeps pointing at the NLB (dns.py's
    balancer branch never reads node_addresses).

    An existing elastic IP is adopted ONLY when it already carries this
    project's and environment's tags. An untagged address that happens to be
    unassociated may well be someone else's reservation.
    """
    if spec_module.wants_balancer(spec) and not spec.stable_node_ips:
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
                _associate_address(ec2, existing.get("AllocationId"),
                                   instance_id, hostname, findings)
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
            _associate_address(ec2, allocated.get("AllocationId"),
                               instance_id, hostname, findings)
    return resolved
