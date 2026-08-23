import hashlib
from unittest import mock

from objict import objict
from testit import helpers as th

from .brownfield_fixture import (
    managed_topology,
    raw_manifest,
    topology,
)


class _Clients:
    def __init__(self, client=None, **clients):
        self.client = client
        self.clients = clients

    def get(self, name):
        if self.clients:
            return self.clients[name]
        return self.client


_ClientsForBalancer = _Clients


class _NoCalls:
    def __getattr__(self, name):
        raise AssertionError(f"core safety path must not call {name}")


def _error(raw):
    from mojo.deploy.provision import brownfield_inputs, inputs

    try:
        brownfield_inputs.validate(raw)
    except inputs.EnvFileError as err:
        return str(err)
    return None



@th.django_unit_test()
def test_manifest_is_strict_secret_free_and_digest_stable(opts):
    from mojo.deploy.provision import brownfield_inputs

    first = brownfield_inputs.validate(raw_manifest())
    second = brownfield_inputs.validate(raw_manifest())
    th.assert_eq(first["manifest_digest"], second["manifest_digest"],
                 "canonical manifests must produce a stable digest")

    unknown = raw_manifest()
    unknown["network"]["guessed_vpc"] = True
    message = _error(unknown)
    th.assert_in("unknown key", message,
                 f"an unknown nested key must fail closed: {message}")

    # The same allowlist is what a stale manifest hits: a field this
    # django-mojo no longer implements is refused by name rather than
    # silently ignored, so nobody believes a retired control is still
    # enforced by something.
    retired = raw_manifest()
    retired["retired_cutover_role_arn"] = (
        "arn:aws:iam::123456789012:role/retired")
    message = _error(retired)
    th.assert_in("unknown key", message,
                 f"an unknown top-level key must fail closed: {message}")
    th.assert_in("retired_cutover_role_arn", message,
                 f"the refusal must name the offending field: {message}")

    secret = raw_manifest()
    secret["database"]["credential"]["password"] = "do-not-commit"
    message = _error(secret)
    th.assert_in("secret value", message,
                 f"a credential value must never enter the manifest: {message}")


@th.django_unit_test()
def test_to_spec_is_separate_and_managed_defaults_do_not_move(opts):
    from mojo.deploy.provision import brownfield_inputs, spec as spec_module

    managed = spec_module.build("maestro", "prod", "us-west-2", preset="small")
    before = spec_module.names(managed)
    fleet = brownfield_inputs.to_spec(
        brownfield_inputs.validate(raw_manifest()))
    th.assert_eq(spec_module.names(managed), before,
                 "building a fleet spec must not change managed topology defaults")
    th.assert_eq(spec_module.names(fleet)["nodes"],
                 ["maestro-api-1", "maestro-api-2"],
                 "brownfield nodes must come from exact declarations")
    tags = spec_module.node_tags(fleet, fleet.node_declarations[0])
    th.assert_eq(tags["mojo:fleet"], "shadow",
                 "fleet ownership must be present at resource creation")
    th.assert_eq(tags["mojo:application-role"], "api",
                 "the opaque application role must be tagged at creation")
    th.assert_true("mojo:request-service" not in tags,
                   "omission must preserve the pre-feature provider tag shape")
    explicit = dict(fleet.node_declarations[0], request_service=True)
    explicit_tags = spec_module.node_tags(fleet, explicit)
    th.assert_eq(explicit_tags["mojo:request-service"], "true",
                 "an explicit framework request role must be tagged at launch")
    th.assert_eq(spec_module.validate_names(fleet), [],
                 "a validated manifest must pass the separate fleet name seam")
    th.assert_eq(fleet.bootstrap_objects["live_config"]["version_id"],
                 "configversion1",
                 "the exact live config must survive manifest-to-spec conversion")


@th.django_unit_test()
def test_role_user_data_is_version_pinned_digest_checked_and_root_owned(opts):
    from mojo.deploy.provision import nodes

    spec = topology()
    declaration = dict(spec.node_declarations[0], request_service=True)
    script = nodes.stage0_user_data(spec, declaration["name"], declaration)
    for expected in ("MOJO_NODE_ROLE=api",
                     "--version-id=stageversion1",
                     "--version-id=configversion1",
                     "sha256sum -c -", "/etc/mojo/node-role.json",
                     "/etc/mojo/request-service.conf",
                     "MOJO_REQUEST_SERVICE=true",
                     "/etc/mojo/request-service.enabled",
                     "ConditionPathExists=/etc/mojo/request-service.enabled",
                     "00-request-service.conf",
                     "/opt/api/var/django.conf",
                     "chown ec2-user:www /opt/api/var/django.conf",
                     "chmod 0640 /opt/api/var/django.conf",
                     "chown root:root /etc/mojo/node-role.json",
                     "chmod 0600 /etc/mojo/node-role.json"):
        th.assert_in(expected, script,
                     f"brownfield user data is missing {expected!r}")
    th.assert_eq("AKIA" in script, False,
                 "no static AWS credential may enter user data")
    stage_pos = script.index("--version-id=stageversion1")
    config_pos = script.index("--version-id=configversion1")
    exec_pos = script.index("exec bash /opt/api/var/stage1.sh")
    th.assert_true(stage_pos < config_pos < exec_pos,
                   "the exact live config must be digest-checked before stage1")


@th.django_unit_test()
def test_managed_stage0_does_not_gain_fleet_role_metadata(opts):
    from mojo.deploy.provision import nodes, spec as spec_module

    managed = spec_module.build("demo", "prod", "us-west-2", preset="small")
    script = nodes.stage0_user_data(managed, "demo1")
    th.assert_eq("MOJO_NODE_ROLE" in script, False,
                 "fleet role metadata must not change managed stage-0 bytes")
    th.assert_eq("MOJO_REQUEST_SERVICE" in script, False,
                 "managed stage-0 must not gain the brownfield lifecycle key")
    th.assert_eq("/etc/mojo/request-service" in script, False,
                 "managed stage-0 must not gain fleet request-service authority")
    th.assert_eq("00-request-service.conf" in script, False,
                 "managed stage-0 must not gain the durable fleet refusal")
    th.assert_eq("MOJOCONF\n\n\n# Stage 1" in script, False,
                 "managed stage-0 layout must not gain a request-role blank line")


@th.django_unit_test()
def test_second_eip_failure_never_creates_one_az_balancer(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import balancer, report

    spec = topology()
    observed = objict(
        vpc_id=spec.brownfield_manifest["network"]["vpc_id"],
        node_records=[{"instance_id": "i-serving", "serving_target": True}],
        target_groups={}, balancer=None, addresses=[], listeners=[], targets={},
        balancer_attributes={})

    class _EC2:
        def __init__(self):
            self.calls = []

        def allocate_address(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 2:
                raise ClientError({"Error": {"Code": "AddressLimitExceeded",
                                              "Message": "limit"}},
                                  "AllocateAddress")
            return {"AllocationId": "eipalloc-first"}

    class _ELB:
        def __init__(self):
            self.load_balancers = 0

        def create_target_group(self, Name, **kwargs):
            return {"TargetGroups": [{"TargetGroupArn": f"tg/{Name}"}]}

        def create_load_balancer(self, **kwargs):
            self.load_balancers += 1
            return {"LoadBalancers": [{}]}

    ec2, elb = _EC2(), _ELB()
    findings, _actions, _result = balancer.ensure_balancer(
        _ClientsForBalancer(ec2=ec2, elbv2=elb), spec, observed, apply=True)
    th.assert_eq(len(ec2.calls), 2,
                 f"both exact address mappings must be attempted: {ec2.calls}")
    th.assert_eq(elb.load_balancers, 0,
                 "a partial address set must never create a one-AZ NLB")
    tags = {row["Key"]: row["Value"]
            for row in ec2.calls[0]["TagSpecifications"][0]["Tags"]}
    th.assert_eq(tags["Name"],
                 "maestro-shadow-nlb:subnet-0123456789abcdef0",
                 f"the retained EIP must identify its exact subnet: {tags}")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"partial mappings must block the edge step: {findings}")


@th.django_unit_test()
def test_listener_wrong_target_is_not_reported_converged(opts):
    from mojo.deploy.provision import balancer, report

    findings, actions = [], []
    observed = {"listeners": [{
        "Port": 443, "Protocol": "TCP", "DefaultActions": [{
            "Type": "forward", "TargetGroupArn": "tg/unowned"}]}]}
    balancer._ensure_listeners(
        object(), topology(), observed, "lb/owned",
        {"api": "tg/api", "certbot": "tg/http"}, findings, actions, False)
    th.assert_true(any(row.code == "listener.443.mismatch"
                       and row.status == report.MANUAL for row in findings),
                   f"wrong forwarding must be explicit drift: {findings}")
    th.assert_eq(any(row.target == "TCP:443" for row in actions), False,
                 "a colliding wrong listener must not receive a create action")


@th.django_unit_test()
def test_immutable_wrong_target_group_arn_is_withheld(opts):
    from mojo.deploy.provision import balancer, report

    spec = topology()
    wanted = balancer.target_group_specs(
        spec, spec.brownfield_manifest["network"]["vpc_id"])
    wrong = dict(wanted["api"], Protocol="UDP",
                 TargetGroupArn="tg/wrong")
    findings, actions = [], []
    arns = balancer._ensure_target_groups(
        object(), spec, {"target_groups": {"api": wrong}}, wanted,
        findings, actions, apply=False)
    th.assert_eq(arns.get("api"), None,
                 "an immutable-mismatch target group must not reach listeners")
    th.assert_true(any(row.code == "target_group.api.immutable"
                       and row.status == report.BLIND for row in findings),
                   f"the wrong group must block fleet convergence: {findings}")


@th.django_unit_test()
def test_unowned_exact_name_balancer_collision_is_not_exposed(opts):
    from mojo.deploy.provision import brownfield_discover, report

    class _ELB:
        def describe_tags(self, ResourceArns):
            return {"TagDescriptions": [{"ResourceArn": ResourceArns[0],
                                         "Tags": [{"Key": "Name",
                                                   "Value": "collision"}]}]}

    findings = []
    accepted = brownfield_discover._owned_elbv2(
        _ELB(), topology(), "arn:aws:elasticloadbalancing:us-west-2:"
        "123456789012:loadbalancer/net/maestro-shadow-nlb/abc", findings,
        "load balancer")
    th.assert_eq(accepted, False,
                 "an exact-name resource without fleet tags is not adoptable")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the collision must block apply, not look missing: {findings}")


@th.django_unit_test()
def test_unowned_node_name_collision_never_enters_node_convergence(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    row = {
        "InstanceId": "i-aaaaaaaaaaaaaaaaa", "VpcId": manifest["network"]["vpc_id"],
        "SubnetId": declaration["subnet_id"],
        "Placement": {"AvailabilityZone": declaration["availability_zone"]},
        "State": {"Name": "running"},
        "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
        "SecurityGroups": [{"GroupId": manifest["network"]["node_security_group_id"]}],
        "Tags": [{"Key": "Name", "Value": declaration["name"]}],
    }

    class _EC2:
        def describe_instances(self, **kwargs):
            return {"Reservations": [{"Instances": [row]}]}

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        "api": {"profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)
    th.assert_eq(list(observed.instances), [],
                 "the unowned collision must not be handed to ensure_nodes")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the collision must block the dependency step: {findings}")


@th.django_unit_test()
def test_second_page_duplicate_node_prevents_adoption(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["compatibility_instance_ids"] = []
    declaration = manifest["nodes"]["items"][0]
    tags = [
        {"Key": "Name", "Value": declaration["name"]},
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "node"},
        {"Key": "mojo:application-role", "Value": declaration["role"]},
    ]

    class _EC2:
        def describe_instances(self, **kwargs):
            suffix = "b" if kwargs.get("NextToken") else "a"
            answer = {"Reservations": [{"Instances": [{
                "InstanceId": f"i-{suffix * 17}", "Tags": tags,
            }]}]}
            if not kwargs.get("NextToken"):
                answer["NextToken"] = "next"
            return answer

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        declaration["role"]: {
            "profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)
    th.assert_eq(list(observed.instances), [],
                 "duplicate names on a later page must never be adopted")
    th.assert_true(any(
        row.status == report.BLIND and "match count" in row.message
        for row in findings),
        f"the complete-set duplicate must block convergence: {findings}")


@th.django_unit_test()
def test_node_observation_validates_vpc_subnet_profile_role_and_security_group(opts):
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
    row = {
        "InstanceId": "i-aaaaaaaaaaaaaaaaa", "VpcId": "vpc-deadbeef",
        "SubnetId": declaration["subnet_id"],
        "Placement": {"AvailabilityZone": declaration["availability_zone"]},
        "State": {"Name": "running"},
        "IamInstanceProfile": {"Arn": declaration["instance_profile_arn"]},
        "SecurityGroups": [{"GroupId": manifest["network"]["node_security_group_id"]}],
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }

    class _EC2:
        def describe_instances(self, **kwargs):
            return {"Reservations": [{"Instances": [row]}]}

    findings, observed, inventory = [], objict(), {}
    observed.brownfield_profiles = {
        "api": {"profile_arn": declaration["instance_profile_arn"]}}
    brownfield_discover._instances(
        _Clients(ec2=_EC2()), spec, manifest, findings, observed, inventory)
    th.assert_eq(list(observed.instances), [],
                 "a wrong-VPC node must never become an owned node")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"a wrong VPC must block apply: {findings}")


@th.django_unit_test()
def test_credential_metadata_key_and_application_user_are_enforced(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    credential = spec.brownfield_manifest["database"]["credential"]

    class _S3:
        def __init__(self, metadata):
            self.metadata = metadata

        def head_object(self, **kwargs):
            return {"VersionId": credential["object"]["version_id"],
                    "Metadata": dict(self.metadata,
                                     sha256=credential["object"]["sha256"]),
                    "ETag": "etag", "ContentLength": 10}

    for metadata, expected_phrase in (({}, "metadata key"),
                                      ({"application-user": "wrong"},
                                       "application user metadata")):
        findings, inventory = [], {}
        brownfield_discover._credential_metadata(
            _Clients(s3=_S3(metadata)), credential, findings,
            "database credential", inventory,
            spec.brownfield_manifest["account_id"],
            expected_metadata_value=spec.brownfield_manifest[
                "database"]["application_user"])
        th.assert_true(any(expected_phrase in item.message
                           and item.status == report.BLIND for item in findings),
                       f"{expected_phrase} must fail closed: {findings}")


@th.django_unit_test()
def test_owned_iam_role_with_admin_policy_is_rejected(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = managed_topology()
    tags = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "identity"},
        {"Key": "mojo:application-role", "Value": "api"},
    ]
    role_arn = "arn:aws:iam::123456789012:role/maestro-shadow-api"

    class _IAM:
        def get_instance_profile(self, **kwargs):
            return {"InstanceProfile": {
                "Arn": "arn:aws:iam::123456789012:instance-profile/maestro-shadow-api",
                "Tags": tags, "Roles": [{"Arn": role_arn}]}}

        def get_role(self, **kwargs):
            return {"Role": {"Arn": role_arn, "Tags": tags,
                             "AssumeRolePolicyDocument":
                                 brownfield_discover.EC2_TRUST}}

        def list_role_policies(self, **kwargs):
            return {"PolicyNames": ["maestro-shadow-api-runtime"]}

        def list_attached_role_policies(self, **kwargs):
            return {"AttachedPolicies": [{
                "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]}

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._profiles(
        _Clients(iam=_IAM()), spec.brownfield_manifest, findings, observed,
        inventory)
    row = observed.brownfield_profiles["api"]
    th.assert_true(row.role_collision and row.profile_collision,
                   f"a broadened owned role must be withheld: {row}")
    th.assert_eq(row.role_arn, None,
                 "AdministratorAccess must never reach node launch")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"the broadened role must block apply: {findings}")


@th.django_unit_test()
def test_pagination_merges_complete_results_and_discards_partial_failures(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import brownfield_discover, report

    class _EC2:
        def __init__(self, pages):
            self.pages = iter(pages)

        def describe_vpcs(self, **kwargs):
            page = next(self.pages)
            if isinstance(page, Exception):
                raise page
            return page

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "ec2.describe_vpcs", _EC2([
            {"Vpcs": [{"VpcId": "vpc-first"}], "NextToken": "next"},
            {"Vpcs": [{"VpcId": "vpc-second"}]},
        ]))
    th.assert_eq([row["VpcId"] for row in answer["Vpcs"]],
                 ["vpc-first", "vpc-second"],
                 "every provider page must enter the dependency inventory")
    th.assert_eq(findings, [],
                 "complete pagination must not add a blocking finding")

    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "later page"}},
        "DescribeVpcs")
    findings = []
    answer = brownfield_discover._read_pages(
        findings, "ec2.describe_vpcs", _EC2([
            {"Vpcs": [{"VpcId": "vpc-first"}], "NextToken": "next"},
            error,
        ]))
    th.assert_eq(answer, {"Vpcs": []},
                 "a failed later page must discard every partial result")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"a later-page failure must block apply: {findings}")

    for token in (None, "again"):
        pages = [{"Vpcs": [{"VpcId": "vpc-first"}],
                  "NextToken": "again"}]
        if token == "again":
            pages.append({"Vpcs": [{"VpcId": "vpc-second"}],
                          "NextToken": "again"})
        else:
            pages = [{"Vpcs": [{"VpcId": "vpc-first"}],
                      "NextToken": token}]
        findings = []
        answer = brownfield_discover._read_pages(
            findings, "ec2.describe_vpcs", _EC2(pages))
        if token is None:
            th.assert_eq(answer, {"Vpcs": [{"VpcId": "vpc-first"}]},
                         "an absent token without a continuation claim is final")
            th.assert_eq(findings, [],
                         "a provider may finish by omitting its token")
        else:
            th.assert_eq(answer, {"Vpcs": []},
                         "a repeated token must discard partial inventory")
            th.assert_true(any(
                row.code == "dependency.enumeration_truncated"
                for row in findings),
                f"a repeated token must fail closed: {findings}")

    class _KMS:
        def list_grants(self, **kwargs):
            return {"Grants": [{"GrantId": "partial"}], "Truncated": True}

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "kms.list_grants", _KMS(), {"KeyId": "alias/test"})
    th.assert_eq(answer, {"Grants": []},
                 "a missing required continuation token must discard results")
    th.assert_true(any(
        row.code == "dependency.enumeration_truncated" for row in findings),
        f"a missing continuation token must fail closed: {findings}")


@th.django_unit_test()
def test_second_page_iam_policy_collision_is_rejected(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = managed_topology()
    manifest = spec.brownfield_manifest
    managed = manifest["nodes"]["profiles"]["api"]["managed"]
    tags = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "mojo:project", "Value": spec.project},
        {"Key": "mojo:env", "Value": spec.env},
        {"Key": "mojo:fleet", "Value": spec.fleet},
        {"Key": "mojo:role", "Value": "identity"},
        {"Key": "mojo:application-role", "Value": "api"},
    ]
    role_arn = f"arn:aws:iam::123456789012:role/{managed['role_name']}"

    class _IAM:
        def get_instance_profile(self, **kwargs):
            return {"InstanceProfile": {
                "Arn": ("arn:aws:iam::123456789012:instance-profile/"
                        f"{managed['profile_name']}"),
                "Tags": tags, "Roles": [{"Arn": role_arn}]}}

        def get_role(self, **kwargs):
            return {"Role": {"Arn": role_arn, "Tags": tags,
                             "AssumeRolePolicyDocument":
                                 brownfield_discover.EC2_TRUST}}

        def list_role_policies(self, **kwargs):
            if kwargs.get("Marker"):
                return {"PolicyNames": ["unexpected-admin"],
                        "IsTruncated": False}
            return {"PolicyNames": [f"{managed['role_name']}-runtime"],
                    "IsTruncated": True, "Marker": "next"}

        def list_attached_role_policies(self, **kwargs):
            return {"AttachedPolicies": [], "IsTruncated": False}

    findings, observed, inventory = [], objict(), {}
    brownfield_discover._profiles(
        _Clients(iam=_IAM()), manifest, findings, observed, inventory)
    row = observed.brownfield_profiles["api"]
    th.assert_true(row.role_collision and row.profile_collision,
                   f"a second-page policy must withhold the role: {row}")
    th.assert_true(any(item.status == report.BLIND for item in findings),
                   f"the second-page policy collision must block: {findings}")


@th.django_unit_test()
def test_missing_elb_resource_remains_an_optional_empty_collection(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import brownfield_discover, report

    class _ELB:
        def describe_load_balancers(self, **kwargs):
            raise ClientError({"Error": {
                "Code": "LoadBalancerNotFound", "Message": "not found"}},
                "DescribeLoadBalancers")

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "elbv2.describe_load_balancers", _ELB(),
        {"Names": ["missing"]}, not_found=True)
    th.assert_eq(answer, {"LoadBalancers": []},
                 "an absent optional balancer must remain creatable")
    th.assert_eq(findings, [],
                 "normal ELB absence must not be reported as blind")

    class _VanishingELB:
        def describe_load_balancers(self, **kwargs):
            if not kwargs.get("Marker"):
                return {"LoadBalancers": [{"LoadBalancerArn": "partial"}],
                        "NextMarker": "next"}
            raise ClientError({"Error": {
                "Code": "LoadBalancerNotFound", "Message": "vanished"}},
                "DescribeLoadBalancers")

    findings = []
    answer = brownfield_discover._read_pages(
        findings, "elbv2.describe_load_balancers", _VanishingELB(),
        {"Names": ["vanishing"]}, not_found=True)
    th.assert_eq(answer, {"LoadBalancers": []},
                 "later-page absence must discard partial edge evidence")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"later-page absence must fail closed: {findings}")


@th.django_unit_test()
def test_owned_eip_ambiguity_and_wrong_border_group_fail_closed(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    subnet = spec.nlb_subnet_ids[0]
    name = f"{spec.nlb_name}:{subnet}"
    tags = {
        "Name": name, "managed-by": "django-mojo",
        "mojo:project": spec.project, "mojo:env": spec.env,
        "mojo:fleet": spec.fleet, "mojo:role": "balancer",
    }
    rows = [{
        "AllocationId": f"eipalloc-{index}", "Domain": "vpc",
        "NetworkBorderGroup": "us-east-1",
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    } for index in (1, 2)]
    findings = []
    accepted = brownfield_discover._owned_addresses(
        spec, spec.brownfield_manifest, rows, None, findings)
    th.assert_eq(accepted, [],
                 "ambiguous subnet-bound addresses must not enter NLB creation")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"ambiguity/border drift must block preparation: {findings}")


@th.django_unit_test()
def test_node_ingress_tracks_declared_client_ip_posture(opts):
    from mojo.deploy.provision import brownfield_discover, report

    world = {"IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        for port in (80, 443)]}
    private = {"IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}, {
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
        "IpRanges": [{"CidrIp": "172.31.0.0/16"}]}]}

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, world, "172.31.0.0/16", {})
    th.assert_eq(findings, [],
                 "omission must preserve the existing world-ingress contract")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, private, "172.31.0.0/16",
        {"api_preserve_client_ip": False})
    th.assert_eq(findings, [],
                 "disabled preservation must accept exact VPC-only API ingress")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, world, "172.31.0.0/16",
        {"api_preserve_client_ip": False})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"world ingress must block a private-source declaration: {findings}")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, {"IpPermissions": []}, "172.31.0.0/16",
        {"api_preserve_client_ip": False})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"missing VPC ingress must block NLB target launch: {findings}")

    nlb_group_id = "sg-nlb"
    node_group_id = "sg-node"
    locked_node = {"GroupId": node_group_id, "IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "UserIdGroupPairs": [{"GroupId": nlb_group_id}]}
        for port in (80, 443)]}
    public_nlb = {"GroupId": nlb_group_id, "IpPermissions": [{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        for port in (80, 443)], "IpPermissionsEgress": [{
            "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
            "UserIdGroupPairs": [{"GroupId": node_group_id}]}
            for port in (80, 443)]}
    findings = []
    brownfield_discover._validate_node_ingress(
        findings, locked_node, "172.31.0.0/16",
        {"security_group_id": nlb_group_id,
         "api_preserve_client_ip": True,
         "certbot_preserve_client_ip": True},
        {nlb_group_id: public_nlb})
    th.assert_eq(findings, [],
                 "an exact NLB-SG boundary must preserve client IPs safely")

    ipv6_only = dict(public_nlb, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
        "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}
        for port in (80, 443)])
    findings = []
    brownfield_discover._validate_node_ingress(
        findings, locked_node, "172.31.0.0/16",
        {"security_group_id": nlb_group_id},
        {nlb_group_id: ipv6_only})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"IPv6-only ingress cannot prove an IPv4 NLB public: {findings}")

    findings = []
    brownfield_discover._validate_node_ingress(
        findings, world, "172.31.0.0/16",
        {"security_group_id": nlb_group_id},
        {nlb_group_id: public_nlb})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the NLB SG must never coexist with direct world target ingress: {findings}")

    narrowed_direct = dict(locked_node, IpPermissions=(
        list(locked_node["IpPermissions"]) + [{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "IpRanges": [{"CidrIp": "198.51.100.0/24"}]}]))
    findings = []
    brownfield_discover._validate_node_ingress(
        findings, narrowed_direct, "172.31.0.0/16",
        {"security_group_id": nlb_group_id},
        {nlb_group_id: public_nlb})
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"any non-NLB source on a target port must block: {findings}")


@th.django_unit_test()
def test_existing_nlb_must_carry_the_declared_create_time_security_group(opts):
    from mojo.deploy.provision import brownfield_discover, report

    spec = topology()
    manifest = spec.brownfield_manifest
    manifest["load_balancer"]["security_group_id"] = (
        "sg-3123456789abcdef0")
    balancer = {
        "Type": "network", "Scheme": "internet-facing",
        "AvailabilityZones": [{"SubnetId": subnet_id}
                              for subnet_id in spec.nlb_subnet_ids],
        "SecurityGroups": ["sg-4123456789abcdef0"],
    }
    findings = []
    accepted = brownfield_discover._validate_balancer_shape(
        findings, balancer, manifest)
    th.assert_eq(accepted, False,
                 "an NLB with a wrong/missing first SG must never be adopted")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the irreversible SG mismatch must block apply: {findings}")

    balancer["SecurityGroups"] = ["sg-3123456789abcdef0"]
    findings = []
    accepted = brownfield_discover._validate_balancer_shape(
        findings, balancer, manifest)
    th.assert_true(accepted,
                   f"the exact declared NLB SG must be reusable: {findings}")


@th.django_unit_test()
def test_managed_iam_name_collision_performs_no_mutation(opts):
    from mojo.deploy.provision import brownfield_identity, report

    observed = {"brownfield_profiles": {"api": {
        "role_collision": True, "profile_collision": False,
        "role_arn": None, "profile_arn": None}}}
    findings, actions, result = brownfield_identity.ensure_identity(
        _Clients(_NoCalls()), managed_topology(), observed, apply=True)
    th.assert_eq(actions, [],
                 "an unowned collision must not advertise or attempt a mutation")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the collision must block downstream nodes: {findings}")
    th.assert_eq(result.as_dict()["brownfield_profiles"], {},
                 "no colliding profile may be exposed downstream")


@th.django_unit_test()
def test_runtime_policy_authorizes_only_exact_versioned_artifacts(opts):
    from mojo.deploy.provision import brownfield_identity

    policy = brownfield_identity.policy_document(managed_topology())
    statements = {row["Sid"]: row for row in policy["Statement"]}
    pinned = statements["ReadPinnedFleetArtifacts"]
    th.assert_eq(pinned["Action"], ["s3:GetObjectVersion"],
                 f"version-pinned downloads need GetObjectVersion: {pinned}")
    expected = {
        "arn:aws:s3:::maestro-prod-config/bootstrap/stage1.sh",
        "arn:aws:s3:::maestro-prod-config/config/live/django.conf",
        "arn:aws:s3:::maestro-prod-config/bootstrap/node-role.json",
        "arn:aws:s3:::maestro-prod-config/secrets/db.json",
    }
    th.assert_eq(set(pinned["Resource"]), expected,
                 f"only exact bootstrap/credential keys may be version-read: {pinned}")
    prefixes = statements["ReadDeclaredFleetPrefixes"]
    th.assert_eq(prefixes["Action"], ["s3:GetObject"],
                 f"unversioned GetObject must stay in its own prefix grant: {prefixes}")
    th.assert_eq(any(value.endswith("bootstrap/*")
                     for value in prefixes["Resource"]), False,
                 f"bootstrap must not receive a broad unversioned grant: {prefixes}")


@th.django_unit_test()
def test_telemetry_collisions_never_mutate(opts):
    from mojo.deploy.provision import brownfield_observability, report

    spec = topology()
    group_names = [f"/mojo/{spec.project}-{spec.fleet}/{kind}"
                   for kind in brownfield_observability.LOG_KINDS]
    alarm_names = [f"{spec.project}-{spec.fleet}-{role}-unhealthy"
                   for role in ("api", "certbot")]
    observed = {"log_groups": {}, "log_group_collisions": group_names,
                "brownfield_alarms": [], "alarm_collisions": alarm_names}
    findings, actions, _result = brownfield_observability.ensure_observability(
        _Clients(logs=_NoCalls(), cloudwatch=_NoCalls()), spec, observed,
        apply=True)
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"collisions must block the telemetry step: {findings}")
    th.assert_eq(any(action.target in group_names + alarm_names
                     for action in actions), False,
                 "colliding names must not receive create/modify actions")


@th.django_unit_test()
def test_apply_reobserves_and_refuses_dependency_digest_drift(opts):
    from mojo.deploy.provision import brownfield_plan

    run = objict(observed=objict(dependency_digest="changed",
                                action_digest="actions"), blocking=False,
                 validated=True, steps=objict(), worst="PASS", problems=[])
    with mock.patch.object(brownfield_plan, "_prepare",
                           return_value=([], [], run)) as prepared:
        raised = None
        try:
            brownfield_plan.apply(object(), topology(), "previewed", "actions")
        except brownfield_plan.DependencyDriftError as err:
            raised = err
    th.assert_true(raised is not None,
                   "a dependency change must abort before the first mutation")
    th.assert_in("nothing was mutated", str(raised),
                 f"the refusal must state the safety outcome: {raised}")
    th.assert_eq(prepared.call_count, 1,
                 "apply must perform one fresh exact observation")


@th.django_unit_test()
def test_apply_refuses_changed_preview_action_digest(opts):
    from mojo.deploy.provision import brownfield_plan

    run = objict(observed=objict(dependency_digest="dependencies",
                                action_digest="new-actions"), blocking=False,
                 validated=True, steps=objict(), worst="PASS", problems=[])
    with mock.patch.object(brownfield_plan, "_prepare",
                           return_value=([], [], run)):
        raised = None
        try:
            brownfield_plan.apply(
                object(), topology(), "dependencies", "confirmed-actions")
        except brownfield_plan.DependencyDriftError as err:
            raised = err
    th.assert_true(raised is not None,
                   "a new allowed action after confirmation must abort apply")
    th.assert_in("action set changed", str(raised),
                 f"the refusal must name action drift: {raised}")
    th.assert_in("nothing was mutated", str(raised),
                 f"the CAS failure must state the safety outcome: {raised}")
