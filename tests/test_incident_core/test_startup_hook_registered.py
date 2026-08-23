"""The incident app must register its boot-recovery hook (item #2716).

Default tier on purpose: this is a pure read of the process-global hook
registry, patching nothing. The registration it asserts on happened in THIS
process when Django loaded the app, which is the same thing a real runner
daemon does before JobEngine.start() fires the hooks.
"""
from testit import helpers as th

HOOK = "mojo.apps.incident.asyncjobs.on_engine_start"


@th.django_unit_test("the incident app registers its startup firewall recovery")
def test_incident_startup_hook_is_registered(opts):
    from mojo.apps import jobs

    hooks = jobs.get_startup_hooks()
    assert HOOK in hooks, (
        "the incident app did not register its boot-recovery hook — a "
        "restarted node would depend on a broadcast published while its "
        f"engine was down (registered: {hooks})")
