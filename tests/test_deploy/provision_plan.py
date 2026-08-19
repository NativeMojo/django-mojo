"""The DAG: its edges, how failure and slowness propagate, and the no-op.

Two kinds of test live here. The graph tests drive `plan._walk` with a small
synthetic step list, because propagation is a property of the walk and proving
it against fourteen real steps would only make the failure harder to read. The
whole-account tests drive the real graph against a hand-built converged
observation, because "a second apply creates nothing" is a claim about the
actual fourteen steps and cannot be shown any other way.
"""

from unittest import mock

from testit import helpers as th


REGION = "us-west-2"
PROJECT = "wmx"
ENV = "prod"
ACCOUNT = "123456789012"
VPC_ID = "vpc-0aaa1111"
ZONES = ("us-west-2a", "us-west-2b")


def _spec(**overrides):
    from mojo.deploy.provision import spec as spec_module
    preset = overrides.pop("preset", "small")
    overrides.setdefault("account_id", ACCOUNT)
    return spec_module.build(PROJECT, ENV, REGION, preset=preset, **overrides)


def _stub(service):
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name=REGION,
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def _clients(**overrides):
    from mojo.deploy.provision import discover
    return discover.Clients(session=None, **overrides)


def _fixed(status_findings=(), actions=()):
    """A step function that reports whatever the test tells it to."""
    from mojo.deploy.provision import report

    def run(clients, spec, observed, apply=False):
        return list(status_findings), list(actions), report.Result()
    return run


# ── the graph itself ────────────────────────────────────────────────────────

@th.django_unit_test("every declared dependency exists and the graph has no cycle")
def test_the_dag_is_a_valid_graph(opts):
    from mojo.deploy.provision import plan

    ordered = plan._ordered()
    th.assert_eq(len(ordered), len(plan.STEPS),
                 "the topological sort must place every step exactly once")

    placed = set()
    for step in ordered:
        for dependency in step.depends_on:
            th.assert_in(dependency, placed,
                         f"{step.name} runs before its dependency "
                         f"{dependency} — that is a real ordering bug, not a "
                         f"cosmetic one: the step would run against values "
                         f"nothing had resolved yet")
        placed.add(step.name)


@th.django_unit_test("the edges that matter are actually declared")
def test_the_dag_declares_the_load_bearing_edges(opts):
    from mojo.deploy.provision import plan

    edges = {step.name: set(step.depends_on) for step in plan.STEPS}

    th.assert_in("secrets", edges["key_pair"],
                 "a generated private key has to land in the secrets object in "
                 "the same step it is created, so the object must exist first")
    th.assert_in("stage1_payload", edges["nodes"],
                 "a node boots by reading the stage-1 payload; launching "
                 "before it is written gives a box with nothing to configure "
                 "itself from")
    th.assert_in("db", edges["stage1_payload"],
                 "the payload carries the database endpoint")
    th.assert_in("cache", edges["stage1_payload"],
                 "the payload carries the cache endpoint")
    th.assert_in("network", edges["db"],
                 "Aurora needs the private subnets")
    th.assert_in("network", edges["cache"],
                 "Valkey needs the private subnets")
    th.assert_eq(edges["dns"], {"nodes", "balancer"},
                 "dns points at whichever of the two produced an address, so "
                 "it must declare both — and a disabled balancer must not "
                 "block it")
    th.assert_eq(edges["account"], set(),
                 "the account check depends on nothing; it is what everything "
                 "else depends on")


@th.django_unit_test("a cycle or an unknown dependency is refused, not run in file order")
def test_a_broken_graph_is_refused(opts):
    from mojo.deploy.provision import plan

    cyclic = (plan.Step("a", _fixed(), ("b",)),
              plan.Step("b", _fixed(), ("a",)))
    raised = ""
    try:
        plan._ordered(cyclic)
    except ValueError as err:
        raised = str(err)
    th.assert_in("cycle", raised,
                 "a cycle must raise and name the steps involved — degrading "
                 "to 'ran in whatever order the tuple happened to be in' is "
                 "what makes a DAG worth nothing")

    unknown = (plan.Step("a", _fixed(), ("nope",)),)
    raised = ""
    try:
        plan._ordered(unknown)
    except ValueError as err:
        raised = str(err)
    th.assert_in("nope", raised,
                 "a dependency on a step that does not exist must raise and "
                 "name it")


# ── propagation ─────────────────────────────────────────────────────────────

def _graph(first_findings):
    from mojo.deploy.provision import plan

    return (
        plan.Step("first", _fixed(first_findings)),
        plan.Step("second", _fixed(), ("first",)),
        plan.Step("third", _fixed(), ("second",)),
        plan.Step("unrelated", _fixed()),
    )


@th.django_unit_test("a failed dependency blocks its dependents, transitively, with no traceback")
def test_a_failed_dependency_blocks_its_dependents(opts):
    from mojo.deploy.provision import plan, report

    failure = [report.Finding("first", report.BLIND, "first.denied",
                              "the credential cannot do this", "grant it")]
    with mock.patch.object(plan.discover, "observe",
                           return_value=([], plan.discover.blank())):
        findings, actions, run = plan.apply(
            _clients(), _spec(), steps=_graph(failure))

    th.assert_eq(run.steps["first"].status, plan.FAILED,
                 "a BLIND finding means the call was not made, so the step "
                 "failed — reporting it as a warning would let every step "
                 "downstream run against values nothing resolved")
    th.assert_eq(run.steps["second"].status, plan.BLOCKED,
                 "the direct dependent must be blocked")
    th.assert_eq(run.steps["third"].status, plan.BLOCKED,
                 "blocking must be transitive — this is the whole reason the "
                 "steps declare edges instead of sitting in a list")
    th.assert_eq(run.steps["second"].blocked_by, ["first"],
                 "the report must name what blocked it")
    th.assert_eq(run.steps["unrelated"].status, plan.OK,
                 "a step on another branch of the graph must still run")
    th.assert_true(run.blocking,
                   "the run as a whole must report itself as blocking")


@th.django_unit_test("a pending dependency skips its dependents rather than blocking them")
def test_a_pending_dependency_skips_its_dependents(opts):
    from mojo.deploy.provision import plan, report

    waiting = [report.pending("first", "first.creating",
                              "AWS is still building this")]
    with mock.patch.object(plan.discover, "observe",
                           return_value=([], plan.discover.blank())):
        findings, actions, run = plan.apply(
            _clients(), _spec(), steps=_graph(waiting))

    th.assert_eq(run.steps["first"].status, plan.PENDING,
                 "a still-creating resource is PENDING, not a failure")
    th.assert_eq(run.steps["second"].status, plan.SKIPPED,
                 "a dependent of a PENDING step is SKIPPED — the next apply, "
                 "ten minutes later, picks it up; BLOCKED would tell an "
                 "operator their bootstrap broke when the right advice is "
                 "'run it again'")
    th.assert_eq(run.steps["third"].status, plan.SKIPPED,
                 "skipping is transitive too")
    th.assert_eq(run.blocking, False,
                 "waiting on AWS must not make the run report as failed")
    th.assert_in("second.waiting", [f.code for f in findings],
                 f"the report must say what is being waited on: "
                 f"{[f.code for f in findings]}")


@th.django_unit_test("a disabled step blocks nothing")
def test_a_disabled_step_does_not_block_its_dependents(opts):
    from mojo.deploy.provision import plan

    steps = (
        plan.Step("first", _fixed()),
        plan.Step("optional", _fixed(), ("first",), enabled=lambda spec: False),
        plan.Step("after", _fixed(), ("optional",)),
    )
    with mock.patch.object(plan.discover, "observe",
                           return_value=([], plan.discover.blank())):
        findings, actions, run = plan.apply(_clients(), _spec(), steps=steps)

    th.assert_eq(run.steps["optional"].status, plan.DISABLED,
                 "a step the topology does not want is DISABLED")
    th.assert_eq(run.steps["after"].status, plan.OK,
                 "a dependent of a DISABLED step must still run — this is what "
                 "lets dns point at the node's own address when the micro "
                 "preset builds no balancer")


@th.django_unit_test("the balancer step is disabled entirely on the micro preset")
def test_balancer_step_is_skipped_entirely_on_the_micro_preset(opts):
    from mojo.deploy.provision import plan

    with mock.patch.object(plan.discover, "observe",
                           return_value=([], plan.discover.blank())):
        _, _, micro = plan.observe(_clients(), _spec(preset="micro"))
        _, _, small = plan.observe(_clients(), _spec(preset="small"))

    th.assert_eq(micro.steps["balancer"].status, plan.DISABLED,
                 "one node has nothing to balance across")
    th.assert_true(small.steps["balancer"].status != plan.DISABLED,
                   "two nodes must get a balancer, or the second is "
                   "unreachable")
    th.assert_true(micro.steps["nodes"].status != plan.DISABLED,
                   "the micro preset still builds a node")


# ── validation runs first ───────────────────────────────────────────────────

@th.django_unit_test("name validation runs before the first AWS call, and stops the run")
def test_validate_names_runs_before_any_aws_call(opts):
    from mojo.deploy.provision import plan, report
    from mojo.deploy.provision import spec as spec_module

    # A project slug this long produces a target group name AWS rejects. Found
    # after the VPC, the subnets and an encrypted Aurora cluster exist, that is
    # a bill and a manual cleanup — nothing in this package deletes.
    spec = spec_module.build("verylongprojectname", "staging", REGION)
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return [], plan.discover.blank()

    ec2, ec2_stub = _stub("ec2")
    with mock.patch.object(plan.discover, "observe", side_effect=record):
        with ec2_stub:
            findings, actions, run = plan.apply(_clients(ec2=ec2), spec)

    th.assert_eq(calls, [],
                 "the account must not even be READ when the spec cannot be "
                 "named — validation is step 0, before discovery")
    th.assert_eq(actions, [], "nothing may be planned or created")
    th.assert_eq(run.validated, False, "the run must record that it stopped")
    th.assert_true(run.problems,
                   "the problems must be returned so the CLI can print them")
    th.assert_true(all(f.status == report.MANUAL for f in findings),
                   f"a naming problem is something only a human can fix: "
                   f"{[(f.code, f.status) for f in findings]}")
    th.assert_eq(run.steps["network"].status, plan.BLOCKED,
                 "every step must report as blocked, not silently absent")
    ec2_stub.assert_no_pending_responses()


# ── the whole graph against a converged account ─────────────────────────────

def _converged(spec):
    """An account that already matches the spec, in the shape `observe` returns.

    Long, and deliberately so: "a second apply creates nothing" is only worth
    anything if the fixture covers every step, and a shortcut here would be a
    shortcut in exactly the assertion the design rests on.
    """
    from mojo.deploy.provision import balancer, discover, identity, storage
    from mojo.deploy.provision import spec as spec_module

    names = spec_module.names(spec)
    secrets = {"db_password": "p" * 40, "cache_auth_token": "t" * 40,
               "django_secret_key": "s" * 64,
               "ssh_private_key": "PRIVATE", "ssh_key_name": names["key_pair"]}
    groups = balancer.target_group_specs(spec, VPC_ID)

    observed = discover.blank()
    observed.account_id = ACCOUNT
    observed.caller_arn = f"arn:aws:iam::{ACCOUNT}:user/bootstrap"
    observed.region = REGION
    observed.azs = [{"ZoneName": zone} for zone in ZONES]
    observed.offered_zone_names = list(ZONES)

    observed.vpc = {"VpcId": VPC_ID, "CidrBlock": spec_module.VPC_CIDR}
    observed.subnets = []
    for index in range(spec_module.AZ_COUNT):
        observed.subnets.append({
            "SubnetId": f"subnet-0pub{index + 1}",
            "AvailabilityZone": ZONES[index],
            "CidrBlock": spec_module.PUBLIC_SUBNET_CIDRS[index],
            "Tags": [{"Key": "Name",
                      "Value": names["public_subnets"][index]}]})
        observed.subnets.append({
            "SubnetId": f"subnet-0prv{index + 1}",
            "AvailabilityZone": ZONES[index],
            "CidrBlock": spec_module.PRIVATE_SUBNET_CIDRS[index],
            "Tags": [{"Key": "Name",
                      "Value": names["private_subnets"][index]}]})
    observed.internet_gateway = {"InternetGatewayId": "igw-0aaa"}
    observed.route_tables = [
        {"RouteTableId": "rtb-0pub",
         "Routes": [{"DestinationCidrBlock": "0.0.0.0/0"}],
         "Associations": [{"SubnetId": "subnet-0pub1"},
                          {"SubnetId": "subnet-0pub2"}],
         "Tags": [{"Key": "Name", "Value": names["public_route_table"]}]},
        {"RouteTableId": "rtb-0prv", "Routes": [],
         "Associations": [{"SubnetId": "subnet-0prv1"},
                          {"SubnetId": "subnet-0prv2"}],
         "Tags": [{"Key": "Name", "Value": names["private_route_table"]}]},
    ]
    observed.vpc_endpoints = [{"VpcEndpointId": "vpce-0aaa",
                               "ServiceName": f"com.amazonaws.{REGION}.s3"}]
    observed.security_groups = {
        "node": {"GroupId": "sg-0node", "GroupName": names["node_sg"],
                 "IpPermissions": [
                     {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                      "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                     {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                      "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
        "rds": {"GroupId": "sg-0rds", "GroupName": names["rds_sg"],
                "IpPermissions": [
                    {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                     "UserIdGroupPairs": [{"GroupId": "sg-0node"}]}]},
        "cache": {"GroupId": "sg-0cache", "GroupName": names["cache_sg"],
                  "IpPermissions": [
                      {"IpProtocol": "tcp", "FromPort": 6379, "ToPort": 6379,
                       "UserIdGroupPairs": [{"GroupId": "sg-0node"}]}]},
    }

    observed.key_pair = {"KeyName": names["key_pair"], "KeyPairId": "key-0aaa"}
    observed.node_role = {
        "RoleName": names["node_role"],
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/{names['node_role']}"}
    observed.node_role_policy = identity.node_policy_document(spec)
    observed.instance_profile = {
        "InstanceProfileName": names["instance_profile"],
        "Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/"
               f"{names['instance_profile']}",
        "Roles": [{"RoleName": names["node_role"]}]}

    observed.config_bucket = names["config_bucket"]
    observed.config_bucket_state = {
        "versioning": "Enabled",
        "encryption": {"ServerSideEncryptionConfiguration": {"Rules": []}},
        "public_access_block": {"PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True}},
        "policy": storage.secure_transport_policy(names["config_bucket"]),
    }
    observed.secrets = secrets

    observed.db_subnet_group = {"DBSubnetGroupName": names["db_subnet_group"]}
    observed.db_cluster = {
        "DBClusterIdentifier": names["db_cluster"], "Status": "available",
        "Endpoint": "writer.rds.internal", "ReaderEndpoint": "reader.rds.internal",
        "DatabaseName": names["db_name"], "StorageEncrypted": True,
        "EngineVersion": spec_module.ENGINE_VERSIONS[spec_module.DB_ENGINE],
        "DeletionProtection": True,
        "BackupRetentionPeriod": spec.db_retention_days}
    observed.db_instances = [{"DBInstanceIdentifier": names["db_writer"]}] + [
        {"DBInstanceIdentifier": value} for value in names["db_readers"]]
    observed.cache_subnet_group = {
        "CacheSubnetGroupName": names["cache_subnet_group"]}
    observed.cache_group = {
        "ReplicationGroupId": names["cache_group"], "Status": "available",
        "TransitEncryptionEnabled": True, "AtRestEncryptionEnabled": True,
        "NodeGroups": [{"PrimaryEndpoint": {"Address": "cache.internal"}}]}

    # The stage-1 payload is built from the endpoints the db and cache steps
    # resolve, so it has to be computed the same way here or the converged
    # fixture would report drift against itself.
    resolved = dict(observed)
    resolved.update({"db_endpoint": "writer.rds.internal",
                     "db_reader_endpoint": "reader.rds.internal",
                     "cache_endpoint": "cache.internal"})
    observed.stage1 = storage.stage1_document(spec, resolved,
                                              names["secrets_object"])

    observed.ami_id = "ami-0base"
    observed.instances = [
        {"InstanceId": f"i-0node{index + 1}", "State": {"Name": "running"},
         "InstanceType": spec.node_type, "ImageId": "ami-0base",
         "Tags": [{"Key": "Name", "Value": hostname}]}
        for index, hostname in enumerate(names["nodes"])]
    observed.addresses = []

    observed.balancer = {
        "LoadBalancerArn": "arn:aws:elasticloadbalancing:::loadbalancer/net/x",
        "DNSName": "nlb.example.com", "CanonicalHostedZoneId": "Z0LB",
        "State": {"Code": "active"}}
    observed.balancer_attributes = {
        "load_balancing.cross_zone.enabled": "true",
        "deletion_protection.enabled": "true"}
    observed.listeners = [{"Port": 443}, {"Port": 80}]
    observed.target_groups = {
        "api": dict(groups["api"], TargetGroupArn="arn:tg-api",
                    TargetGroupName=names["api_target_group"]),
        "certbot": dict(groups["certbot"], TargetGroupArn="arn:tg-certbot",
                        TargetGroupName=names["certbot_target_group"]),
    }
    node_ids = [f"i-0node{index + 1}" for index in range(spec.node_count)]
    observed.targets = {
        "api": [{"Target": {"Id": value}} for value in node_ids],
        "certbot": [{"Target": {"Id": node_ids[0]}}]}

    observed.cloudtrail_bucket = names["cloudtrail_bucket"]
    observed.trails = [{"Name": names["cloudtrail"], "IsMultiRegionTrail": True,
                        "LogFileValidationEnabled": True}]
    observed.detector_ids = ["d-0aaa"]
    observed.log_groups = {
        name: {"logGroupName": name,
               "retentionInDays": spec_module.LOG_RETENTION_DAYS}
        for name in names["log_groups"].values()}
    return observed


@th.django_unit_test("a second apply against a converged account creates nothing")
def test_a_second_apply_against_a_converged_account_creates_nothing(opts):
    from mojo.deploy.provision import plan, report

    spec = _spec()
    observed = _converged(spec)
    services = ("ec2", "iam", "s3", "rds", "elasticache", "elbv2", "logs",
                "cloudtrail", "guardduty", "route53", "ssm", "sts")
    clients, stubbers = {}, []
    for service in services:
        client, stubber = _stub(service)
        clients[service] = client
        stubbers.append(stubber)

    # Every Stubber is empty. Any AWS call at all raises UnStubbedResponseError,
    # which report.safe turns into a BLIND finding — so "no BLIND findings"
    # is the assertion that nothing was called.
    for stubber in stubbers:
        stubber.activate()
    try:
        with mock.patch.object(plan.discover, "observe",
                               return_value=([], observed)):
            findings, actions, run = plan.apply(_clients(**clients), spec)
    finally:
        for stubber in stubbers:
            stubber.deactivate()

    blind = [(f.step, f.code, f.message) for f in findings
             if f.status == report.BLIND]
    th.assert_eq(blind, [],
                 "a converged account must produce no AWS calls whatsoever — "
                 "this is the property the no-state-file, resume-by-re-"
                 "observation design rests on")
    th.assert_eq([(a.step, a.verb, a.target) for a in actions], [],
                 "a second apply must plan nothing")
    th.assert_eq(run.worst, report.PASS,
                 f"every finding must be PASS: "
                 f"{[(f.code, f.status) for f in findings if f.status != report.PASS]}")
    th.assert_eq(run.blocking, False, "nothing may be blocking")

    for name, state in run.steps.items():
        th.assert_true(state.status in (plan.OK, plan.DISABLED),
                       f"step {name} reported {state.status} against a "
                       f"converged account")


@th.django_unit_test("a dry run of the whole graph reports what apply would do, and calls nothing")
def test_observe_on_an_empty_account_plans_without_touching_it(opts):
    from mojo.deploy.provision import plan, report

    spec = _spec()
    services = ("ec2", "iam", "s3", "rds", "elasticache", "elbv2", "logs",
                "cloudtrail", "guardduty", "route53", "ssm", "sts")
    clients, stubbers = {}, []
    for service in services:
        client, stubber = _stub(service)
        clients[service] = client
        stubbers.append(stubber)

    empty = plan.discover.blank()
    empty.account_id = ACCOUNT
    empty.caller_arn = f"arn:aws:iam::{ACCOUNT}:user/bootstrap"
    empty.azs = [{"ZoneName": zone} for zone in ZONES]
    empty.offered_zone_names = list(ZONES)

    for stubber in stubbers:
        stubber.activate()
    try:
        with mock.patch.object(plan.discover, "observe",
                               return_value=([], empty)):
            findings, actions, run = plan.observe(_clients(**clients), spec)
    finally:
        for stubber in stubbers:
            stubber.deactivate()

    th.assert_eq([f.code for f in findings if f.status == report.BLIND], [],
                 "a dry run must make no AWS calls at all — every read lives "
                 "in discover.observe, which is what makes it free")
    th.assert_true(actions,
                   "a dry run against an empty account must report what apply "
                   "would build; a silent plan is not a plan")
    planned = {a.step for a in actions}
    for step in ("config_bucket", "secrets", "network", "security_groups",
                 "key_pair", "node_role", "observability"):
        th.assert_in(step, planned,
                     f"{step} has nothing in the account and must plan work: "
                     f"{sorted(planned)}")

    # The database has no endpoint yet on an empty account, so everything
    # downstream of it waits rather than pretending it can run. That is the
    # honest consequence of having no waiters.
    th.assert_eq(run.steps["stage1_payload"].status, plan.PENDING,
                 "the stage-1 payload cannot be written without endpoints")
    th.assert_eq(run.steps["nodes"].status, plan.SKIPPED,
                 "nodes wait for the payload rather than launching against a "
                 "database that does not answer yet")
