"""
Startup convergence (maestro #1772): a node reconciles itself because its
engine started, not because a broadcast happened to arrive while it was
listening. Fan-out resolves the roster at publish time, so a broadcast sent
while an engine restarts — which every deploy causes — quietly skips it.

The handler is deliberately local: it installs pools and publishes nothing.
The hook-firing mechanics (start()-only, failure containment) are covered in
`test_job_engine/test_startup_hooks.py`; this file covers the edge handler.
"""
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import declare_pools, with_setting


def _engine(runner_id="test-node-engine"):
    return mock.Mock(runner_id=runner_id)


@th.django_unit_test("the startup hook is registered by the edge app")
def test_startup_hook_is_registered(opts):
    """apps.EdgeConfig.ready() ran in this very process — the registration it
    performed is what a real runner daemon fires on start."""
    from mojo.apps import jobs

    assert "mojo.apps.edge.asyncjobs.on_engine_start" in jobs.get_startup_hooks(), (
        "the edge app did not register its converge hook — a restarting node "
        f"would depend on the sweep again (registered: {jobs.get_startup_hooks()})")


@th.django_unit_test("startup converge installs every pool, publishes nothing")
def test_startup_converge_installs_pools(opts):
    from mojo.apps.edge import asyncjobs

    declare_pools(["alpha", "beta"])
    installed = []

    try:
        with mock.patch(
                "mojo.apps.edge.services.installer.install_pools",
                side_effect=lambda pools: installed.append(list(pools)) or mock.Mock(changed=True)), \
                th.capture_publishes(
                    lambda c: c.get("channel") == "edge" and c.get("broadcast")
                ) as edge_broadcasts:
            result = asyncjobs.on_engine_start(_engine())
    finally:
        declare_pools()

    assert installed == [["alpha", "beta"]], (
        f"startup convergence must atomically install the pool union, got {installed}")
    assert edge_broadcasts == [], (
        "startup convergence is the node reconciling ITSELF — it must not "
        f"broadcast to the fleet, got {edge_broadcasts}")
    assert result == "completed:converged=alpha,beta", (
        f"unexpected hook result: {result!r}")


@th.django_unit_test("EDGE_CONVERGE_ENABLED=False disables startup converge")
def test_startup_converge_honors_gate(opts):
    """Deployments that install this app only for the fleet-deploy plane gate
    the sweep off; the startup converge must honor the same gate."""
    from mojo.apps.edge import asyncjobs

    installed = []
    with mock.patch("mojo.apps.edge.services.installer.install_pools",
                    side_effect=lambda pools: installed.append(list(pools))):
        result = with_setting(
            "EDGE_CONVERGE_ENABLED", False,
            lambda: asyncjobs.on_engine_start(_engine()))

    assert result == "disabled", (
        f"the gated startup converge must say it was disabled, got {result!r}")
    assert installed == [], (
        f"a disabled startup converge must not install anything, got {installed}")


@th.django_unit_test("a combined pool failure never claims a partial convergence")
def test_startup_converge_reports_combined_failure(opts):
    from mojo.apps.edge import asyncjobs

    declare_pools(["alpha", "beta"])
    attempted = []

    def install(pools):
        attempted.append(list(pools))
        raise RuntimeError("secret install detail")

    try:
        with mock.patch("mojo.apps.edge.services.installer.install_pools",
                        side_effect=install):
            result = asyncjobs.on_engine_start(_engine())
    finally:
        declare_pools()

    assert attempted == [["alpha", "beta"]], (
        f"startup did not attempt the exact combined assignment: {attempted}")
    assert result == "completed:converged=none", (
        f"an atomic combined failure reported a partially green pool: {result!r}")
