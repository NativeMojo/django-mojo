"""
The convergence-sweep gate (`EDGE_CONVERGE_ENABLED`).

Exists for deployments that install `mojo.apps.edge` only for the
fleet-deploy plane (django-mojo-skeleton): without the gate, cron discovery
schedules `converge_edge` and it broadcasts onto an `edge` channel none of
their runners consume, every 10 minutes, forever.

`get_static` reads live Django settings, so the override is a direct
`setattr` on `django.conf.settings` with a try/finally restore — the DM-015
pattern; `th.server_settings` would reload the wrong process.
"""
from unittest import mock

from testit import helpers as th

_SENTINEL = object()


def _with_setting(name, value, func):
    from django.conf import settings as dj_settings

    saved = getattr(dj_settings, name, _SENTINEL)
    setattr(dj_settings, name, value)
    try:
        return func()
    finally:
        if saved is _SENTINEL:
            delattr(dj_settings, name)
        else:
            setattr(dj_settings, name, saved)


@th.django_unit_test("converge_edge publishes by default (gate absent or True)")
def test_converge_enabled_by_default(opts):
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge import cronjobs

    calls = []
    with mock.patch.object(jobs_module, "publish",
                           side_effect=lambda **kw: calls.append(kw) or "job-1"):
        result = cronjobs.converge_edge()
    th.assert_eq(len(calls), 1,
                 f"the sweep must publish when the gate is unset, got {calls!r}")
    th.assert_true(calls[0].get("broadcast"),
                   "the sweep is a broadcast to every runner")
    th.assert_eq(result, "job-1", "the publish result must be returned")

    calls_true = []
    with mock.patch.object(jobs_module, "publish",
                           side_effect=lambda **kw: calls_true.append(kw) or "job-2"):
        _with_setting("EDGE_CONVERGE_ENABLED", True, cronjobs.converge_edge)
    th.assert_eq(len(calls_true), 1,
                 "an explicit True must behave exactly like the default")


@th.django_unit_test("EDGE_CONVERGE_ENABLED=False publishes nothing at all")
def test_converge_gate_disables(opts):
    import mojo.apps.jobs as jobs_module
    from mojo.apps.edge import cronjobs

    calls = []
    with mock.patch.object(jobs_module, "publish",
                           side_effect=lambda **kw: calls.append(kw)):
        result = _with_setting(
            "EDGE_CONVERGE_ENABLED", False, cronjobs.converge_edge)
    th.assert_eq(result, "disabled",
                 f"the gated sweep must say it was disabled, got {result!r}")
    th.assert_eq(calls, [],
                 "a disabled sweep must not publish anything — the whole point "
                 "is zero traffic onto an unconsumed channel")
