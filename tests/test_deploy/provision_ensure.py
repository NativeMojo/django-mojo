"""The ensure-services, against `botocore.stub.Stubber`.

Stubber rather than a Mock, for the reason `tests/test_aws/capacity.py` gives:
a Mock accepts any keyword, so it would happily take `TagSpecifcations` spelled
wrong, a `HealthCheckProtocol` that is not legal on a TCP target group, or a
`CreateBucketConfiguration` shaped for a different API version. Stubber validates
every request against the real service model before the network would.

TWO ASSERTIONS APPEAR IN NEARLY EVERY TEST HERE, and both matter:

    stubber.assert_no_pending_responses()   nothing we queued went uncalled
    _assert_no_blind(findings)              nothing UNEXPECTED was called

The second one is not decoration. `report.safe` catches BotoCoreError, and
botocore's `UnStubbedResponseError` and `ParamValidationError` are both
BotoCoreErrors — so a call this package should not have made, or a request the
service model rejects, arrives as a quiet BLIND finding rather than as an
exception. Checking for BLIND is what turns those back into test failures.

It is also how `apply=False` is proved to cost nothing: a dry run against a
Stubber with no responses queued produces no BLIND findings only if it made no
calls at all.
"""

import os

from testit import helpers as th


REGION = "us-west-2"
PROJECT = "wmx"
ENV = "prod"
ACCOUNT = "123456789012"
VPC_ID = "vpc-0aaa1111"
ZONES = ("us-west-2a", "us-west-2b", "us-west-2c")


# ── fixtures ────────────────────────────────────────────────────────────────

def _stub(service):
    """A real bounded client plus its Stubber. Credentials are never used."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name=REGION,
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def _spec(**overrides):
    from mojo.deploy.provision import spec as spec_module
    preset = overrides.pop("preset", "small")
    overrides.setdefault("account_id", ACCOUNT)
    return spec_module.build(PROJECT, ENV, REGION, preset=preset, **overrides)


def _clients(**overrides):
    from mojo.deploy.provision import discover
    return discover.Clients(session=None, **overrides)


def _observed(**overrides):
    from mojo.deploy.provision import discover
    observed = discover.blank()
    observed.account_id = ACCOUNT
    observed.region = REGION
    observed.azs = [{"ZoneName": zone} for zone in ZONES]
    observed.offered_zone_names = list(ZONES)
    observed.update(overrides)
    return observed


def _assert_no_blind(findings, msg):
    from mojo.deploy.provision import report

    blind = [f"{f.code}: {f.message}" for f in findings
             if f.status == report.BLIND]
    th.assert_eq(blind, [], msg)


def _codes(findings, status=None):
    return [f.code for f in findings if status is None or f.status == status]


def _tagged(spec, role, name=None):
    from mojo.deploy.provision import spec as spec_module
    return spec_module.tag_list(spec, role, name=name)


# ── network ─────────────────────────────────────────────────────────────────

@th.django_unit_test("availability zones are filtered by what actually offers the instance type")
def test_network_filters_azs_by_instance_type_offering(opts):
    from mojo.deploy.provision import network

    spec = _spec()
    observed = _observed(offered_zone_names=["us-west-2b", "us-west-2c"])
    th.assert_eq(network.usable_azs(spec, observed),
                 ["us-west-2b", "us-west-2c"],
                 "a zone the account can see but which does not offer the "
                 "preset's instance type must not be chosen — the failure "
                 "would otherwise surface at run_instances, against a subnet "
                 "this tool cannot delete")


@th.django_unit_test("too few offering zones stops before a single subnet is created")
def test_network_stops_when_too_few_zones_offer_the_instance_type(opts):
    from mojo.deploy.provision import network, report

    spec = _spec()
    observed = _observed(offered_zone_names=["us-west-2c"])
    client, stubber = _stub("ec2")
    with stubber:
        findings, actions, result = network.ensure_vpc(
            _clients(ec2=client), spec, observed, apply=True)

    th.assert_in("az.insufficient", _codes(findings, report.MANUAL),
                 f"one usable zone must be reported as MANUAL: "
                 f"{_codes(findings)}")
    th.assert_eq(actions, [],
                 "nothing may be planned or created when the zones are wrong")
    _assert_no_blind(findings, "no AWS call may be attempted at all")
    stubber.assert_no_pending_responses()


@th.django_unit_test("a dry run of the network step calls AWS not once")
def test_network_records_without_calling_aws_when_not_applying(opts):
    from mojo.deploy.provision import network, report

    client, stubber = _stub("ec2")
    with stubber:
        findings, actions, result = network.ensure_vpc(
            _clients(ec2=client), _spec(), _observed(), apply=False)

    _assert_no_blind(findings,
                     "apply=False must make no AWS calls — every read lives in "
                     "discover.observe, which is what makes a dry run free")
    th.assert_true(actions,
                   "a dry run against an empty account must still report what "
                   "apply would do")
    th.assert_in("vpc.missing", _codes(findings, report.MISSING),
                 f"an empty account is missing a VPC: {_codes(findings)}")
    stubber.assert_no_pending_responses()


@th.django_unit_test("the network step creates exactly what is missing, tagged in the create call")
def test_network_creates_exactly_what_is_missing(opts):
    from mojo.deploy.provision import network
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("ec2")

    # Expected params on the two creates that matter: the tags go on in the
    # SAME call. A create followed by a separate create_tags would leave an
    # untagged resource on an interrupted run, and this package never adopts an
    # untagged resource — so the next run would build a second one.
    stubber.add_response("create_vpc", {"Vpc": {"VpcId": VPC_ID}}, {
        "CidrBlock": spec_module.VPC_CIDR,
        "TagSpecifications": spec_module.tag_specifications(
            spec, "network", "vpc", name=names["vpc"])})
    stubber.add_response("modify_vpc_attribute", {})
    stubber.add_response("modify_vpc_attribute", {})
    stubber.add_response(
        "create_subnet", {"Subnet": {"SubnetId": "subnet-0pub1"}}, {
            "VpcId": VPC_ID,
            "CidrBlock": spec_module.PUBLIC_SUBNET_CIDRS[0],
            "AvailabilityZone": ZONES[0],
            "TagSpecifications": spec_module.tag_specifications(
                spec, "network", "subnet", name=names["public_subnets"][0])})
    stubber.add_response("modify_subnet_attribute", {})
    stubber.add_response("create_subnet", {"Subnet": {"SubnetId": "subnet-0pub2"}})
    stubber.add_response("modify_subnet_attribute", {})
    stubber.add_response("create_subnet", {"Subnet": {"SubnetId": "subnet-0prv1"}})
    stubber.add_response("create_subnet", {"Subnet": {"SubnetId": "subnet-0prv2"}})
    stubber.add_response(
        "create_internet_gateway",
        {"InternetGateway": {"InternetGatewayId": "igw-0aaa"}})
    stubber.add_response("attach_internet_gateway", {})
    stubber.add_response("create_route_table",
                         {"RouteTable": {"RouteTableId": "rtb-0pub"}})
    stubber.add_response("create_route", {"Return": True})
    stubber.add_response("associate_route_table", {"AssociationId": "rtbassoc-1"})
    stubber.add_response("associate_route_table", {"AssociationId": "rtbassoc-2"})
    stubber.add_response("create_route_table",
                         {"RouteTable": {"RouteTableId": "rtb-0prv"}})
    stubber.add_response("associate_route_table", {"AssociationId": "rtbassoc-3"})
    stubber.add_response("associate_route_table", {"AssociationId": "rtbassoc-4"})
    stubber.add_response("create_vpc_endpoint",
                         {"VpcEndpoint": {"VpcEndpointId": "vpce-0aaa"}})

    with stubber:
        findings, actions, result = network.ensure_vpc(
            _clients(ec2=client), spec, _observed(), apply=True)

    _assert_no_blind(findings,
                     "every request must be one the EC2 service model accepts")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["vpc_id"], VPC_ID, "the new VPC id must be returned")
    th.assert_eq(result["public_subnet_ids"], ["subnet-0pub1", "subnet-0pub2"],
                 "both public subnet ids must be returned for the balancer and "
                 "the nodes to use")
    th.assert_eq(result["private_subnet_ids"], ["subnet-0prv1", "subnet-0prv2"],
                 "both private subnet ids must be returned — Aurora needs two")
    th.assert_eq(result["azs"], [ZONES[0], ZONES[1]],
                 "the chosen zones must be recorded and stable")


@th.django_unit_test("a converged network is a genuine no-op on a second apply")
def test_network_is_a_noop_when_converged(opts):
    from mojo.deploy.provision import network, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        vpc={"VpcId": VPC_ID, "CidrBlock": spec_module.VPC_CIDR},
        subnets=[
            {"SubnetId": "subnet-0pub1", "AvailabilityZone": ZONES[0],
             "CidrBlock": spec_module.PUBLIC_SUBNET_CIDRS[0],
             "Tags": [{"Key": "Name", "Value": names["public_subnets"][0]}]},
            {"SubnetId": "subnet-0pub2", "AvailabilityZone": ZONES[1],
             "CidrBlock": spec_module.PUBLIC_SUBNET_CIDRS[1],
             "Tags": [{"Key": "Name", "Value": names["public_subnets"][1]}]},
            {"SubnetId": "subnet-0prv1", "AvailabilityZone": ZONES[0],
             "CidrBlock": spec_module.PRIVATE_SUBNET_CIDRS[0],
             "Tags": [{"Key": "Name", "Value": names["private_subnets"][0]}]},
            {"SubnetId": "subnet-0prv2", "AvailabilityZone": ZONES[1],
             "CidrBlock": spec_module.PRIVATE_SUBNET_CIDRS[1],
             "Tags": [{"Key": "Name", "Value": names["private_subnets"][1]}]},
        ],
        internet_gateway={"InternetGatewayId": "igw-0aaa"},
        route_tables=[
            {"RouteTableId": "rtb-0pub",
             "Routes": [{"DestinationCidrBlock": "0.0.0.0/0"}],
             "Associations": [{"SubnetId": "subnet-0pub1"},
                              {"SubnetId": "subnet-0pub2"}],
             "Tags": [{"Key": "Name", "Value": names["public_route_table"]}]},
            {"RouteTableId": "rtb-0prv", "Routes": [],
             "Associations": [{"SubnetId": "subnet-0prv1"},
                              {"SubnetId": "subnet-0prv2"}],
             "Tags": [{"Key": "Name", "Value": names["private_route_table"]}]},
        ],
        vpc_endpoints=[{"VpcEndpointId": "vpce-0aaa",
                        "ServiceName": f"com.amazonaws.{REGION}.s3"}])

    client, stubber = _stub("ec2")
    with stubber:
        findings, actions, result = network.ensure_vpc(
            _clients(ec2=client), spec, observed, apply=True)

    _assert_no_blind(findings,
                     "a converged account must produce no AWS calls at all — "
                     "this is the property the whole no-state-file design "
                     "rests on")
    th.assert_eq(actions, [],
                 f"a second apply against a converged network must plan "
                 f"nothing: {[a.target for a in actions]}")
    th.assert_eq([f.status for f in findings if f.status != report.PASS], [],
                 f"everything must read as PASS: "
                 f"{[(f.code, f.status) for f in findings]}")
    stubber.assert_no_pending_responses()


@th.django_unit_test("an immutable subnet CIDR is reported, never modified")
def test_network_reports_manual_for_an_immutable_field_drift(opts):
    from mojo.deploy.provision import network, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    observed = _observed(vpc={"VpcId": VPC_ID, "CidrBlock": "10.9.0.0/16"})
    client, stubber = _stub("ec2")
    with stubber:
        findings, actions, result = network.ensure_vpc(
            _clients(ec2=client), spec, observed, apply=True)

    manual = [f for f in findings if f.status == report.MANUAL
              and f.code == "vpc.cidr"]
    th.assert_true(manual,
                   f"a VPC whose primary CIDR differs must be MANUAL — there "
                   f"is no modify call for it and attempting one raises: "
                   f"{_codes(findings)}")
    th.assert_true(manual[0].remedy,
                   "a MANUAL finding without a remedy tells an operator "
                   "nothing they can act on")
    th.assert_eq(
        [a for a in actions if a.verb == "modify" and a.target == VPC_ID], [],
        "no modify may even be planned against an immutable field")


@th.django_unit_test("missing ingress is added; extra ingress is reported, never revoked")
def test_security_groups_add_missing_and_report_extra(opts):
    from mojo.deploy.provision import network, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec(admin_cidrs=["203.0.113.0/24"])
    names = spec_module.names(spec)
    observed = _observed(
        vpc_id=VPC_ID,
        security_groups={
            "node": {"GroupId": "sg-0node", "GroupName": names["node_sg"],
                     "IpPermissions": [
                         {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                          "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                         {"IpProtocol": "tcp", "FromPort": 3306,
                          "ToPort": 3306,
                          "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
            "rds": {"GroupId": "sg-0rds", "GroupName": names["rds_sg"],
                    "IpPermissions": [
                        {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                         "UserIdGroupPairs": [{"GroupId": "sg-0node"}]}]},
            "cache": {"GroupId": "sg-0cache", "GroupName": names["cache_sg"],
                      "IpPermissions": [
                          {"IpProtocol": "tcp", "FromPort": 6379,
                           "ToPort": 6379,
                           "UserIdGroupPairs": [{"GroupId": "sg-0node"}]}]},
        })

    client, stubber = _stub("ec2")
    # The node group is missing :80 and :22; both go on in ONE authorize call.
    stubber.add_response("authorize_security_group_ingress", {"Return": True})
    with stubber:
        findings, actions, result = network.ensure_security_groups(
            _clients(ec2=client), spec, observed, apply=True)

    _assert_no_blind(findings, "the authorize request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_in("sg.node.rules", _codes(findings, report.DRIFT),
                 f"the two missing node rules must be reported as drift: "
                 f"{_codes(findings)}")
    th.assert_in("sg.node.extra", _codes(findings, report.MANUAL),
                 f"the stray :3306 rule must be MANUAL — revoking is a "
                 f"destructive call this package will not make: "
                 f"{_codes(findings)}")
    th.assert_eq(result["node_sg_id"], "sg-0node",
                 "the node security group id must be returned for the database "
                 "and cache rules to reference")


# ── identity ────────────────────────────────────────────────────────────────

@th.django_unit_test("a key pair with no stored private key is reported, never recreated")
def test_identity_reports_a_key_pair_without_a_local_pem(opts):
    from mojo.deploy.provision import identity, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(key_pair={"KeyName": names["key_pair"],
                                   "KeyPairId": "key-0aaa"},
                         secrets={"db_password": "x"})

    client, stubber = _stub("ec2")
    with stubber:
        findings, actions, result = identity.ensure_key_pair(
            _clients(ec2=client), spec, observed, apply=True)

    th.assert_in("key_pair.no_private_material", _codes(findings, report.MANUAL),
                 f"a key AWS holds and we cannot log in with is MANUAL: "
                 f"{_codes(findings)}")
    th.assert_eq(actions, [],
                 "recreating would mean deleting, which this package does not "
                 "do, and would lock out every node already trusting the key")
    _assert_no_blind(findings, "no AWS call may be made in this case")
    th.assert_eq(result["key_pair_created"], False,
                 "nothing was created")
    stubber.assert_no_pending_responses()


@th.django_unit_test("a generated key pair whose private key cannot be stored fails the step")
def test_identity_generated_key_pair_write_failure_fails_the_step(opts):
    from mojo.deploy.provision import identity, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(config_bucket=names["config_bucket"], secrets={})

    ec2_client, ec2_stub = _stub("ec2")
    s3_client, s3_stub = _stub("s3")
    ec2_stub.add_response("create_key_pair", {
        "KeyName": names["key_pair"], "KeyPairId": "key-0aaa",
        "KeyFingerprint": "aa:bb", "KeyMaterial": "PRIVATE-KEY-MATERIAL"})
    s3_stub.add_client_error("put_object", service_error_code="AccessDenied",
                             http_status_code=403)

    with ec2_stub, s3_stub:
        findings, actions, result = identity.ensure_key_pair(
            _clients(ec2=ec2_client, s3=s3_client), spec, observed, apply=True)

    codes = _codes(findings, report.BLIND)
    th.assert_in("key_pair.material_lost", codes,
                 f"AWS returns a generated private key exactly once; a failed "
                 f"write must fail the step loudly rather than leave a key "
                 f"nobody holds: {_codes(findings)}")
    th.assert_eq("key_pair_name" in result, False,
                 "a step that lost the private key must not report the key "
                 "pair as usable to the steps that follow it")
    lost = [f for f in findings if f.code == "key_pair.material_lost"][0]
    th.assert_eq("PRIVATE-KEY-MATERIAL" in (lost.message + (lost.remedy or "")),
                 False,
                 "the private key must never appear in a finding — findings "
                 "are rendered to a terminal and to a browser")


@th.django_unit_test("a generated key pair is stored in the same step it is created")
def test_identity_generated_key_pair_is_stored_atomically(opts):
    from mojo.deploy.provision import identity
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(config_bucket=names["config_bucket"],
                         secrets={"db_password": "kept"})

    ec2_client, ec2_stub = _stub("ec2")
    s3_client, s3_stub = _stub("s3")
    ec2_stub.add_response("create_key_pair", {
        "KeyName": names["key_pair"], "KeyPairId": "key-0aaa",
        "KeyFingerprint": "aa:bb", "KeyMaterial": "PRIVATE"}, {
        "KeyName": names["key_pair"], "KeyType": "ed25519",
        "TagSpecifications": spec_module.tag_specifications(
            spec, "identity", "key-pair", name=names["key_pair"])})
    s3_stub.add_response("put_object", {})

    with ec2_stub, s3_stub:
        findings, actions, result = identity.ensure_key_pair(
            _clients(ec2=ec2_client, s3=s3_client), spec, observed, apply=True)

    _assert_no_blind(findings, "both requests must be model-valid")
    ec2_stub.assert_no_pending_responses()
    s3_stub.assert_no_pending_responses()
    th.assert_eq(result["key_pair_created"], True, "the key pair was created")
    th.assert_eq(result["secrets"]["ssh_private_key"], "PRIVATE",
                 "the private key must be folded into the secrets the next "
                 "steps read")
    th.assert_eq(result["secrets"]["db_password"], "kept",
                 "writing the key must not drop the secrets already there")


@th.django_unit_test("an operator public key is imported, so no private material reaches AWS")
def test_identity_prefers_importing_a_supplied_public_key(opts):
    from mojo.deploy.provision import identity
    from mojo.deploy.provision import spec as spec_module

    spec = _spec(public_key="ssh-ed25519 AAAAC3Nz operator@example")
    names = spec_module.names(spec)
    client, stubber = _stub("ec2")
    stubber.add_response("import_key_pair", {
        "KeyName": names["key_pair"], "KeyPairId": "key-0aaa",
        "KeyFingerprint": "aa:bb"})
    with stubber:
        findings, actions, result = identity.ensure_key_pair(
            _clients(ec2=client), spec, _observed(), apply=True)

    _assert_no_blind(findings, "the import request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["key_pair_created"], True,
                 "the imported key must be reported as created")
    th.assert_eq([a.detail for a in actions], ["import"],
                 "importing is the preferred path — nothing that can only be "
                 "read once ever exists")


@th.django_unit_test("the node policy scopes the agent's log actions to this environment")
def test_identity_policy_scopes_logs_actions_to_the_project_log_group_prefix(opts):
    from mojo.deploy.provision import identity
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    document = identity.node_policy_document(spec)
    by_sid = {s.get("Sid"): s for s in document["Statement"]}

    th.assert_in("DjangoMojoEnvironmentSetup", by_sid,
                 f"the terraform policy statement must be carried across "
                 f"verbatim: {sorted(by_sid)}")
    th.assert_in("DjangoMojoAgentLogs", by_sid,
                 f"the CloudWatch agent's scoped write grant must be a "
                 f"separate statement, so a later hardening pass can narrow "
                 f"the setup one without taking logging with it: "
                 f"{sorted(by_sid)}")
    th.assert_in("DjangoMojoAmiParameter", by_sid,
                 f"the node runs the provisioning convergence with its "
                 f"instance role, so its exact public AMI parameter needs a "
                 f"separate least-privilege read grant: {sorted(by_sid)}")

    ami = by_sid["DjangoMojoAmiParameter"]
    architecture = spec_module.architecture_for(spec.node_type)
    parameter = spec_module.SSM_AMI_PARAMETERS[architecture]
    th.assert_eq(ami["Action"], ["ssm:GetParameter"],
                 "AMI resolution needs GetParameter only — no SSM wildcard "
                 "or write action belongs on the managed node role")
    th.assert_eq(
        ami["Resource"], f"arn:aws:ssm:{REGION}::parameter{parameter}",
        "the grant must name only the architecture-specific public AL2023 "
        "parameter consumed by this topology")

    logs = by_sid["DjangoMojoAgentLogs"]
    expected = (f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:"
                f"{names['log_group_prefix']}/*")
    th.assert_eq(logs["Resource"], expected,
                 "the log actions must be scoped to this environment's log "
                 "group prefix, not to every log group in the account")
    for action in identity.AGENT_LOG_ACTIONS:
        th.assert_in(action, logs["Action"],
                     f"the agent cannot ship logs without {action}")
    th.assert_in("cloudwatch:PutMetricData",
                 by_sid["DjangoMojoAgentMetrics"]["Action"],
                 "the agent publishes metrics as well as logs")

    for action in identity.SETUP_ACTIONS:
        th.assert_in(action, by_sid["DjangoMojoEnvironmentSetup"]["Action"],
                     f"{action} is in the terraform policy and must not be "
                     f"dropped — an account built by this tool and one built "
                     f"by tofu have to look the same to an auditor")


@th.django_unit_test("a matching inline policy is left alone rather than rewritten every run")
def test_identity_policy_comparison_ignores_serialization(opts):
    from mojo.deploy.provision import identity, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    document = identity.node_policy_document(spec)
    # IAM does not preserve statement or action order, and returns a
    # single-element Action as a bare string. Comparing serialized JSON would
    # report drift on every run and rewrite a policy that was already correct.
    shuffled = {"Version": document["Version"],
                "Statement": list(reversed(document["Statement"]))}
    for statement in shuffled["Statement"]:
        statement["Action"] = list(reversed(statement["Action"]))

    observed = _observed(
        node_role={"RoleName": names["node_role"],
                   "Arn": f"arn:aws:iam::{ACCOUNT}:role/{names['node_role']}"},
        node_role_policy=shuffled,
        instance_profile={"InstanceProfileName": names["instance_profile"],
                          "Arn": "arn:aws:iam::123456789012:instance-profile/x",
                          "Roles": [{"RoleName": names["node_role"]}]})

    client, stubber = _stub("iam")
    with stubber:
        findings, actions, result = identity.ensure_node_role(
            _clients(iam=client), spec, observed, apply=True)

    _assert_no_blind(findings, "a converged role must make no IAM calls")
    th.assert_eq(actions, [],
                 f"a policy that differs only in ordering is not drift: "
                 f"{[a.target for a in actions]}")
    th.assert_eq([f.status for f in findings if f.status != report.PASS], [],
                 f"everything must read as PASS: {_codes(findings)}")


# ── storage ─────────────────────────────────────────────────────────────────

@th.django_unit_test("a bucket outside us-east-1 carries a LocationConstraint")
def test_storage_sends_location_constraint_outside_us_east_1(opts):
    from mojo.deploy.provision import storage
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    stubber.add_response("create_bucket", {"Location": f"/{names['config_bucket']}"}, {
        "Bucket": names["config_bucket"],
        "CreateBucketConfiguration": {"LocationConstraint": REGION}})
    stubber.add_response("put_bucket_tagging", {})
    stubber.add_response("put_bucket_versioning", {})
    stubber.add_response("put_bucket_encryption", {})
    stubber.add_response("put_public_access_block", {})
    stubber.add_response("put_bucket_policy", {})

    with stubber:
        findings, actions, result = storage.ensure_config_bucket(
            _clients(s3=client), spec, _observed(), apply=True)

    _assert_no_blind(findings, "every S3 request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["config_bucket"], names["config_bucket"],
                 "the bucket name must be returned for the steps that write "
                 "into it")


@th.django_unit_test("a bucket in us-east-1 omits the LocationConstraint")
def test_storage_omits_location_constraint_in_us_east_1(opts):
    from mojo.deploy.provision import storage
    from mojo.deploy.provision import spec as spec_module

    spec = spec_module.build(PROJECT, ENV, "us-east-1", account_id=ACCOUNT)
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    # Sending LocationConstraint in us-east-1 fails with
    # InvalidLocationConstraint; expected_params here is the assertion.
    stubber.add_response("create_bucket", {}, {"Bucket": names["config_bucket"]})
    stubber.add_response("put_bucket_tagging", {})
    stubber.add_response("put_bucket_versioning", {})
    stubber.add_response("put_bucket_encryption", {})
    stubber.add_response("put_public_access_block", {})
    stubber.add_response("put_bucket_policy", {})

    with stubber:
        findings, actions, result = storage.ensure_config_bucket(
            _clients(s3=client), spec, _observed(), apply=True)

    _assert_no_blind(findings,
                     "us-east-1 must not receive a LocationConstraint")
    stubber.assert_no_pending_responses()


@th.django_unit_test("secrets are generated once and read back on every later run")
def test_storage_secrets_are_generated_once(opts):
    from mojo.deploy.provision import report, storage
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    stubber.add_response("put_object", {})
    with stubber:
        findings, actions, result = storage.ensure_secrets(
            _clients(s3=client), spec,
            _observed(config_bucket=names["config_bucket"]), apply=True)

    _assert_no_blind(findings, "the put_object request must be model-valid")
    stubber.assert_no_pending_responses()
    generated = result["secrets"]
    for key in ("db_password", "cache_auth_token", "django_secret_key"):
        th.assert_in(key, generated, f"{key} must be generated")
        th.assert_true(len(generated[key]) >= 32,
                       f"{key} is too short to be a credential")

    # A second run reads them back and writes nothing. This is what stops the
    # second apply from inventing a new database password for a cluster it
    # already created.
    client2, stubber2 = _stub("s3")
    with stubber2:
        findings2, actions2, result2 = storage.ensure_secrets(
            _clients(s3=client2), spec,
            _observed(config_bucket=names["config_bucket"], secrets=generated),
            apply=True)
    _assert_no_blind(findings2, "reading back must make no S3 call")
    th.assert_eq(actions2, [], "a second run must write nothing")
    th.assert_eq(result2["secrets"], generated,
                 "the same credentials must come back")
    th.assert_eq(_codes(findings2, report.PASS), ["secrets.ok"],
                 f"the secrets object must read as converged: "
                 f"{_codes(findings2)}")


@th.django_unit_test("no secret value ever reaches a finding or an action")
def test_storage_never_puts_a_secret_in_the_report(opts):
    from mojo.deploy.provision import storage
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    stubber.add_response("put_object", {})
    with stubber:
        findings, actions, result = storage.ensure_secrets(
            _clients(s3=client), spec,
            _observed(config_bucket=names["config_bucket"]), apply=True)

    rendered = " ".join(
        [f.message + " " + (f.remedy or "") for f in findings]
        + [f"{a.target} {a.detail or ''}" for a in actions])
    for value in result["secrets"].values():
        th.assert_eq(value in rendered, False,
                     "a generated credential appeared in the report — these "
                     "are printed to a terminal and rendered into a browser")


@th.django_unit_test("a bundle predating a credential gains it without losing the rest")
def test_storage_secrets_backfill_only_adds(opts):
    from mojo.deploy.provision import report, storage
    from mojo.deploy.provision import spec as spec_module

    # Exactly what an estate provisioned before github_webhook_secret existed
    # has in its bucket. It must come out of an upgrade still able to reach
    # its own database.
    old = {"db_password": "p" * 40, "cache_auth_token": "c" * 40,
           "django_secret_key": "k" * 50}
    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    stubber.add_response("put_object", {})
    with stubber:
        findings, actions, result = storage.ensure_secrets(
            _clients(s3=client), spec,
            _observed(config_bucket=names["config_bucket"], secrets=old),
            apply=True)

    _assert_no_blind(findings, "the backfill write must be model-valid")
    stubber.assert_no_pending_responses()
    merged = result["secrets"]
    for key, value in old.items():
        th.assert_eq(merged[key], value,
                     f"{key} was re-minted — an upgrade that invents a new "
                     f"database password takes the environment down")
    th.assert_true(len(merged.get("github_webhook_secret", "")) >= 32,
                   "the missing credential must be minted")
    th.assert_in("secrets.incomplete", _codes(findings, report.DRIFT),
                 f"the backfill must be reported: {_codes(findings)}")
    rendered = " ".join(f.message + " " + (f.remedy or "") for f in findings)
    th.assert_eq(merged["github_webhook_secret"] in rendered, False,
                 "a minted credential appeared in the report")


@th.django_unit_test("a dry-run backfill writes nothing and claims nothing")
def test_storage_secrets_backfill_is_not_previewed_into_existence(opts):
    from mojo.deploy.provision import storage
    from mojo.deploy.provision import spec as spec_module

    old = {"db_password": "p" * 40, "cache_auth_token": "c" * 40,
           "django_secret_key": "k" * 50}
    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    with stubber:
        findings, actions, result = storage.ensure_secrets(
            _clients(s3=client), spec,
            _observed(config_bucket=names["config_bucket"], secrets=old),
            apply=False)

    _assert_no_blind(findings, "a preview must make no S3 call")
    th.assert_eq(result["secrets"].get("github_webhook_secret"), None,
                 "a preview that hands back an unwritten secret renders a "
                 "django.conf no node can ever agree with")
    th.assert_eq(len(actions), 1, "the pending write must still be declared")


# ── data ────────────────────────────────────────────────────────────────────

@th.django_unit_test("a still-creating cluster is PENDING, not a failure and not a wait")
def test_data_reports_pending_for_a_still_creating_cluster(opts):
    from mojo.deploy.provision import data, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        db_subnet_group={"DBSubnetGroupName": names["db_subnet_group"]},
        db_cluster={"DBClusterIdentifier": names["db_cluster"],
                    "Status": "creating",
                    "DatabaseName": names["db_name"],
                    "EngineVersion": spec_module.ENGINE_VERSIONS[
                        spec_module.DB_ENGINE],
                    "DeletionProtection": True,
                    "BackupRetentionPeriod": spec.db_retention_days},
        db_instances=[{"DBInstanceIdentifier": names["db_writer"]},
                      {"DBInstanceIdentifier": names["db_readers"][0]}])

    client, stubber = _stub("rds")
    with stubber:
        findings, actions, result = data.ensure_database(
            _clients(rds=client), spec, observed, apply=True)

    th.assert_in("cluster.pending", _codes(findings, report.PENDING),
                 f"a creating cluster must be PENDING — there are no waiters "
                 f"here, and the next run picks it up: {_codes(findings)}")
    th.assert_eq(result["db_ready"], False, "a creating cluster is not ready")
    th.assert_eq("db_endpoint" in result, False,
                 "an endpoint must not be published before the cluster can "
                 "actually serve it — the steps downstream would connect to it")
    _assert_no_blind(findings, "no AWS call is needed to observe this")
    stubber.assert_no_pending_responses()


@th.django_unit_test("an immutable DBName is reported, never modified")
def test_data_reports_manual_for_an_immutable_dbname(opts):
    from mojo.deploy.provision import data, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        db_subnet_group={"DBSubnetGroupName": names["db_subnet_group"]},
        db_cluster={"DBClusterIdentifier": names["db_cluster"],
                    "Status": "available", "Endpoint": "writer.rds",
                    "ReaderEndpoint": "reader.rds",
                    "DatabaseName": "somethingelse",
                    "EngineVersion": spec_module.ENGINE_VERSIONS[
                        spec_module.DB_ENGINE],
                    "DeletionProtection": True,
                    "BackupRetentionPeriod": spec.db_retention_days},
        db_instances=[{"DBInstanceIdentifier": names["db_writer"]},
                      {"DBInstanceIdentifier": names["db_readers"][0]}])

    client, stubber = _stub("rds")
    with stubber:
        findings, actions, result = data.ensure_database(
            _clients(rds=client), spec, observed, apply=True)

    th.assert_in("cluster.db_name", _codes(findings, report.MANUAL),
                 f"an RDS DBName is fixed at creation and must be reported, "
                 f"not attempted: {_codes(findings)}")
    _assert_no_blind(findings, "no modify may be attempted")
    th.assert_eq([a for a in actions if a.verb == "modify"], [],
                 "no modify may even be planned against DBName")
    stubber.assert_no_pending_responses()


@th.django_unit_test("the cluster is created encrypted, protected, and tagged in one call")
def test_data_creates_the_cluster_tagged_at_creation(opts):
    from mojo.deploy.provision import data
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        db_subnet_group={"DBSubnetGroupName": names["db_subnet_group"]},
        private_subnet_ids=["subnet-0prv1", "subnet-0prv2"],
        rds_sg_id="sg-0rds",
        secrets={"db_password": "p" * 40})

    client, stubber = _stub("rds")
    stubber.add_response(
        "create_db_cluster",
        {"DBCluster": {"DBClusterIdentifier": names["db_cluster"],
                       "Status": "creating"}},
        {"DBClusterIdentifier": names["db_cluster"],
         "Engine": spec_module.DB_ENGINE,
         "EngineVersion": spec_module.ENGINE_VERSIONS[spec_module.DB_ENGINE],
         "DatabaseName": names["db_name"],
         "MasterUsername": data.MASTER_USERNAME,
         "MasterUserPassword": "p" * 40,
         "DBSubnetGroupName": names["db_subnet_group"],
         "VpcSecurityGroupIds": ["sg-0rds"],
         "Port": spec_module.DB_PORT,
         "StorageEncrypted": True,
         "DeletionProtection": True,
         "BackupRetentionPeriod": spec.db_retention_days,
         "Tags": spec_module.tag_list(spec, "database",
                                      name=names["db_cluster"])})
    stubber.add_response("create_db_instance", {"DBInstance": {}})
    stubber.add_response("create_db_instance", {"DBInstance": {}})

    with stubber:
        findings, actions, result = data.ensure_database(
            _clients(rds=client), spec, observed, apply=True)

    _assert_no_blind(findings, "every RDS request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["db_ready"], False,
                 "a freshly created cluster is never immediately available")


@th.django_unit_test("the cache is created with transit and at-rest encryption")
def test_data_creates_the_cache_encrypted(opts):
    from mojo.deploy.provision import data
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        cache_subnet_group={"CacheSubnetGroupName": names["cache_subnet_group"]},
        private_subnet_ids=["subnet-0prv1", "subnet-0prv2"],
        cache_sg_id="sg-0cache",
        secrets={"cache_auth_token": "t" * 40})

    client, stubber = _stub("elasticache")
    stubber.add_response(
        "create_replication_group",
        {"ReplicationGroup": {"ReplicationGroupId": names["cache_group"],
                              "Status": "creating"}},
        {"ReplicationGroupId": names["cache_group"],
         "ReplicationGroupDescription": "django-mojo valkey",
         "Engine": spec_module.CACHE_ENGINE,
         "EngineVersion": spec_module.ENGINE_VERSIONS[spec_module.CACHE_ENGINE],
         "CacheNodeType": spec.cache_type,
         "NumCacheClusters": 1 + spec.cache_replicas,
         "AutomaticFailoverEnabled": True,
         "MultiAZEnabled": True,
         "CacheSubnetGroupName": names["cache_subnet_group"],
         "SecurityGroupIds": ["sg-0cache"],
         "Port": spec_module.CACHE_PORT,
         "TransitEncryptionEnabled": True,
         "AtRestEncryptionEnabled": True,
         "AuthToken": "t" * 40,
         "Tags": spec_module.tag_list(spec, "cache",
                                      name=names["cache_group"])})

    with stubber:
        findings, actions, result = data.ensure_cache(
            _clients(elasticache=client), spec, observed, apply=True)

    _assert_no_blind(findings, "the ElastiCache request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["cache_ready"], False,
                 "a freshly created replication group is still coming up")


# ── stale subnet groups ─────────────────────────────────────────────────────
#
# The shape a partial teardown leaves behind: the subnet group survived (nothing
# here deletes one), the subnets it names did not, and the network step has just
# built replacements under new ids. AWS accepts the group right up until the
# cluster create, then answers "Current AZ coverage: " with nothing after the
# colon — fifteen minutes in, naming neither the group nor the staleness.
#
# In every test below the create is proved NOT to have been attempted twice
# over: `_assert_no_blind` (an unstubbed call arrives as a BLIND finding, not an
# exception) and `assert_no_pending_responses`.

def _dead_subnets():
    return [{"SubnetIdentifier": "subnet-0dead1",
             "SubnetAvailabilityZone": {"Name": ZONES[0]}},
            {"SubnetIdentifier": "subnet-0dead2",
             "SubnetAvailabilityZone": {"Name": ZONES[1]}}]


def _live_subnets():
    return [{"SubnetId": "subnet-0prv1", "AvailabilityZone": ZONES[0]},
            {"SubnetId": "subnet-0prv2", "AvailabilityZone": ZONES[1]}]


def _named_subnets():
    return [{"SubnetIdentifier": "subnet-0prv1",
             "SubnetAvailabilityZone": {"Name": ZONES[0]}},
            {"SubnetIdentifier": "subnet-0prv2",
             "SubnetAvailabilityZone": {"Name": ZONES[1]}}]


def _stale(findings):
    return [f for f in findings if f.code == "subnet_group.stale"]


@th.django_unit_test("a DB subnet group whose subnets are gone is MANUAL, and stops the create")
def test_data_reports_manual_for_a_stale_db_subnet_group(opts):
    from mojo.deploy.provision import data, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        db_subnet_group={"DBSubnetGroupName": names["db_subnet_group"],
                         "Subnets": _dead_subnets()},
        subnets=[],
        private_subnet_ids=["subnet-0new1", "subnet-0new2"],
        rds_sg_id="sg-0rds",
        secrets={"db_password": "p" * 40})

    client, stubber = _stub("rds")
    with stubber:
        findings, actions, result = data.ensure_database(
            _clients(rds=client), spec, observed, apply=True)

    th.assert_in("subnet_group.stale", _codes(findings, report.MANUAL),
                 f"a subnet group naming only deleted subnets must be MANUAL — "
                 f"its membership cannot be patched back and this tool never "
                 f"deletes: {_codes(findings)}")
    stale = _stale(findings)
    th.assert_true(names["db_subnet_group"] in stale[0].message,
                   f"the finding must name the subnet group, which the AWS "
                   f"error never does: {stale[0].message!r}")
    th.assert_true("delete-db-subnet-group" in (stale[0].remedy or ""),
                   f"the remedy must give the exact delete command, since a "
                   f"human is the only thing that can run it: "
                   f"{stale[0].remedy!r}")
    th.assert_eq("subnet_group.ok" in _codes(findings), False,
                 "a stale group must never also be reported as in place")
    th.assert_eq([a.target for a in actions if a.target == names["db_cluster"]],
                 [],
                 "a cluster create must not even be PLANNED against a subnet "
                 "group AWS is going to reject")
    _assert_no_blind(findings,
                     "create_db_cluster must not be attempted — MANUAL alone "
                     "does not block a step, so the create has to be made "
                     "conditional on the validation itself")
    stubber.assert_no_pending_responses()


@th.django_unit_test("a DB subnet group down to one zone is MANUAL — AWS requires two")
def test_data_reports_manual_when_the_db_subnet_group_lost_a_zone(opts):
    from mojo.deploy.provision import data, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    # One of the two survived. AWS counts zones, not subnets, and one is not two.
    observed = _observed(
        db_subnet_group={"DBSubnetGroupName": names["db_subnet_group"],
                         "Subnets": [_named_subnets()[0],
                                     _dead_subnets()[1]]},
        subnets=[_live_subnets()[0]],
        private_subnet_ids=["subnet-0prv1", "subnet-0new2"],
        rds_sg_id="sg-0rds",
        secrets={"db_password": "p" * 40})

    client, stubber = _stub("rds")
    with stubber:
        findings, actions, result = data.ensure_database(
            _clients(rds=client), spec, observed, apply=True)

    th.assert_in("subnet_group.stale", _codes(findings, report.MANUAL),
                 f"one surviving subnet in one zone does not meet the two-AZ "
                 f"coverage AWS enforces at create time: {_codes(findings)}")
    _assert_no_blind(findings, "no cluster create may be attempted")
    stubber.assert_no_pending_responses()


@th.django_unit_test("a stale cache subnet group is MANUAL, and stops the replication group")
def test_data_reports_manual_for_a_stale_cache_subnet_group(opts):
    from mojo.deploy.provision import data, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        cache_subnet_group={
            "CacheSubnetGroupName": names["cache_subnet_group"],
            "Subnets": _dead_subnets()},
        subnets=[],
        private_subnet_ids=["subnet-0new1", "subnet-0new2"],
        cache_sg_id="sg-0cache",
        secrets={"cache_auth_token": "t" * 40})

    client, stubber = _stub("elasticache")
    with stubber:
        findings, actions, result = data.ensure_cache(
            _clients(elasticache=client), spec, observed, apply=True)

    th.assert_in("subnet_group.stale", _codes(findings, report.MANUAL),
                 f"the cache subnet group has the same failure mode as the DB "
                 f"one and must be reported the same way: {_codes(findings)}")
    stale = _stale(findings)
    th.assert_true(names["cache_subnet_group"] in stale[0].message,
                   f"the finding must name the cache subnet group: "
                   f"{stale[0].message!r}")
    th.assert_true("delete-cache-subnet-group" in (stale[0].remedy or ""),
                   f"the remedy must give the elasticache delete command: "
                   f"{stale[0].remedy!r}")
    th.assert_eq([a.target for a in actions if a.target == names["cache_group"]],
                 [],
                 "a replication group create must not be planned against it")
    th.assert_eq(result["cache_ready"], False,
                 "nothing was created, so the cache is certainly not ready")
    _assert_no_blind(findings,
                     "create_replication_group must not be attempted")
    stubber.assert_no_pending_responses()


@th.django_unit_test("a healthy DB subnet group spanning two zones changes nothing")
def test_data_accepts_a_db_subnet_group_that_still_spans_two_zones(opts):
    from mojo.deploy.provision import data, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        db_subnet_group={"DBSubnetGroupName": names["db_subnet_group"],
                         "Subnets": _named_subnets()},
        subnets=_live_subnets(),
        private_subnet_ids=["subnet-0prv1", "subnet-0prv2"],
        rds_sg_id="sg-0rds",
        secrets={"db_password": "p" * 40})

    client, stubber = _stub("rds")
    stubber.add_response(
        "create_db_cluster",
        {"DBCluster": {"DBClusterIdentifier": names["db_cluster"],
                       "Status": "creating"}},
        {"DBClusterIdentifier": names["db_cluster"],
         "Engine": spec_module.DB_ENGINE,
         "EngineVersion": spec_module.ENGINE_VERSIONS[spec_module.DB_ENGINE],
         "DatabaseName": names["db_name"],
         "MasterUsername": data.MASTER_USERNAME,
         "MasterUserPassword": "p" * 40,
         "DBSubnetGroupName": names["db_subnet_group"],
         "VpcSecurityGroupIds": ["sg-0rds"],
         "Port": spec_module.DB_PORT,
         "StorageEncrypted": True,
         "DeletionProtection": True,
         "BackupRetentionPeriod": spec.db_retention_days,
         "Tags": spec_module.tag_list(spec, "database",
                                      name=names["db_cluster"])})
    stubber.add_response("create_db_instance", {"DBInstance": {}})
    stubber.add_response("create_db_instance", {"DBInstance": {}})

    with stubber:
        findings, actions, result = data.ensure_database(
            _clients(rds=client), spec, observed, apply=True)

    th.assert_eq(_stale(findings), [],
                 f"a group whose subnets all still exist, across two zones, is "
                 f"exactly what this package builds — accusing it would send an "
                 f"operator to delete a working group: {_codes(findings)}")
    th.assert_in("subnet_group.ok", _codes(findings, report.PASS),
                 f"a healthy group must still report as in place: "
                 f"{_codes(findings)}")
    stubber.assert_no_pending_responses()
    _assert_no_blind(findings,
                     "the cluster create must go ahead exactly as before")


@th.django_unit_test("a healthy cache subnet group spanning two zones changes nothing")
def test_data_accepts_a_cache_subnet_group_that_still_spans_two_zones(opts):
    from mojo.deploy.provision import data, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        cache_subnet_group={
            "CacheSubnetGroupName": names["cache_subnet_group"],
            "Subnets": _named_subnets()},
        subnets=_live_subnets(),
        private_subnet_ids=["subnet-0prv1", "subnet-0prv2"],
        cache_sg_id="sg-0cache",
        secrets={"cache_auth_token": "t" * 40})

    client, stubber = _stub("elasticache")
    stubber.add_response(
        "create_replication_group",
        {"ReplicationGroup": {"ReplicationGroupId": names["cache_group"],
                              "Status": "creating"}},
        {"ReplicationGroupId": names["cache_group"],
         "ReplicationGroupDescription": "django-mojo valkey",
         "Engine": spec_module.CACHE_ENGINE,
         "EngineVersion": spec_module.ENGINE_VERSIONS[spec_module.CACHE_ENGINE],
         "CacheNodeType": spec.cache_type,
         "NumCacheClusters": 1 + spec.cache_replicas,
         "AutomaticFailoverEnabled": True,
         "MultiAZEnabled": True,
         "CacheSubnetGroupName": names["cache_subnet_group"],
         "SecurityGroupIds": ["sg-0cache"],
         "Port": spec_module.CACHE_PORT,
         "TransitEncryptionEnabled": True,
         "AtRestEncryptionEnabled": True,
         "AuthToken": "t" * 40,
         "Tags": spec_module.tag_list(spec, "cache",
                                      name=names["cache_group"])})

    with stubber:
        findings, actions, result = data.ensure_cache(
            _clients(elasticache=client), spec, observed, apply=True)

    th.assert_eq(_stale(findings), [],
                 f"a cache subnet group whose subnets still exist must be left "
                 f"alone: {_codes(findings)}")
    th.assert_in("subnet_group.ok", _codes(findings, report.PASS),
                 f"a healthy cache subnet group must still report as in place: "
                 f"{_codes(findings)}")
    stubber.assert_no_pending_responses()
    _assert_no_blind(findings,
                     "the replication group create must go ahead as before")


# ── nodes ───────────────────────────────────────────────────────────────────

@th.django_unit_test("an elastic IP is adopted only when tagged for this project and env")
def test_nodes_adopts_an_eip_only_when_tagged_for_this_project_and_env(opts):
    from mojo.deploy.provision import nodes, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec(preset="micro")
    names = spec_module.names(spec)
    hostname = names["nodes"][0]

    stranger = {"AllocationId": "eipalloc-stranger",
                "PublicIp": "198.51.100.9",
                "Tags": [{"Key": "Name", "Value": hostname}]}
    observed = _observed(
        instances=[{"InstanceId": "i-0aaa", "State": {"Name": "running"},
                    "InstanceType": spec.node_type, "ImageId": "ami-0base",
                    "Tags": [{"Key": "Name", "Value": hostname}]}],
        addresses=[stranger], ami_id="ami-0base")

    client, stubber = _stub("ec2")
    stubber.add_response("allocate_address", {"AllocationId": "eipalloc-mine",
                                              "PublicIp": "203.0.113.10"})
    stubber.add_response("associate_address", {"AssociationId": "eipassoc-1"})
    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, observed, apply=True)

    _assert_no_blind(findings, "every EC2 request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_in("address.missing", _codes(findings, report.MISSING),
                 f"an address that carries our Name tag but none of our "
                 f"mojo:* tags may well be someone else's reservation and must "
                 f"never be adopted: {_codes(findings)}")
    th.assert_eq(result["node_addresses"], ["203.0.113.10"],
                 "the newly allocated address must be the one returned")


@th.django_unit_test("a correctly tagged elastic IP is adopted rather than duplicated")
def test_nodes_adopts_a_correctly_tagged_eip(opts):
    from mojo.deploy.provision import nodes, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec(preset="micro")
    names = spec_module.names(spec)
    hostname = names["nodes"][0]
    mine = {"AllocationId": "eipalloc-mine", "PublicIp": "203.0.113.10",
            "InstanceId": "i-0aaa",
            "Tags": spec_module.tag_list(spec, "node", name=hostname)}
    observed = _observed(
        instances=[{"InstanceId": "i-0aaa", "State": {"Name": "running"},
                    "InstanceType": spec.node_type, "ImageId": "ami-0base",
                    "Tags": [{"Key": "Name", "Value": hostname}]}],
        addresses=[mine], ami_id="ami-0base")

    client, stubber = _stub("ec2")
    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, observed, apply=True)

    _assert_no_blind(findings,
                     "a converged single-node environment must make no calls")
    th.assert_eq(actions, [], "nothing may be created on a second apply")
    th.assert_in("address.ok", _codes(findings, report.PASS),
                 f"the tagged address must be adopted: {_codes(findings)}")
    stubber.assert_no_pending_responses()


@th.django_unit_test("the resolved base image is reported, and an override wins")
def test_nodes_report_the_resolved_image(opts):
    from mojo.deploy.provision import nodes, report

    spec = _spec(preset="micro")
    client, stubber = _stub("ec2")
    with stubber:
        findings, _, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, _observed(ami_id="ami-0fromssm"),
            apply=False)
    th.assert_eq(result["ami_id"], "ami-0fromssm",
                 "the image resolved from the SSM parameter must be recorded, "
                 "so an operator can answer what a fleet was built from")
    resolved = [f for f in findings if f.code == "ami.resolved"]
    th.assert_true(resolved and "ami-0fromssm" in resolved[0].message,
                   f"the report must name the image id: {_codes(findings)}")

    override = _spec(preset="micro", ami_override="ami-0pinned")
    client2, stubber2 = _stub("ec2")
    with stubber2:
        _, _, result2 = nodes.ensure_nodes(
            _clients(ec2=client2), override, _observed(ami_id="ami-0fromssm"),
            apply=False)
    th.assert_eq(result2["ami_id"], "ami-0pinned",
                 "spec.ami_override must win over the SSM lookup")


@th.django_unit_test("a launched node forces IMDSv2, encrypts its root volume, and tags both")
def test_nodes_launch_request_shape(opts):
    from mojo.deploy.provision import nodes
    from mojo.deploy.provision import spec as spec_module

    spec = _spec(preset="micro")
    names = spec_module.names(spec)
    observed = _observed(
        ami_id="ami-0base", public_subnet_ids=["subnet-0pub1"],
        node_sg_id="sg-0node",
        instance_profile_name=names["instance_profile"],
        key_pair_name=names["key_pair"],
        # A node is not launched until the stage-1 payload it boots from is
        # published — see provision_nodes_userdata.py for the refusal.
        bootstrap_payload={"bucket": names["config_bucket"],
                           "version": "0.0.0-test"})

    client, stubber = _stub("ec2")
    stubber.add_response(
        "run_instances", {"Instances": [{"InstanceId": "i-0new"}]}, {
            "ImageId": "ami-0base",
            "InstanceType": spec.node_type,
            "MinCount": 1, "MaxCount": 1,
            "KeyName": names["key_pair"],
            "SubnetId": "subnet-0pub1",
            "SecurityGroupIds": ["sg-0node"],
            "IamInstanceProfile": {"Name": names["instance_profile"]},
            "MetadataOptions": {"HttpEndpoint": "enabled",
                                "HttpTokens": "required"},
            "BlockDeviceMappings": [{
                "DeviceName": nodes.ROOT_DEVICE,
                "Ebs": {"VolumeSize": spec.node_volume_gb,
                        "VolumeType": "gp3", "Encrypted": True,
                        "DeleteOnTermination": True}}],
            "UserData": nodes.stage0_user_data(spec, names["nodes"][0]),
            "TagSpecifications": (
                spec_module.tag_specifications(
                    spec, "node", "instance", name=names["nodes"][0])
                + spec_module.tag_specifications(
                    spec, "node", "volume", name=names["nodes"][0]))})
    stubber.add_response("allocate_address", {"AllocationId": "eipalloc-1",
                                              "PublicIp": "203.0.113.10"})
    stubber.add_response("associate_address", {"AssociationId": "eipassoc-1"})

    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, observed, apply=True)

    _assert_no_blind(findings, "the launch request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["instance_ids"], ["i-0new"],
                 "the launched instance id must be returned for the balancer "
                 "to register")


# ── balancer ────────────────────────────────────────────────────────────────

@th.django_unit_test("target groups forward over TCP and health check over a separate protocol")
def test_balancer_target_group_uses_tcp_with_a_separate_health_check_protocol(opts):
    from mojo.deploy.provision import balancer

    spec = _spec()
    groups = balancer.target_group_specs(spec, VPC_ID)

    th.assert_eq(groups["api"]["Protocol"], "TCP",
                 "an NLB target group forwards over TCP even for HTTPS "
                 "traffic; Protocol='HTTPS' is rejected by CreateTargetGroup")
    th.assert_eq(groups["api"]["Port"], 443, "the api group serves 443")
    th.assert_eq(groups["api"]["HealthCheckProtocol"], "HTTPS",
                 "health is measured over HTTPS — a separate field from the "
                 "forwarding protocol")
    th.assert_eq(groups["api"]["HealthCheckPath"], balancer.HEALTH_PATH,
                 "the health check path is wrong")

    th.assert_eq(groups["certbot"]["Protocol"], "TCP",
                 "the certbot group forwards over TCP too")
    th.assert_eq(groups["certbot"]["Port"], 80, "the certbot group serves 80")
    th.assert_eq(groups["certbot"]["HealthCheckProtocol"], "HTTP",
                 "the :80 group is health checked over HTTP")
    th.assert_eq(groups["certbot"]["Matcher"], {"HttpCode": "200-399"},
                 "a node that redirects :80 to :443 is up, and up is what the "
                 "check is for")


def _custom_health_topology():
    from mojo.deploy.provision import brownfield_inputs
    from .brownfield_fixture import raw_manifest

    raw = raw_manifest()
    raw["load_balancer"]["api_health_path"] = "/api/maestro/node/ready"
    raw["load_balancer"]["certbot_health_path"] = "/certbot/ready"
    return brownfield_inputs.to_spec(brownfield_inputs.validate(raw))


@th.django_unit_test("brownfield target-group creates carry the declared health paths")
def test_balancer_creates_brownfield_groups_with_declared_health_paths(opts):
    from mojo.deploy.provision import balancer
    from mojo.deploy.provision import spec as spec_module

    spec = _custom_health_topology()
    wanted = balancer.target_group_specs(spec, VPC_ID)
    th.assert_eq(wanted["api"]["HealthCheckPath"],
                 "/api/maestro/node/ready",
                 "the API group must use application-owned readiness")
    th.assert_eq(wanted["certbot"]["HealthCheckPath"], "/certbot/ready",
                 "the certbot group must use its independently declared path")

    client, stubber = _stub("elbv2")
    for role in ("api", "certbot"):
        request = wanted[role]
        stubber.add_response(
            "create_target_group",
            {"TargetGroups": [{"TargetGroupArn": f"arn:tg:{role}"}]},
            dict(request, Tags=spec_module.tag_list(
                spec, "balancer", name=request["Name"])))
    findings, actions = [], []
    with stubber:
        arns = balancer._ensure_target_groups(
            client, spec, {}, wanted, findings, actions, apply=True)

    _assert_no_blind(findings,
                     "declared health paths must be valid ELBv2 create fields")
    stubber.assert_no_pending_responses()
    th.assert_eq(arns, {"api": "arn:tg:api", "certbot": "arn:tg:certbot"},
                 "both created target-group ARNs must remain available to listeners")
    details = {action.target: action.detail for action in actions}
    th.assert_in("/api/maestro/node/ready",
                 details[wanted["api"]["Name"]],
                 "the reviewed preview action must bind the API health path")
    th.assert_in("/certbot/ready", details[wanted["certbot"]["Name"]],
                 "the reviewed preview action must bind the certbot health path")


@th.django_unit_test("owned brownfield target groups converge health-path drift in place")
def test_balancer_modifies_owned_brownfield_health_path_drift(opts):
    from mojo.deploy.provision import balancer, report

    spec = _custom_health_topology()
    wanted = balancer.target_group_specs(spec, VPC_ID)
    existing = {}
    client, stubber = _stub("elbv2")
    for role in ("api", "certbot"):
        existing[role] = dict(
            wanted[role], TargetGroupArn=f"arn:tg:{role}",
            HealthCheckPath=balancer.HEALTH_PATH)
        stubber.add_response("modify_target_group", {}, {
            "TargetGroupArn": f"arn:tg:{role}",
            "HealthCheckPath": wanted[role]["HealthCheckPath"],
        })

    findings, actions = [], []
    with stubber:
        arns = balancer._ensure_target_groups(
            client, spec, {"target_groups": existing}, wanted,
            findings, actions, apply=True)

    _assert_no_blind(findings,
                     "health-path drift must produce valid ModifyTargetGroup calls")
    stubber.assert_no_pending_responses()
    th.assert_eq(arns, {"api": "arn:tg:api", "certbot": "arn:tg:certbot"},
                 "in-place health convergence must preserve target-group identity")
    th.assert_eq(
        sorted(_codes(findings, report.DRIFT)),
        ["target_group.api.health_check", "target_group.certbot.health_check"],
        "both owned groups must report their health-path drift")
    th.assert_eq([action.verb for action in actions], ["modify", "modify"],
                 "mutable path drift must plan modifications, never replacements")
    details = {action.target: action.detail for action in actions}
    th.assert_eq(
        details[wanted["api"]["Name"]],
        '{"HealthCheckPath":"/api/maestro/node/ready"}',
        "the API modify preview must bind the exact desired path as JSON")
    th.assert_eq(
        details[wanted["certbot"]["Name"]],
        '{"HealthCheckPath":"/certbot/ready"}',
        "the certbot modify preview must bind the exact desired path as JSON")


@th.django_unit_test("a target group that AWS made immutable is reported, never modified")
def test_balancer_reports_manual_for_an_immutable_target_group_field(opts):
    from mojo.deploy.provision import balancer, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        vpc_id=VPC_ID, public_subnet_ids=["subnet-0pub1", "subnet-0pub2"],
        instance_ids=["i-0aaa", "i-0bbb"],
        balancer={"LoadBalancerArn": "arn:lb", "DNSName": "nlb.example",
                  "CanonicalHostedZoneId": "Z0LB",
                  "State": {"Code": "active"}},
        listeners=[{"Port": 443}, {"Port": 80}],
        target_groups={
            "api": {"TargetGroupArn": "arn:tg-api",
                    "TargetGroupName": names["api_target_group"],
                    # Someone built this by hand as a TLS group.
                    "Protocol": "TLS", "Port": 443, "TargetType": "instance",
                    "VpcId": VPC_ID, "HealthCheckProtocol": "HTTPS",
                    "HealthCheckPort": "traffic-port",
                    "HealthCheckPath": balancer.HEALTH_PATH,
                    "HealthCheckIntervalSeconds": 30,
                    "HealthyThresholdCount": 3, "UnhealthyThresholdCount": 3},
            "certbot": {"TargetGroupArn": "arn:tg-certbot",
                        "TargetGroupName": names["certbot_target_group"],
                        "Protocol": "TCP", "Port": 80,
                        "TargetType": "instance", "VpcId": VPC_ID,
                        "HealthCheckProtocol": "HTTP",
                        "HealthCheckPort": "traffic-port",
                        "HealthCheckPath": balancer.HEALTH_PATH,
                        "HealthCheckIntervalSeconds": 30,
                        "HealthyThresholdCount": 3,
                        "UnhealthyThresholdCount": 3,
                        "Matcher": {"HttpCode": "200-399"}},
        },
        targets={"api": [{"Target": {"Id": "i-0aaa"}},
                         {"Target": {"Id": "i-0bbb"}}],
                 "certbot": [{"Target": {"Id": "i-0aaa"}}]})

    elbv2, elbv2_stub = _stub("elbv2")
    ec2, ec2_stub = _stub("ec2")
    with elbv2_stub, ec2_stub:
        findings, actions, result = balancer.ensure_balancer(
            _clients(elbv2=elbv2, ec2=ec2), spec, observed, apply=False)

    th.assert_in("target_group.api.immutable", _codes(findings, report.MANUAL),
                 f"a target group's Protocol is fixed at creation; there is no "
                 f"ModifyTargetGroup that accepts it: {_codes(findings)}")
    th.assert_eq([a for a in actions if a.target == names["api_target_group"]],
                 [],
                 "no modify may be planned against an immutable field")
    _assert_no_blind(findings, "a dry run must make no calls")


@th.django_unit_test("the balancer step is not run at all on a preset that has none")
def test_balancer_does_nothing_on_the_micro_preset(opts):
    from mojo.deploy.provision import balancer, report

    spec = _spec(preset="micro")
    elbv2, stubber = _stub("elbv2")
    with stubber:
        findings, actions, result = balancer.ensure_balancer(
            _clients(elbv2=elbv2), spec, _observed(), apply=True)

    th.assert_eq(actions, [], "micro must plan no balancer work")
    th.assert_eq(_codes(findings, report.PASS), ["balancer.not_wanted"],
                 f"the report must say why, rather than staying silent: "
                 f"{_codes(findings)}")
    _assert_no_blind(findings, "no ELBv2 call may be made")
    stubber.assert_no_pending_responses()


@th.django_unit_test("a re-run registers only the targets that are missing")
def test_balancer_registers_only_missing_targets(opts):
    from mojo.deploy.provision import balancer, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    groups = balancer.target_group_specs(spec, VPC_ID)
    observed = _observed(
        vpc_id=VPC_ID, public_subnet_ids=["subnet-0pub1", "subnet-0pub2"],
        instance_ids=["i-0aaa", "i-0bbb"],
        balancer={"LoadBalancerArn": "arn:lb", "DNSName": "nlb.example",
                  "CanonicalHostedZoneId": "Z0LB",
                  "State": {"Code": "active"}},
        listeners=[{"Port": 443}, {"Port": 80}],
        target_groups={
            "api": dict(groups["api"], TargetGroupArn="arn:tg-api",
                        TargetGroupName=names["api_target_group"]),
            "certbot": dict(groups["certbot"], TargetGroupArn="arn:tg-certbot",
                            TargetGroupName=names["certbot_target_group"]),
        },
        targets={"api": [{"Target": {"Id": "i-0aaa"}}],
                 "certbot": [{"Target": {"Id": "i-0aaa"}}]})

    elbv2, elbv2_stub = _stub("elbv2")
    ec2, ec2_stub = _stub("ec2")
    elbv2_stub.add_response("modify_load_balancer_attributes", {"Attributes": []})
    elbv2_stub.add_response("register_targets", {}, {
        "TargetGroupArn": "arn:tg-api",
        "Targets": [{"Id": "i-0bbb", "Port": 443}]})

    with elbv2_stub, ec2_stub:
        findings, actions, result = balancer.ensure_balancer(
            _clients(elbv2=elbv2, ec2=ec2), spec, observed, apply=True)

    _assert_no_blind(findings, "every ELBv2 request must be model-valid")
    elbv2_stub.assert_no_pending_responses()
    th.assert_in("targets.certbot.ok", _codes(findings, report.PASS),
                 f"the certbot group already holds node 0 and must not be "
                 f"touched: {_codes(findings)}")
    th.assert_eq(result["balancer_dns"], "nlb.example",
                 "the balancer DNS name must be returned for the DNS step")


# ── observability ───────────────────────────────────────────────────────────

@th.django_unit_test("the three log groups are created at 90-day retention")
def test_observability_creates_the_three_log_groups_with_90_day_retention(opts):
    from mojo.deploy.provision import observability
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    logs, logs_stub = _stub("logs")
    for group_name in sorted(names["log_groups"].values()):
        logs_stub.add_response("create_log_group", {}, {
            "logGroupName": group_name,
            "tags": spec_module.TAGS(spec, "observability")})
        logs_stub.add_response("put_retention_policy", {}, {
            "logGroupName": group_name,
            "retentionInDays": spec_module.LOG_RETENTION_DAYS})

    observed = _observed(
        trails=[{"Name": "org-trail", "IsMultiRegionTrail": True,
                 "LogFileValidationEnabled": True}],
        detector_ids=["d-0aaa"])
    cloudtrail, ct_stub = _stub("cloudtrail")
    guardduty, gd_stub = _stub("guardduty")
    s3, s3_stub = _stub("s3")

    with logs_stub, ct_stub, gd_stub, s3_stub:
        findings, actions, result = observability.ensure_observability(
            _clients(logs=logs, cloudtrail=cloudtrail, guardduty=guardduty,
                     s3=s3), spec, observed, apply=True)

    _assert_no_blind(findings, "every CloudWatch Logs request must be valid")
    logs_stub.assert_no_pending_responses()
    th.assert_eq(result["log_group_names"],
                 sorted(names["log_groups"].values()),
                 "all three log groups must be reported, and their names come "
                 "from spec.names() so the node agent config cannot drift "
                 "from them")
    th.assert_eq(spec_module.LOG_RETENTION_DAYS, 90,
                 "the retention the gate asked for is 90 days")


@th.django_unit_test("a log group at a different retention is drift, and is never replaced")
def test_observability_reports_drift_on_retention_and_never_replaces(opts):
    from mojo.deploy.provision import observability, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    present = {name: {"logGroupName": name, "retentionInDays": 7}
               for name in names["log_groups"].values()}
    observed = _observed(
        log_groups=present,
        trails=[{"Name": "org-trail", "IsMultiRegionTrail": True,
                 "LogFileValidationEnabled": True}],
        detector_ids=["d-0aaa"])

    logs, logs_stub = _stub("logs")
    for group_name in sorted(names["log_groups"].values()):
        logs_stub.add_response("put_retention_policy", {}, {
            "logGroupName": group_name,
            "retentionInDays": spec_module.LOG_RETENTION_DAYS})
    cloudtrail, ct_stub = _stub("cloudtrail")
    guardduty, gd_stub = _stub("guardduty")
    s3, s3_stub = _stub("s3")

    with logs_stub, ct_stub, gd_stub, s3_stub:
        findings, actions, result = observability.ensure_observability(
            _clients(logs=logs, cloudtrail=cloudtrail, guardduty=guardduty,
                     s3=s3), spec, observed, apply=True)

    _assert_no_blind(findings,
                     "only put_retention_policy may be called — a create or "
                     "any other call would appear here as BLIND")
    logs_stub.assert_no_pending_responses()
    drift = [f for f in findings if f.status == report.DRIFT
             and f.code.startswith("log_group.")]
    th.assert_eq(len(drift), 3,
                 f"all three groups differ and must be reported: "
                 f"{_codes(findings)}")
    th.assert_true(all("7" in f.message for f in drift),
                   f"the finding must name the retention actually in place: "
                   f"{[f.message for f in drift]}")


@th.django_unit_test("an existing multi-region trail is adopted, not duplicated")
def test_observability_adopts_an_existing_multi_region_trail(opts):
    from mojo.deploy.provision import observability, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        log_groups={name: {"logGroupName": name, "retentionInDays": 90}
                    for name in names["log_groups"].values()},
        trails=[{"Name": "company-wide", "IsMultiRegionTrail": True,
                 "LogFileValidationEnabled": True}],
        detector_ids=["d-0aaa"])

    logs, logs_stub = _stub("logs")
    cloudtrail, ct_stub = _stub("cloudtrail")
    guardduty, gd_stub = _stub("guardduty")
    s3, s3_stub = _stub("s3")

    with logs_stub, ct_stub, gd_stub, s3_stub:
        findings, actions, result = observability.ensure_observability(
            _clients(logs=logs, cloudtrail=cloudtrail, guardduty=guardduty,
                     s3=s3), spec, observed, apply=True)

    _assert_no_blind(findings,
                     "no trail, bucket or detector call may be made when both "
                     "already exist — a second multi-region trail records the "
                     "same events twice and bills for both")
    th.assert_eq(actions, [],
                 f"nothing may be created: {[a.target for a in actions]}")
    th.assert_eq(result["trail_name"], "company-wide",
                 "the existing trail must be the one reported")
    th.assert_in("guardduty.ok", _codes(findings, report.PASS),
                 f"an existing detector must be adopted — AWS allows one per "
                 f"region per account: {_codes(findings)}")


@th.django_unit_test("a denied call becomes a BLIND finding, not a traceback")
def test_access_denied_becomes_a_blind_finding_not_a_traceback(opts):
    from mojo.deploy.provision import observability, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(
        trails=[{"Name": "org-trail", "IsMultiRegionTrail": True,
                 "LogFileValidationEnabled": True}],
        detector_ids=["d-0aaa"])

    logs, logs_stub = _stub("logs")
    for _ in sorted(names["log_groups"].values()):
        logs_stub.add_client_error(
            "create_log_group", service_error_code="AccessDeniedException",
            http_status_code=403)
    cloudtrail, ct_stub = _stub("cloudtrail")
    guardduty, gd_stub = _stub("guardduty")
    s3, s3_stub = _stub("s3")

    with logs_stub, ct_stub, gd_stub, s3_stub:
        findings, actions, result = observability.ensure_observability(
            _clients(logs=logs, cloudtrail=cloudtrail, guardduty=guardduty,
                     s3=s3), spec, observed, apply=True)

    blind = [f for f in findings if f.status == report.BLIND]
    th.assert_eq(len(blind), 3,
                 f"each denied create must produce a BLIND finding rather "
                 f"than killing the run: {_codes(findings)}")
    th.assert_true(all(f.code.endswith(".denied") for f in blind),
                   f"a denial must be distinguishable from any other error: "
                   f"{[f.code for f in blind]}")
    th.assert_true(all(f.remedy for f in blind),
                   "a BLIND finding must say what permission to grant")


# ── the package-wide rules ──────────────────────────────────────────────────

def _destructive_calls(source, label):
    """Destructive calls in this source, found by parsing it — not by grepping.

    A substring search for "delete" flags `DeletionProtection=True`, which is
    the OPPOSITE of a destructive call, and flags every comment and docstring
    that names a forbidden method in order to document the rule. Both are
    exactly the lines this package is full of. Parsing looks at calls, so prose
    and settings are invisible to it.

    What parsing still cannot see is `getattr(client, "delete_" + kind)()`. That
    is what the runtime guard on the `Clients` proxy is for, and it is tested
    directly below. A `getattr` with a literal destructive name IS caught here.
    """
    import ast

    from mojo.deploy.provision.discover import DESTRUCTIVE_PREFIXES

    hits = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            if node.func.id == "getattr" and len(node.args) >= 2:
                literal = node.args[1]
                if isinstance(literal, ast.Constant) and isinstance(
                        literal.value, str):
                    name = literal.value
            else:
                name = node.func.id
        if name and name.startswith(DESTRUCTIVE_PREFIXES):
            hits.append(f"{label}:{node.lineno}: {name}")
    return hits


@th.django_unit_test("no module in the provision package calls a destructive verb")
def test_no_delete_verbs_anywhere_in_the_provision_package(opts):
    from mojo.deploy import provision

    root = os.path.dirname(os.path.abspath(provision.__file__))
    offenders = []
    scanned = 0
    for entry in sorted(os.listdir(root)):
        if not entry.endswith(".py"):
            continue
        scanned += 1
        with open(os.path.join(root, entry)) as handle:
            offenders.extend(_destructive_calls(handle.read(), entry))

    th.assert_true(scanned >= 13,
                   f"the scan must actually cover the package — only "
                   f"{scanned} module(s) were read")
    th.assert_eq(offenders, [],
                 "mojo/deploy/provision converges accounts and never tears "
                 "them down; removing a resource is a deliberate human act "
                 "performed elsewhere")


@th.django_unit_test("the destructive-call scan is a call scan, not a substring grep")
def test_the_destructive_call_scan_is_not_a_substring_grep(opts):
    innocent = "\n".join([
        'request = {"DeletionProtection": True, "SkipFinalSnapshot": False}',
        '# this package must never call client.delete_bucket(Bucket=name)',
        'def helper():',
        '    """Never reach for ec2.terminate_instances(InstanceIds=ids)."""',
        '    return request',
    ])
    th.assert_eq(_destructive_calls(innocent, "innocent.py"), [],
                 "a setting that PREVENTS deletion, and prose documenting the "
                 "rule, must not be reported — those are most of the lines in "
                 "this package that contain the word")

    th.assert_true(_destructive_calls("client.delete_bucket(Bucket=b)\n", "x.py"),
                   "an actual destructive call must be caught")
    th.assert_true(_destructive_calls("ec2.terminate_instances(Ids=x)\n", "x.py"),
                   "terminate must be caught too")
    th.assert_true(
        _destructive_calls('getattr(c, "delete_vpc")(VpcId=v)\n', "x.py"),
        "a getattr with a literal destructive name must be caught as well")


@th.django_unit_test("the client proxy refuses a destructive method even through getattr")
def test_clients_proxy_raises_on_a_denylisted_method_even_via_getattr(opts):
    from mojo.deploy.provision import discover

    client, _ = _stub("ec2")
    guarded = discover.Clients(ec2=client).get("ec2")

    for verb in ("delete_vpc", "terminate_instances", "deregister_targets",
                 "revoke_security_group_ingress", "remove_role_from_instance_profile"):
        raised = False
        try:
            getattr(guarded, verb)
        except discover.DestructiveCallBlocked:
            raised = True
        th.assert_true(raised,
                       f"{verb} must be refused at the seam — a source scan "
                       f"cannot see a method reached dynamically")

    th.assert_true(callable(getattr(guarded, "describe_vpcs")),
                   "a read must still work; the guard is not a wall around the "
                   "whole client")
    th.assert_true(callable(getattr(guarded, "create_vpc")),
                   "a create must still work")


@th.django_unit_test("importing the provision package pulls in no AWS SDK and no logit")
def test_the_package_costs_nothing_to_import(opts):
    import subprocess
    import sys

    import mojo

    root = os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONPATH"] = root
    done = subprocess.run(
        [sys.executable, "-c",
         "import sys; import mojo.deploy.provision.plan as p; "
         "print('boto3' if 'boto3' in sys.modules else 'lazy', "
         "'logit' if 'mojo.helpers.logit' in sys.modules else 'clean')"],
        env=env, capture_output=True, text=True, timeout=120)

    th.assert_eq(done.returncode, 0,
                 f"the package must import with no settings configured: "
                 f"{done.stderr}")
    th.assert_eq(done.stdout.strip(), "lazy clean",
                 f"boto3 must be imported lazily inside functions and "
                 f"mojo.helpers.logit must never be reachable from here: "
                 f"{done.stdout!r} {done.stderr!r}")


@th.unit_test("a NoSuchKey on an optional S3 read is absence, not blindness")
def test_no_such_key_is_absent_not_blind(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import discover

    def read():
        raise ClientError(
            {"Error": {"Code": "NoSuchKey",
                       "Message": "The specified key does not exist."}},
            "GetObject")

    findings = []
    got = discover.optional(findings, "discover", "s3.get_object", read, None)
    th.assert_eq(got, None,
                 "an absent optional object must read back as its default")
    th.assert_eq(len(findings), 0,
                 "an object that legitimately does not exist yet must not "
                 "produce a BLIND finding — that was the live NoSuchKey bug "
                 "that painted every fresh-account observe as unreadable")


@th.unit_test("observe maps a security group by its exact name when the role tag is wrong")
def test_observe_maps_security_group_by_name_when_role_tag_is_wrong(opts):
    from mojo.deploy.provision import discover
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)

    def sg(name, role):
        return {"GroupId": "sg-" + name[-8:], "GroupName": name,
                "VpcId": "vpc-test", "IpPermissions": [],
                "Tags": [{"Key": "mojo:project", "Value": spec.project},
                         {"Key": "mojo:env", "Value": spec.env},
                         {"Key": "mojo:role", "Value": role},
                         {"Key": "managed-by", "Value": "django-mojo"}]}

    # Every group carries the OLD step-level role tag ("network") — the shape
    # the first shipped release actually wrote. Discovery must still map all
    # three by their exact contracted names, or apply re-creates them forever
    # (the live InvalidGroup.Duplicate loop this regression pins).
    groups = [sg(names["node_sg"], "network"),
              sg(names["rds_sg"], "network"),
              sg(names["cache_sg"], "network")]

    client, stubber = _stub("ec2")
    import mojo.deploy.provision.discover as dmod
    observed = {}
    findings = []
    # Drive just the mapping logic through the public observe path by stubbing
    # every network describe; only describe_security_groups returns rows.
    from unittest import mock
    with mock.patch.object(dmod, "optional") as opt:
        def fake_optional(f, step, name, func, default=None):
            if name == "ec2.describe_vpcs":
                return [{"VpcId": "vpc-test", "CidrBlock": "10.0.0.0/16",
                         "Tags": []}]
            if name == "ec2.describe_security_groups":
                return groups
            return default
        opt.side_effect = fake_optional
        obs = dmod.objict()
        dmod._observe_network(_clients(ec2=client), spec, obs, findings)

    got = obs.get("security_groups") or {}
    for role in ("node", "rds", "cache"):
        th.assert_true(role in got,
                       f"the {role} group must be found by name even though "
                       f"its mojo:role tag says 'network'")
        th.assert_eq(got[role]["GroupName"],
                     {"node": names["node_sg"], "rds": names["rds_sg"],
                      "cache": names["cache_sg"]}[role],
                     f"the {role} slot must hold the group with the "
                     f"contracted name")


# ── nodes: an EIP association against a still-pending instance ──────────────

def _one_node_with_a_reserved_eip(spec):
    """A micro environment whose node exists and whose tagged EIP is unattached.

    That is the shape a real first `apply` leaves behind: `run_instances`
    returned, the address was allocated and tagged, and the association is the
    very next call — against an instance EC2 still reports as `pending`.
    """
    from mojo.deploy.provision import spec as spec_module

    hostname = spec_module.names(spec)["nodes"][0]
    return _observed(
        instances=[{"InstanceId": "i-0new", "State": {"Name": "pending"},
                    "InstanceType": spec.node_type, "ImageId": "ami-0base",
                    "Tags": [{"Key": "Name", "Value": hostname}]}],
        addresses=[{"AllocationId": "eipalloc-mine",
                    "PublicIp": "203.0.113.10",
                    "Tags": spec_module.tag_list(spec, "node", name=hostname)}],
        ami_id="ami-0base")


@th.django_unit_test("associating an EIP with a still-pending instance is PENDING, not BLIND")
def test_nodes_report_pending_when_the_instance_is_not_running_yet(opts):
    from mojo.deploy.provision import nodes, report

    spec = _spec(preset="micro")
    observed = _one_node_with_a_reserved_eip(spec)

    client, stubber = _stub("ec2")
    # The live error, verbatim: EC2 models no error shapes, so the code string
    # is the only thing there is to classify on.
    stubber.add_client_error(
        "associate_address", service_error_code="InvalidInstanceID",
        service_message="The pending instance 'i-0new' is not in a valid "
                        "state for this operation",
        http_status_code=400)

    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, observed, apply=True)

    stubber.assert_no_pending_responses()
    _assert_no_blind(findings,
                     "an instance that is simply not running yet must never be "
                     "BLIND — BLIND fails the step and BLOCKS dns, telling an "
                     "operator their bootstrap broke when the correct advice is "
                     "'run it again in a minute'")
    th.assert_in("address.instance_not_ready", _codes(findings, report.PENDING),
                 f"the not-yet-running instance must be reported as PENDING: "
                 f"{[(f.code, f.status) for f in findings]}")
    th.assert_eq(report.Report(findings).is_blocking(), False,
                 "a PENDING address must not block the steps that follow — the "
                 "next apply associates it")
    waiting = [f for f in findings
               if f.code == "address.instance_not_ready"][0]
    th.assert_true(waiting.remedy and "re-run" in waiting.remedy,
                   f"the remedy must tell the operator to re-run apply, which "
                   f"is the entire action available to them: {waiting.remedy!r}")


@th.django_unit_test("any other associate_address error is still BLIND")
def test_nodes_still_report_blind_for_a_real_associate_address_failure(opts):
    from mojo.deploy.provision import nodes, report

    spec = _spec(preset="micro")
    observed = _one_node_with_a_reserved_eip(spec)

    client, stubber = _stub("ec2")
    stubber.add_client_error(
        "associate_address", service_error_code="UnauthorizedOperation",
        service_message="You are not authorized to perform this operation",
        http_status_code=403)

    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, observed, apply=True)

    stubber.assert_no_pending_responses()
    th.assert_in("ec2.associate_address.denied", _codes(findings, report.BLIND),
                 f"a denial is not 'not ready yet' — it must keep failing the "
                 f"step through report.safe: "
                 f"{[(f.code, f.status) for f in findings]}")
    th.assert_eq(report.Report(findings).is_blocking(), True,
                 "a credential that cannot associate addresses must block the "
                 "steps downstream of this one")


@th.django_unit_test("a not-ready instance still leaves the address usable to dns")
def test_nodes_return_the_address_even_when_association_is_pending(opts):
    from mojo.deploy.provision import nodes

    spec = _spec(preset="micro")
    observed = _one_node_with_a_reserved_eip(spec)

    client, stubber = _stub("ec2")
    stubber.add_client_error(
        "associate_address", service_error_code="IncorrectInstanceState",
        service_message="The instance 'i-0new' is not in a valid state",
        http_status_code=400)

    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, observed, apply=True)

    th.assert_eq(result["node_addresses"], ["203.0.113.10"],
                 "the address is allocated and reserved for this node whether "
                 "or not the instance was ready to take it — dns points at it "
                 "either way, and the next apply attaches it")


# ── the operator's copy of the generated SSH key ────────────────────────────

PRIVATE_KEY_BODY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
                    "b3BlbnNzaC1rZXktdjEAAAAA-not-a-real-key\n"
                    "-----END OPENSSH PRIVATE KEY-----")


class _Console:
    """Records what the CLI would print, so a test can assert what it did NOT."""

    def __init__(self):
        self.lines = []

    def say(self, text=""):
        self.lines.append(text)

    def text(self):
        return "\n".join(self.lines)


@th.unit_test("the generated private key is written once, at 0600, and re-used after")
def test_storage_materializes_the_generated_key_at_0600(opts):
    import os
    import stat
    import tempfile

    from mojo.deploy.provision import storage

    spec = _spec()
    with tempfile.TemporaryDirectory() as home:
        path, wrote = storage.materialize_ssh_identity(
            spec, {"ssh_private_key": PRIVATE_KEY_BODY}, home=home)

        th.assert_eq(path, os.path.join(home, ".ssh",
                                        f"{PROJECT}-{ENV}.pem"),
                     "the key must land at one stable path per project and "
                     "environment, so a second configure finds the first "
                     "one's file instead of writing another copy")
        th.assert_eq(wrote, True, "the first call must actually write the file")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        th.assert_eq(oct(mode), oct(0o600),
                     f"ssh refuses an identity file any other account can read "
                     f"— 0600 is what makes this usable at all, not hygiene: "
                     f"got {oct(mode)}")
        with open(path, "r", encoding="utf-8") as handle:
            th.assert_eq(handle.read(), PRIVATE_KEY_BODY + "\n",
                         "the key must be written verbatim, with the trailing "
                         "newline ssh expects")

        # Second call, identical material: nothing rewritten.
        path2, wrote2 = storage.materialize_ssh_identity(
            spec, {"ssh_private_key": PRIVATE_KEY_BODY}, home=home)
        th.assert_eq(path2, path, "the same path must come back")
        th.assert_eq(wrote2, False,
                     "an identical key must not be rewritten on every run")

        # A file left at a permissive mode by an older run is corrected.
        os.chmod(path, 0o644)
        storage.materialize_ssh_identity(
            spec, {"ssh_private_key": PRIVATE_KEY_BODY}, home=home)
        th.assert_eq(oct(stat.S_IMODE(os.stat(path).st_mode)), oct(0o600),
                     "a key file found at a mode ssh would ignore must be "
                     "corrected, not left for the operator to discover")


@th.unit_test("a rotated key replaces the file rather than being ignored")
def test_storage_rewrites_the_key_when_the_material_differs(opts):
    import tempfile

    from mojo.deploy.provision import storage

    spec = _spec()
    with tempfile.TemporaryDirectory() as home:
        storage.materialize_ssh_identity(
            spec, {"ssh_private_key": "old-material"}, home=home)
        path, wrote = storage.materialize_ssh_identity(
            spec, {"ssh_private_key": PRIVATE_KEY_BODY}, home=home)

        th.assert_eq(wrote, True,
                     "a key that differs from what is on disk must be written "
                     "— otherwise a re-created key pair leaves configure "
                     "authenticating with the one it replaced")
        with open(path, "r", encoding="utf-8") as handle:
            th.assert_eq(handle.read(), PRIVATE_KEY_BODY + "\n",
                         "the new material must be what is on disk")


@th.unit_test("an imported key pair stores no private half, and nothing is written")
def test_storage_writes_nothing_when_there_is_no_generated_key(opts):
    import os
    import tempfile

    from mojo.deploy.provision import storage

    spec = _spec()
    with tempfile.TemporaryDirectory() as home:
        path, wrote = storage.materialize_ssh_identity(
            spec, {"db_password": "x"}, home=home)

        th.assert_eq(path, None,
                     "an ImportKeyPair environment has no private half in the "
                     "bucket — the operator holds it, and saying so is the "
                     "correct outcome, not writing an empty file")
        th.assert_eq(wrote, False, "nothing may be written")
        th.assert_eq(os.path.exists(os.path.join(home, ".ssh")), False,
                     "not even the directory may be created when there is no "
                     "key to put in it")


@th.django_unit_test("stable_node_ips keeps per-node EIPs even behind a balancer")
def test_nodes_stable_ips_behind_balancer(opts):
    from mojo.deploy.provision import nodes, report
    from mojo.deploy.provision import spec as spec_module

    spec = _spec(preset="micro", want_balancer=True, stable_node_ips=True)
    th.assert_true(spec_module.wants_balancer(spec),
                   "this test exists for the balancer path; the spec lost it")
    names = spec_module.names(spec)
    hostname = names["nodes"][0]
    observed = _observed(
        instances=[{"InstanceId": "i-0aaa", "State": {"Name": "running"},
                    "InstanceType": spec.node_type, "ImageId": "ami-0base",
                    "Tags": [{"Key": "Name", "Value": hostname}]}],
        ami_id="ami-0base")

    client, stubber = _stub("ec2")
    stubber.add_response("allocate_address", {"AllocationId": "eipalloc-mine",
                                              "PublicIp": "203.0.113.10"})
    stubber.add_response("associate_address", {"AssociationId": "eipassoc-1"})
    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), spec, observed, apply=True)

    _assert_no_blind(findings, "every EC2 request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["node_addresses"], ["203.0.113.10"],
                 "a balancer fleet with stable_node_ips must still hold "
                 "per-node addresses — that is the whole point of the flag")

    # Without the flag, the balancer path stays address-free as before.
    plain = _spec(preset="micro", want_balancer=True)
    client, stubber = _stub("ec2")
    with stubber:
        findings, actions, result = nodes.ensure_nodes(
            _clients(ec2=client), plain, observed, apply=True)
    stubber.assert_no_pending_responses()
    th.assert_eq(result["node_addresses"], [],
                 "a balancer fleet without stable_node_ips must not grow "
                 "node addresses")
    address_codes = [f.code for f in findings if f.code.startswith("address.")]
    th.assert_eq(address_codes, [],
                 f"the balancer path reported address work it must not do: "
                 f"{address_codes}")
