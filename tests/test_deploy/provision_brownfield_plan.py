from unittest import mock

from objict import objict
from testit import helpers as th

from .brownfield_fixture import topology


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
