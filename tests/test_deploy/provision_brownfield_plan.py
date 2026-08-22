import json
from unittest import mock

from objict import objict
from testit import helpers as th

from .brownfield_fixture import topology


def _health_path_modify_action(path):
    from mojo.deploy.provision import balancer

    spec = topology()
    spec.api_health_path = path
    vpc_id = spec.brownfield_manifest["network"]["vpc_id"]
    wanted = balancer.target_group_specs(spec, vpc_id)
    existing = {
        role: dict(request, TargetGroupArn=f"arn:tg:{role}")
        for role, request in wanted.items()}
    existing["api"]["HealthCheckPath"] = balancer.HEALTH_PATH
    findings, actions = [], []
    balancer._ensure_target_groups(
        None, spec, {"target_groups": existing}, wanted,
        findings, actions, apply=False)
    return actions[0]


@th.django_unit_test()
def test_health_path_value_is_bound_into_the_preview_action_digest(opts):
    from mojo.deploy.provision import brownfield_plan

    first_path = "/api/maestro/node/ready"
    second_path = "/api/maestro/node/ready-v2"
    first = _health_path_modify_action(first_path)
    second = _health_path_modify_action(second_path)

    th.assert_eq(json.loads(first.detail), {"HealthCheckPath": first_path},
                 "modify detail must carry the exact desired health path")
    th.assert_eq(json.loads(second.detail), {"HealthCheckPath": second_path},
                 "JSON detail must preserve a second valid path exactly")
    th.assert_true(
        brownfield_plan._action_digest([first])
        != brownfield_plan._action_digest([second]),
        "changing only the desired health path must change the action digest")


def _client_ip_modify_action(value):
    from mojo.deploy.provision import balancer

    spec = topology()
    spec.api_preserve_client_ip = value
    vpc_id = spec.brownfield_manifest["network"]["vpc_id"]
    wanted = balancer.target_group_specs(spec, vpc_id)
    current = "false" if value else "true"
    existing = {
        role: dict(request, TargetGroupArn=f"arn:tg:{role}",
                   TargetGroupAttributes={"preserve_client_ip.enabled": current})
        for role, request in wanted.items()}
    findings, actions = [], []
    balancer._ensure_target_groups(
        None, spec, {"target_groups": existing}, wanted,
        findings, actions, apply=False)
    return [action for action in actions if action.target == wanted["api"]["Name"]][0]


@th.django_unit_test()
def test_client_ip_value_is_bound_into_the_preview_action_digest(opts):
    from mojo.deploy.provision import brownfield_plan

    disabled = _client_ip_modify_action(False)
    enabled = _client_ip_modify_action(True)
    th.assert_eq(json.loads(disabled.detail),
                 {"preserve_client_ip.enabled": "false"},
                 "preview evidence must carry the exact disabled posture")
    th.assert_eq(json.loads(enabled.detail),
                 {"preserve_client_ip.enabled": "true"},
                 "preview evidence must carry the exact enabled posture")
    th.assert_true(
        brownfield_plan._action_digest([disabled])
        != brownfield_plan._action_digest([enabled]),
        "changing only source-IP posture must change the action digest")


@th.django_unit_test()
def test_explicit_request_service_is_bound_without_changing_omitted_action(opts):
    from mojo.deploy.provision import brownfield_plan, nodes, report

    spec = topology()
    declaration = spec.node_declarations[0]
    omitted = nodes._create_action_detail(spec, declaration)
    th.assert_eq(omitted, spec.node_type,
                 "omission must preserve the pre-feature node action detail")

    details = []
    actions = []
    for selected in (True, False):
        explicit = dict(declaration, request_service=selected)
        detail = nodes._create_action_detail(spec, explicit)
        details.append(json.loads(detail))
        actions.append(report.Action(
            "nodes", "create", declaration["name"], detail))
    th.assert_eq(details[0]["request_service"], True,
                 "the reviewed action must carry explicit request authority")
    th.assert_eq(details[1]["request_service"], False,
                 "the reviewed action must carry explicit non-request authority")
    th.assert_true(
        brownfield_plan._action_digest([actions[0]])
        != brownfield_plan._action_digest([actions[1]]),
        "changing only request_service must change the reviewed action digest")


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
def test_brownfield_dag_contains_only_identity_nodes_balancer_telemetry(opts):
    from mojo.deploy.provision import brownfield_plan

    names = [name for name, _ensure in brownfield_plan.STEPS]
    th.assert_eq(names, ["identity", "nodes", "balancer", "telemetry"],
                 f"data, DNS, certificate and managed steps must be absent: {names}")
    modules = [ensure.__module__ for _name, ensure in brownfield_plan.STEPS]
    for forbidden in (".data", ".dns", ".certificate", ".storage",
                      ".network", ".encryption"):
        th.assert_eq(any(name.endswith(forbidden) for name in modules), False,
                     f"the brownfield DAG must not import a {forbidden} ensure")


@th.django_unit_test()
def test_action_validation_runs_before_apply_walk(opts):
    from mojo.deploy.provision import brownfield_plan, brownfield_policy, report

    action = report.Action("database", "create", "Aurora production")
    raised = None
    try:
        brownfield_policy.validate_actions(
            [action], brownfield_plan._allowed_action_targets(topology()))
    except brownfield_policy.BrownfieldCallBlocked as err:
        raised = err
    th.assert_true(raised is not None,
                   "a data-plane action must fail the complete preview gate")
    th.assert_in("outside", str(raised),
                 f"the rejection must explain the allowlist boundary: {raised}")


@th.django_unit_test()
def test_prepare_rejects_similarly_named_unowned_action(opts):
    from mojo.deploy.provision import brownfield_plan, brownfield_policy, report

    def forged(_clients, _topology, _observed, _apply):
        result = report.Result()
        return [], [report.Action(
            "balancer", "modify", "maestro-shadow-nlb-unowned")], result

    observed = objict(dependency_digest="stable")
    raised = None
    with mock.patch.object(brownfield_plan.brownfield_discover, "observe",
                           return_value=([], observed)), \
            mock.patch.object(brownfield_plan, "STEPS",
                              (("balancer", forged),)):
        try:
            brownfield_plan._prepare(object(), topology())
        except brownfield_policy.BrownfieldCallBlocked as err:
            raised = err
    th.assert_true(raised is not None,
                   "an allowed verb must not authorize a similar unowned name")
    th.assert_in("not an exact declared", str(raised),
                 f"the action gate must explain its exact-name refusal: {raised}")


@th.django_unit_test()
def test_expected_digest_is_mandatory_not_optional_state(opts):
    from mojo.deploy.provision import brownfield_plan

    raised = None
    try:
        brownfield_plan.apply(object(), topology(), None, None)
    except brownfield_plan.DependencyDriftError as err:
        raised = err
    th.assert_true(raised is not None,
                   "apply without its immediately preceding preview must refuse")
    th.assert_in("requires", str(raised),
                 f"the error must explain the preview contract: {raised}")


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
