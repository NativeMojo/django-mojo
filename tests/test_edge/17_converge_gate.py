"""
The convergence-sweep gate (`EDGE_CONVERGE_ENABLED`).

Exists for deployments that install `mojo.apps.edge` only for the
fleet-deploy plane (django-mojo-skeleton): without the gate, cron discovery
schedules `converge_edge` and it broadcasts onto an `edge` channel none of
their runners consume, every 10 minutes, forever.

`get_static` reads live Django settings, so the override is a direct
`setattr` on `django.conf.settings` with a try/finally restore — the DM-015
pattern (`_helpers.with_setting`); `th.server_settings` would reload the
wrong process.

Publishes are captured with `th.capture_publishes` scoped to the converge
job: test modules run as parallel threads, and a plain publish mock here
would swallow other modules' real publishes mid-window.
"""
from testit import helpers as th

from tests.test_edge._helpers import with_setting


def _converge_publish(call):
    from mojo.apps.edge import cronjobs

    return call.get("func") == cronjobs.CONVERGE_JOB


@th.django_unit_test("converge_edge publishes by default (gate absent or True)")
def test_converge_enabled_by_default(opts):
    from mojo.apps.edge import cronjobs

    with th.capture_publishes(_converge_publish) as calls:
        result = cronjobs.converge_edge()
    th.assert_eq(len(calls), 1,
                 f"the sweep must publish when the gate is unset, got {calls!r}")
    th.assert_true(calls[0].get("broadcast"),
                   "the sweep is a broadcast to every runner")
    th.assert_eq(result, "fake-job-1", "the publish result must be returned")

    with th.capture_publishes(_converge_publish) as calls_true:
        with_setting("EDGE_CONVERGE_ENABLED", True, cronjobs.converge_edge)
    th.assert_eq(len(calls_true), 1,
                 "an explicit True must behave exactly like the default")


@th.django_unit_test("EDGE_CONVERGE_ENABLED=False publishes nothing at all")
def test_converge_gate_disables(opts):
    from mojo.apps.edge import cronjobs

    with th.capture_publishes(_converge_publish) as calls:
        result = with_setting(
            "EDGE_CONVERGE_ENABLED", False, cronjobs.converge_edge)
    th.assert_eq(result, "disabled",
                 f"the gated sweep must say it was disabled, got {result!r}")
    th.assert_eq(calls, [],
                 "a disabled sweep must not publish anything — the whole point "
                 "is zero traffic onto an unconsumed channel")
