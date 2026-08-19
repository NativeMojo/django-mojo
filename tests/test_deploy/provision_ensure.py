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
