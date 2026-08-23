"""Split out of tests/test_edge/24_readiness.py (maestro #1839).

These tests patch shared production surfaces
(mojo.apps.jobs.get_runners_bounded, mojo.apps.jobs.manager.get_manager,
mojo.apps.jobs.job_engine.host_channel) — process-global, so unsafe under the
parallel default tier.
"""

from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import with_setting


@th.django_unit_test("fleet discovery calls only live edge-channel runners")
def test_fleet_discovery_is_edge_channel_only(opts):
    import mojo
    from mojo.apps.edge.services import readiness

    runner = {"runner_id": "edge-a-engine", "alive": True, "channels": ["edge"]}
    proof = {"status": "success", "result": {
        "node_id": "edge-a", "django_mojo_version": mojo.__version__,
        "pools": {"default": {"generation": "g", "excluded": 0,
                                "www_pending": 0, "cert_pending": 0,
                                "serving_generation": "combined",
                                "current_generation": "combined"}}}}
    manager = mock.Mock()
    manager.execute_on_runner.return_value = proof
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[runner]) as get_runners, \
            mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
            mock.patch.object(readiness, "_desired_generations",
                              return_value={"default": "g"}), \
            mock.patch("mojo.apps.account.services.system_settings.get_value",
                       return_value={"nodes": ["edge-a"], "pools": ["default"]}):
        rows = readiness.check_fleet({"timeout": 0.1})
    assert get_runners.call_count == 1, (
        f"fleet discovery must resolve the roster exactly once, got "
        f"{get_runners.call_count} calls")
    assert get_runners.call_args.kwargs.get("channel") == "edge", (
        f"fleet discovery must ask only for edge-channel runners — the filter "
        f"is what keeps unrelated job runners from receiving node-proof calls; "
        f"got {get_runners.call_args!r}")
    assert rows[0]["status"] == "pass", f"valid node/pool proof was not green: {rows}"


@th.django_unit_test("a missing node or pool proof can never report green")
def test_missing_node_and_pool_are_pending(opts):
    import mojo
    from mojo.apps.edge.services import readiness

    topology = {"nodes": ["edge-a", "edge-b"], "pools": ["default", "blue"]}
    runner = {"runner_id": "edge-a-engine", "alive": True, "channels": ["edge"]}
    proof = {"status": "success", "result": {
        "node_id": "edge-a", "django_mojo_version": mojo.__version__,
        "pools": {"default": {"generation": "gd", "excluded": 0,
                                "www_pending": 0, "cert_pending": 0,
                                "serving_generation": "combined",
                                "current_generation": "combined"}}}}
    manager = mock.Mock()
    manager.execute_on_runner.return_value = proof
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[runner]), \
            mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
            mock.patch.object(readiness, "_desired_generations",
                              return_value={"default": "gd", "blue": "gb"}), \
            mock.patch("mojo.apps.account.services.system_settings.get_value",
                       return_value=topology):
        rows = readiness.check_fleet({"timeout": 0.1})
    by_code = {row["code"]: row for row in rows}
    assert by_code["fleet.node.edge-a.pool.blue"]["status"] == "pending", \
        "missing pool proof was reported green"
    assert by_code["fleet.node.edge-b"]["status"] == "pending", \
        "missing expected node was reported green"


@th.django_unit_test("fleet proof refuses failed, stale, wrong, and degraded evidence")
def test_fleet_proof_adverse_states(opts):
    import mojo
    from mojo.apps.edge.services import readiness

    topology = {"nodes": ["edge-a"], "pools": ["default"]}
    runner = {"runner_id": "edge-a-engine", "alive": True, "channels": ["edge"]}

    def check(response):
        manager = mock.Mock()
        manager.execute_on_runner.return_value = response
        with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[runner]), \
                mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
                mock.patch.object(readiness, "_desired_generations",
                                  return_value={"default": "wanted"}), \
                mock.patch("mojo.apps.account.services.system_settings.get_value",
                           return_value=topology):
            return readiness.check_fleet({"timeout": 0.1})[0]["status"]

    assert check({"status": "error", "error": "private remote detail"}) == "pending", \
        "a failed node-proof response was reported green"
    base = {
        "node_id": "edge-a", "django_mojo_version": mojo.__version__,
        "pools": {"default": {
            "generation": "wanted", "excluded": 0, "www_pending": 0,
            "cert_pending": 0, "serving_generation": "combined",
            "current_generation": "combined"}}}
    stale = dict(base, django_mojo_version="0.0-stale")
    assert check({"status": "success", "result": stale}) == "fail", \
        "a stale django-mojo version was not a hard readiness failure"
    for field, value in (
            ("generation", "wrong"), ("excluded", 1), ("www_pending", 1),
            ("cert_pending", 1), ("current_generation", "other")):
        proof = {**base, "pools": {"default": {
            **base["pools"]["default"], field: value}}}
        assert check({"status": "success", "result": proof}) == "pending", \
            f"degraded fleet proof {field}={value!r} was reported green"


@th.django_unit_test("node proof identity defaults to the normalized job hostname")
def test_local_node_id_automatic_with_optional_override(opts):
    from mojo.apps.edge.services import readiness

    with mock.patch.object(
            readiness.settings, "get_static", return_value=""), \
         mock.patch(
            "mojo.apps.jobs.job_engine.host_channel",
            return_value="cross-platform-node"):
        assert readiness.local_node_id() == "cross-platform-node", \
            "an ordinary host still required project-owned EDGE_NODE_ID config"

    explicit = with_setting(
        "EDGE_NODE_ID", "Stable_Override",
        lambda: readiness.local_node_id())
    assert explicit == "stable_override", \
        "the file-only override did not win over automatic hostname identity"



@th.django_unit_test("an unreadable roster reports once, not once per node")
def test_fleet_unreadable_roster_reports_once(opts):
    """Item #2730: get_runners swallowed its errors into [], so a Redis fault
    rendered as N nodes each told to 'start its edge-channel runner' — the one
    place the problem was not."""
    from mojo.apps.edge.services import readiness

    topology = {"nodes": ["edge-a", "edge-b"], "pools": ["default", "blue"]}
    manager = mock.Mock()

    with mock.patch("mojo.apps.jobs.get_runners_bounded",
                    side_effect=RuntimeError("runner_roster_invalid")), \
            mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
            mock.patch.object(readiness, "_desired_generations",
                              return_value={"default": "g", "blue": "g"}), \
            mock.patch("mojo.apps.account.services.system_settings.get_value",
                       return_value=topology):
        rows = readiness.check_fleet({"timeout": 0.1})

    assert len(rows) == 1, (
        f"an unprovable roster must produce ONE row about the roster, not a "
        f"verdict per node; got {[row['code'] for row in rows]}")
    assert rows[0]["code"] == "fleet.roster", \
        f"expected a fleet.roster row, got {rows[0]['code']!r}"
    assert rows[0]["status"] == "fail", \
        f"a check that could not run is a failure, got {rows[0]['status']!r}"
    assert "clock skew" in rows[0]["remediation"], (
        f"runner_roster_invalid must point at clock skew first, not Redis; "
        f"got {rows[0]['remediation']!r}")
    assert not manager.execute_on_runner.called, (
        "node-proof calls were issued without a trustworthy roster — with no "
        "provable edge roster there is no legitimate set of runners to call")


@th.django_unit_test("an empty roster still reports every expected node")
def test_fleet_empty_roster_still_blames_nodes(opts):
    """The regression guard on the distinction: 'no runners' and 'could not
    read' must not collapse back into one answer."""
    from mojo.apps.edge.services import readiness

    topology = {"nodes": ["edge-a", "edge-b"], "pools": ["default"]}
    manager = mock.Mock()
    manager.execute_on_runner.return_value = None

    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[]), \
            mock.patch("mojo.apps.jobs.manager.get_manager", return_value=manager), \
            mock.patch.object(readiness, "_desired_generations",
                              return_value={"default": "g"}), \
            mock.patch("mojo.apps.account.services.system_settings.get_value",
                       return_value=topology):
        rows = readiness.check_fleet({"timeout": 0.1})

    by_code = {row["code"]: row for row in rows}
    assert "fleet.roster" not in by_code, (
        "an empty fleet is not an unreadable roster; reporting it as one hides "
        f"which nodes are missing: {list(by_code)}")
    assert by_code["fleet.node.edge-a"]["status"] == "pending", \
        f"a missing node must still be reported per node: {by_code.keys()}"
    assert by_code["fleet.node.edge-b"]["status"] == "pending", \
        f"a missing node must still be reported per node: {by_code.keys()}"
    assert "fleet.summary" in by_code, \
        f"the aggregate row is expected on the normal path: {list(by_code)}"


@th.django_unit_test("a programming error is not reported as a Redis fault")
def test_fleet_programming_error_is_not_reported_as_redis(opts):
    """The catch is narrow so that system_readiness.run()'s generic wrapper —
    'inspect the server log' — keeps owning bugs."""
    from mojo.apps.edge.services import readiness

    topology = {"nodes": ["edge-a"], "pools": ["default"]}

    with mock.patch("mojo.apps.jobs.get_runners_bounded",
                    side_effect=TypeError("unexpected keyword argument")), \
            mock.patch.object(readiness, "_desired_generations",
                              return_value={"default": "g"}), \
            mock.patch("mojo.apps.account.services.system_settings.get_value",
                       return_value=topology):
        raised = None
        try:
            readiness.check_fleet({"timeout": 0.1})
        except TypeError as error:
            raised = error

    assert raised is not None, (
        "a programming error was swallowed into a 'could not read the roster' "
        "row, sending the operator to Redis for a code bug")
