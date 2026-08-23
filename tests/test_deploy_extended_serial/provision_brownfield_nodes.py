import hashlib

from objict import objict
from testit import helpers as th

from test_deploy.brownfield_fixture import preserved_topology, topology


@th.django_unit_test()
def test_omitted_request_service_preserves_pre_feature_stage0_bytes(opts):
    from mojo.deploy.provision import nodes

    spec = topology()
    declaration = spec.node_declarations[0]
    script = nodes.stage0_user_data(spec, declaration["name"], declaration)
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()

    th.assert_eq(
        digest,
        "b9f50a45807990f011fc581cc28c9553b0b4e6e9e42998826cf25d852d9a089d",
        "omission must retain the exact pre-feature RunInstances UserData bytes")
    th.assert_eq("/etc/mojo/request-service" in script, False,
                 "omission must not project the new authority or systemd marker")


@th.django_unit_test()
def test_request_service_projection_is_strict_and_brownfield_only(opts):
    from mojo.deploy.provision import nodes, spec as spec_module

    spec = topology()
    declaration = dict(spec.node_declarations[0], request_service=False)
    script = nodes.stage0_user_data(spec, declaration["name"], declaration)
    th.assert_in(
        "MOJO_REQUEST_SERVICE=false", script,
        "stage 0 must seal the exact non-request lifecycle under /etc/mojo")
    th.assert_in(
        "printf '%s\\n' 'MOJO_REQUEST_SERVICE=false' > "
        "/etc/mojo/request-service.conf", script,
        "the sealed authority must be written as one exact key=value line")
    th.assert_in(
        "rm -f -- /etc/mojo/request-service.enabled", script,
        "a non-request node must remove the systemd enable marker")
    th.assert_in(
        "ConditionPathExists=/etc/mojo/request-service.enabled", script,
        "the persistent systemd drop-in must survive framework rollback")
    th.assert_in(
        "CONFIG_SYNC_RESTART=false", script,
        "a later config-sync must not resurrect intentionally disabled ASGI")
    th.assert_in(
        "chmod 0600 /etc/mojo/node-role.json", script,
        "the lifecycle projection must not weaken the opaque role document")

    malformed = dict(spec.node_declarations[0], request_service="false")
    try:
        nodes.stage0_user_data(spec, malformed["name"], malformed)
    except ValueError as err:
        th.assert_in("JSON boolean", str(err),
                     f"the strict projection must explain its refusal: {err}")
    else:
        raise AssertionError(
            "a string request_service reached root user data as authority")

    managed = spec_module.build("demo", "prod", "us-west-2", preset="small")
    try:
        nodes.stage0_user_data(
            managed, "demo1", {"role": "default", "serving_target": True,
                                "request_service": False})
    except ValueError as err:
        th.assert_in("brownfield-only", str(err),
                     f"managed misuse must name the mode boundary: {err}")
    else:
        raise AssertionError(
            "a managed node accepted a brownfield-only lifecycle declaration")


@th.django_unit_test()
def test_nodes_report_roles_and_use_declared_subnets_profiles(opts):
    from mojo.deploy.provision import nodes

    spec = topology()
    declaration = dict(spec.node_declarations[0], request_service=False)
    spec.node_declarations[0] = declaration
    instance = objict(
        InstanceId="i-aaaaaaaaaaaaaaaaa", ImageId=spec.ami_override,
        InstanceType=spec.node_type, State={"Name": "running"},
        Tags=[{"Key": "Name", "Value": declaration["name"]},
              {"Key": "mojo:request-service", "Value": "false"}])
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
    th.assert_eq(records[0]["request_service"], False,
                 "NLB eligibility must remain independent of framework ASGI")
    th.assert_true(any(row.code == "node.data_plane_canary"
                       for row in findings),
                   f"metadata-only provisioning must require live DB/cache proof: {findings}")
    th.assert_eq(result.as_dict()["required_canary_proofs"][0]["database"],
                 "SELECT 1", "the handoff must name the required DB proof")
    th.assert_eq(
        result.as_dict()["required_canary_proofs"][0]["request_service"],
        {"authority": "/etc/mojo/request-service.conf", "expected": False,
         "systemd": "inactive and disabled; enable marker absent"},
        "the handoff must require node-side proof of the sealed local role")


@th.django_unit_test()
def test_node_launch_tags_only_explicit_request_service(opts):
    from mojo.deploy.provision import nodes

    spec = topology()

    class _EC2:
        def __init__(self):
            self.calls = []

        def run_instances(self, **kwargs):
            self.calls.append(kwargs)
            return {"Instances": [{"InstanceId": "i-new"}]}

    def launch(declaration):
        ec2 = _EC2()
        nodes._launch(
            ec2, spec, {}, declaration["name"], spec.ami_override,
            declaration["subnet_id"],
            spec.brownfield_manifest["network"]["node_security_group_id"],
            declaration["instance_profile_arn"],
            spec.brownfield_manifest["nodes"]["key_pair_name"], [],
            declaration=declaration)
        return {row["Key"]: row["Value"] for row in
                ec2.calls[0]["TagSpecifications"][0]["Tags"]}

    omitted = launch(spec.node_declarations[0])
    th.assert_true("mojo:request-service" not in omitted,
                   "omission must preserve the pre-feature RunInstances tag shape")
    explicit = launch(dict(spec.node_declarations[0], request_service=False))
    th.assert_eq(explicit["mojo:request-service"], "false",
                 "an explicit role must be bound into RunInstances itself")


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
def test_preserved_mode_prepares_two_az_nlb_with_temporary_addresses(opts):
    from mojo.deploy.provision import balancer

    spec = preserved_topology()
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


class _ClientsForBalancer:
    def __init__(self, **clients):
        self.clients = clients

    def get(self, name):
        return self.clients[name]
