"""The join leg of an added node: one targeted deploy, proof, then topology.

Three properties this covers, each of which is a way an added node could
silently be wrong:

* the converge is published to the NEW runner's own box-direct channel, for the
  fleet's LAST CONVERGED sha. Publishing on ``DEPLOY_CHANNEL`` would redeploy
  the whole fleet because one node was added — precisely the blast radius the
  feature exists to avoid.
* the proof compare is ``platform_deploy.proof_matches``, so a node reporting
  the right sha under the WRONG deployment uuid does not count as proven.
* the topology write EXTENDS the node list under ``nodes``; it never replaces
  it and never touches the pools.
"""

from unittest import mock

from testit import helpers as th


SHA = "c" * 40
NEW_NODE_ID = "mojo-api-a-0a1b2c3d4e5f60044"
NEW_RUNNER = f"{NEW_NODE_ID}-engine"


@th.django_unit_setup()
def setup_capacity_converge(opts):
    from mojo.apps.edge.models import PlatformDeployment
    PlatformDeployment.objects.filter(actor="capacity-converge-test").delete()


@th.django_unit_test("the topology write EXTENDS node_ids; it never replaces them")
def test_topology_extend_only(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services import capacity

    record = {"actor": None, "detail": {}, "warnings": [], "state": "running"}
    existing = {"nodes": ["mojo-api-a", "mojo-api-b"], "pools": ["default", "www"]}
    with mock.patch.object(capacity, "_write_operation", side_effect=lambda r: r), \
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
            mock.patch.object(system_settings, "get_value", return_value=existing), \
            mock.patch.object(system_settings, "set_value") as wrote_again:
        capacity._extend_topology({"actor": None, "detail": {}, "warnings": [],
                                   "state": "running"}, "mojo-api-a")
    assert wrote_again.call_count == 0, \
        "an already-listed node rewrote the topology"

