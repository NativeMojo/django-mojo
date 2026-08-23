"""Split out of tests/test_edge/29_capacity_converge.py (maestro #1839).

These tests patch shared production surfaces (mojo.apps.jobs.publish,
mojo.apps.jobs.manager.get_manager, mojo.apps.jobs.get_runners_bounded) —
process-global, so unsafe under the parallel default tier. The properties
under test are the source module's; see its docstring.
"""

import uuid
from unittest import mock

from testit import helpers as th


SHA = "c" * 40


NEW_NODE_ID = "mojo-api-a-0a1b2c3d4e5f60044"


NEW_RUNNER = f"{NEW_NODE_ID}-engine"


@th.django_unit_setup()
def setup_capacity_converge(opts):
    from mojo.apps.edge.models import PlatformDeployment
    PlatformDeployment.objects.filter(actor="capacity-converge-test").delete()


def _converged_row():
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy

    with mock.patch.object(platform_deploy, "edge_roster",
                           return_value=["edge-a-engine"]):
        row, _ = platform_deploy.create(
            SHA, actor="capacity-converge-test", source="test")
    platform_deploy.set_framework(row.pk, "1.13.0")
    row.status = PlatformDeployment.STATUS_CONVERGED
    row.save(update_fields=["status", "modified"])
    return platform_deploy.get(row.pk)


@th.django_unit_test("the added node's converge is ONE targeted publish, never the fleet channel")
def test_converge_targets_only_the_new_runner(opts):
    from mojo.apps import jobs
    from mojo.apps.edge.asyncjobs import _publish_deploy_node
    from mojo.apps.edge.services import deploy, platform_deploy

    row = _converged_row()
    with mock.patch.object(jobs, "publish", return_value="job-1") as published:
        _publish_deploy_node(NEW_RUNNER, row.sha, row.framework_version,
                             migrate=False, deployment_id=row.pk)

    assert published.call_count == 1, \
        f"the join leg published {published.call_count} jobs, not one"
    params = published.call_args.kwargs
    assert params["channel"] == NEW_RUNNER, \
        (f"the converge went to {params['channel']!r} instead of the new "
         f"runner's own channel")
    assert params["channel"] != deploy.DEPLOY_CHANNEL, \
        "adding one node published on the fleet-wide deploy channel"
    assert params["func"] == deploy.DEPLOY_NODE_JOB, \
        f"the join leg published the wrong job: {params['func']}"
    assert params["payload"]["sha"] == SHA, \
        f"the new node was not sent the last converged sha: {params['payload']}"
    assert params["payload"]["migrate"] is False, \
        "an added node was told to run migrations; the fleet is already on this sha"
    assert params["payload"]["deployment"] == str(row.pk), \
        f"the converge did not carry the converged deployment's uuid: {params['payload']}"
    assert params["max_retries"] == 0, \
        "a redelivered deploy job would re-run a whole update on the new node"


@th.django_unit_test("the added node is proven by proof_matches, not by its sha alone")
def test_capacity_proof_uses_proof_matches(opts):
    from mojo.apps.aws.services import capacity
    from mojo.apps.edge.services import platform_deploy

    row = _converged_row()
    good = {"platform_deployment": str(row.pk), "platform_sha": SHA,
            "node_id": NEW_NODE_ID}
    wrong_uuid = {**good, "platform_deployment": str(uuid.uuid4())}
    wrong_sha = {**good, "platform_sha": "d" * 40}

    def responder(proof):
        return lambda *args, **kwargs: {"status": "success", "result": proof}

    for proof, expected, why in (
            (good, True, "a node reporting this deployment's uuid and sha"),
            (wrong_uuid, False, "a node reporting the right sha under another attempt"),
            (wrong_sha, False, "a node reporting a different sha"),
            (None, False, "a runner that answered with no proof at all")):
        manager = mock.Mock()
        manager.execute_on_runner.side_effect = responder(proof)
        with mock.patch.object(capacity, "_sleep"), \
                mock.patch("mojo.apps.jobs.manager.get_manager",
                           return_value=manager):
            result = capacity._await_proof(NEW_RUNNER, row, timeout=0.2)
        assert result is expected, f"{why} was judged {result}"

    # The proof leg asks the NEW runner for the node proof, with this exact
    # deployment and sha — the same question the fleet reconciler asks.
    assert manager.execute_on_runner.call_args[0][0] == NEW_RUNNER, \
        "the proof was collected from the wrong runner"
    assert manager.execute_on_runner.call_args[0][1] == \
        "mojo.apps.edge.services.readiness.local_node_proof", \
        "the proof leg called something other than the node proof"
    assert platform_deploy.proof_matches(row, good) is True, \
        "the shared matcher disagrees with what the capacity leg accepted"


@th.django_unit_test("the runner the join leg waits for is the one the user-data creates")
def test_runner_identity_is_derived_not_discovered(opts):
    from mojo.apps.aws.services import capacity

    node_id = capacity.expected_node_id("mojo-api-a", "i-0a1b2c3d4e5f60044")
    assert node_id == NEW_NODE_ID, \
        f"the predicted node id changed shape: {node_id}"
    assert capacity.expected_runner_id(node_id) == NEW_RUNNER, \
        "the runner id is not the node id plus the engine suffix"

    # The wait is for that EXACT runner. A roster diff would race a heartbeat
    # from any unrelated node that happened to join in the same window.
    rows = [{"runner_id": "edge-a-engine", "alive": True},
            {"runner_id": NEW_RUNNER, "alive": True}]
    with mock.patch.object(capacity, "_sleep"), \
            mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=rows):
        assert capacity._await_runner(NEW_RUNNER, timeout=0.2) is True, \
            "the join leg did not see a runner that had joined"
    with mock.patch.object(capacity, "_sleep"), \
            mock.patch("mojo.apps.jobs.get_runners_bounded",
                       return_value=[{"runner_id": NEW_RUNNER, "alive": False}]):
        assert capacity._await_runner(NEW_RUNNER, timeout=0.2) is False, \
            "a dead runner row was accepted as a joined node"


@th.django_unit_test("the topology write EXTENDS node_ids; it never replaces them")
def test_topology_extend_only(opts):
    """Moved from tests/test_edge/29_capacity_converge.py (maestro #2558):
    patches capacity._write_operation and the shared
    mojo.apps.account.services.system_settings surface — process-global, so
    unsafe under the parallel default tier."""
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services import capacity

    record = {"actor": None, "detail": {}, "warnings": [], "state": "running"}
    existing = {"nodes": ["mojo-api-a", "mojo-api-b"], "pools": ["default", "www"]}
    with mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "_persist",
                              side_effect=lambda record, **kw: record), \
            mock.patch.object(system_settings, "get_value", return_value=existing), \
            mock.patch.object(system_settings, "set_value") as wrote:
        capacity._extend_topology(record, NEW_NODE_ID)

    assert wrote.call_count == 1, \
        f"the topology was written {wrote.call_count} times, not one"
    _, key, value = wrote.call_args[0]
    assert key == system_settings.EXPECTED_EDGE_TOPOLOGY, \
        f"the wrong setting was written: {key}"
    assert set(value["nodes"]) == {"mojo-api-a", "mojo-api-b", NEW_NODE_ID}, \
        f"the write did not extend the existing node list: {value['nodes']}"
    assert value["pools"] == ["default", "www"], \
        f"the write changed the pools it had no business touching: {value['pools']}"
    assert record["detail"]["topology_nodes"] == value["nodes"], \
        "the operation record does not carry what was written"

    # A node already listed is not written again — an add that reuses a
    # hostname must not churn a protected setting.
    with mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
            mock.patch.object(capacity, "_persist",
                              side_effect=lambda record, **kw: record), \
            mock.patch.object(system_settings, "get_value", return_value=existing), \
            mock.patch.object(system_settings, "set_value") as wrote_again:
        capacity._extend_topology({"actor": None, "detail": {}, "warnings": [],
                                   "state": "running"}, "mojo-api-a")
    assert wrote_again.call_count == 0, \
        "an already-listed node rewrote the topology"
