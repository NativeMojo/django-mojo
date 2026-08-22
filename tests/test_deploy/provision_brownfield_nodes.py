from objict import objict
from testit import helpers as th

from .brownfield_fixture import handoff_topology, topology


@th.django_unit_test()
def test_role_user_data_is_version_pinned_digest_checked_and_root_owned(opts):
    from mojo.deploy.provision import nodes

    spec = topology()
    declaration = spec.node_declarations[0]
    script = nodes.stage0_user_data(spec, declaration["name"], declaration)
    for expected in ("MOJO_NODE_ROLE=api", "--version-id=stageversion1",
                     "--version-id=configversion1",
                     "sha256sum -c -", "/etc/mojo/node-role.json",
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


@th.django_unit_test()
def test_nodes_report_roles_and_use_declared_subnets_profiles(opts):
    from mojo.deploy.provision import nodes

    spec = topology()
    declaration = spec.node_declarations[0]
    instance = objict(
        InstanceId="i-aaaaaaaaaaaaaaaaa", ImageId=spec.ami_override,
        InstanceType=spec.node_type, State={"Name": "running"},
        Tags=[{"Key": "Name", "Value": declaration["name"]}])
    observed = objict(
        instances=[instance], ami_id=spec.ami_override,
        public_subnet_ids=spec.nlb_subnet_ids,
        node_sg_id=spec.brownfield_manifest["network"]["node_security_group_id"],
        key_pair_name=spec.brownfield_manifest["nodes"]["key_pair_name"],
        bootstrap_payload=True,
        brownfield_profiles={"api": {
            "profile_arn": declaration["instance_profile_arn"]}})

    class _Clients:
        def get(self, name):
            return object()

    findings, _actions, result = nodes.ensure_nodes(
        _Clients(), spec, observed, apply=False)
    records = result.as_dict()["node_records"]
    th.assert_eq(records[0]["role"], "api",
                 f"node results must retain application roles: {records}")
    th.assert_true(records[0]["serving_target"],
                   f"serving eligibility must survive into balancer input: {records}")
    th.assert_true(any(row.code == "node.data_plane_canary"
                       for row in findings),
                   f"metadata-only provisioning must require live DB/cache proof: {findings}")
    th.assert_eq(result.as_dict()["required_canary_proofs"][0]["database"],
                 "SELECT 1", "the handoff must name the required DB proof")


@th.django_unit_test()
def test_balancer_selects_serving_nodes_plus_explicit_compatibility(opts):
    from mojo.deploy.provision import balancer

    spec = topology()
    observed = objict(
        vpc_id=spec.brownfield_manifest["network"]["vpc_id"],
        public_subnet_ids=spec.nlb_subnet_ids,
        node_records=[
            {"instance_id": "i-serving", "serving_target": True},
            {"instance_id": "i-worker", "serving_target": False}],
        target_groups={}, balancer=None, addresses=[])

    class _Clients:
        def get(self, name):
            return object()

    _findings, _actions, result = balancer.ensure_balancer(
        _Clients(), spec, observed, apply=False)
    wanted = result.as_dict()["serving_instance_ids"]
    th.assert_eq(wanted, ["i-serving", "i-0123456789abcdef0"],
                 f"only serving nodes and declared compatibility targets belong: {wanted}")
    th.assert_eq(result.as_dict()["compatibility_target_ids"],
                 ["i-0123456789abcdef0"],
                 "compatibility registration must remain explicitly non-owned")


@th.django_unit_test()
def test_new_balancer_preview_covers_every_apply_action(opts):
    from mojo.deploy.provision import balancer

    spec = topology()
    observed = objict(
        vpc_id=spec.brownfield_manifest["network"]["vpc_id"],
        node_records=[{"instance_id": "i-serving", "serving_target": True}],
        target_groups={}, balancer=None, addresses=[], listeners=[], targets={},
        balancer_attributes={})

    class _EC2:
        def __init__(self):
            self.index = 0

        def allocate_address(self, **kwargs):
            self.index += 1
            return {"AllocationId": f"eipalloc-{self.index}"}

    class _ELB:
        def create_target_group(self, Name, **kwargs):
            return {"TargetGroups": [{"TargetGroupArn":
                    f"arn:aws:elasticloadbalancing:us-west-2:123456789012:"
                    f"targetgroup/{Name}/abc"}]}

        def create_load_balancer(self, **kwargs):
            return {"LoadBalancers": [{
                "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-west-2:"
                "123456789012:loadbalancer/net/maestro-shadow-nlb/abc"}]}

        def __getattr__(self, name):
            return lambda **kwargs: {}

    clients = _ClientsForBalancer(ec2=_EC2(), elbv2=_ELB())
    _pf, preview_actions, _pr = balancer.ensure_balancer(
        clients, spec, observed, apply=False)
    _af, apply_actions, _ar = balancer.ensure_balancer(
        clients, spec, observed, apply=True)
    preview = {(row.step, row.verb, row.target) for row in preview_actions}
    applied = {(row.step, row.verb, row.target) for row in apply_actions}
    th.assert_eq(preview, applied,
                 f"the approved preview must cover every apply action: "
                 f"preview={preview}, apply={applied}")


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
def test_preserved_mode_prepares_two_az_nlb_with_temporary_addresses(opts):
    from mojo.deploy.provision import balancer

    spec = handoff_topology()
    spec.nlb_security_group_id = "sg-3123456789abcdef0"
    observed = objict(
        vpc_id=spec.brownfield_manifest["network"]["vpc_id"],
        node_records=[{"instance_id": "i-serving", "serving_target": True}],
        target_groups={}, balancer=None, addresses=[], listeners=[], targets={},
        balancer_attributes={})

    class _EC2:
        def allocate_address(self, **kwargs):
            raise AssertionError(
                "preserved mode must not allocate replacement EIPs")

    class _ELB:
        def __init__(self):
            self.request = None

        def create_target_group(self, Name, **kwargs):
            return {"TargetGroups": [{"TargetGroupArn": f"tg/{Name}"}]}

        def create_load_balancer(self, **kwargs):
            self.request = kwargs
            return {"LoadBalancers": [{"LoadBalancerArn": "lb/shadow"}]}

        def __getattr__(self, name):
            return lambda **kwargs: {}

    elb = _ELB()
    _findings, preview_actions, _result = balancer.ensure_balancer(
        _ClientsForBalancer(ec2=_EC2(), elbv2=elb), spec, observed,
        apply=False)
    create_action = next(
        row for row in preview_actions
        if row.verb == "create" and row.target == spec.nlb_name)
    th.assert_eq(
        create_action.detail,
        '{"SecurityGroups":["sg-3123456789abcdef0"]}',
        "preview/action CAS must bind the irreversible create-time NLB SG")
    balancer.ensure_balancer(
        _ClientsForBalancer(ec2=_EC2(), elbv2=elb), spec, observed,
        apply=True)
    th.assert_eq(elb.request["Subnets"], spec.nlb_subnet_ids,
                 f"AWS must assign temporary addresses in both AZs: {elb.request}")
    th.assert_eq("SubnetMappings" in elb.request, False,
                 f"live allocation ids must not reach preparation: {elb.request}")
    th.assert_eq(elb.request["SecurityGroups"],
                 ["sg-3123456789abcdef0"],
                 f"the NLB SG must be attached irreversibly at create: {elb.request}")


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


class _ClientsForBalancer:
    def __init__(self, **clients):
        self.clients = clients

    def get(self, name):
        return self.clients[name]
