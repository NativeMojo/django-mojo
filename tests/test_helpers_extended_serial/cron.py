"""Real-module cron integration tests moved out of tests/test_helpers/cron.py.

These force fresh imports by popping entries out of sys.modules — a
process-global mutation every parallel module shares — so they run only in the
opt-in serial tier (maestro item #1839).
"""
from testit import helpers as th


@th.django_unit_test()
def test_load_app_cron_real_modules_no_errors(opts):
    """
    Regression: load_app_cron() must not raise when importing real cronjobs modules.

    The production incident: cronjobs.py used @schedule(days_of_week="0") but the
    decorator only accepts `weekdays`. Python raises TypeError at module import time.
    load_app_cron() only catches ImportError, so the TypeError propagated and crashed
    cron.py --run on startup.

    This test forces a fresh import of each known cronjobs module so the decorator
    args are re-evaluated even if the module was already cached.
    """
    import sys
    import importlib
    from mojo.decorators.cron import schedule

    known_modules = [
        "mojo.apps.incident.cronjobs",
        "mojo.apps.account.cronjobs",
        "mojo.apps.docit_kb.cronjobs",
        "mojo.apps.jobs.cronjobs",
        "mojo.apps.logit.cronjobs",
        "mojo.apps.shortlink.cronjobs",
    ]

    # Save existing registrations so we can restore after the test
    existing = list(getattr(schedule, 'scheduled_functions', []))

    for module_name in known_modules:
        sys.modules.pop(module_name, None)  # Force fresh import

    try:
        from mojo.helpers.cron import load_app_cron
        load_app_cron()
    except TypeError as exc:
        assert False, (
            f"load_app_cron() raised TypeError — a cronjobs module has an invalid "
            f"@schedule argument (e.g. 'days_of_week' instead of 'weekdays'): {exc}"
        )
    except Exception as exc:
        # ImportError is silently swallowed inside load_app_cron; anything else is a bug
        if not isinstance(exc, (ImportError, ModuleNotFoundError)):
            assert False, f"load_app_cron() raised unexpected {type(exc).__name__}: {exc}"

    # Restore: remove any duplicate registrations added by the fresh imports.
    # Also pop the freshly-imported modules from sys.modules so that other tests
    # that import them will trigger fresh decorator registration — leaving a module
    # in sys.modules while its functions are absent from scheduled_functions is an
    # inconsistent state that breaks parallel/serial test isolation.
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions[:] = existing
    for module_name in known_modules:
        sys.modules.pop(module_name, None)


@th.django_unit_test()
def test_incident_cronjobs_registered(opts):
    """After load_app_cron(), the incident cronjobs functions should be registered."""
    import sys
    from mojo.decorators.cron import schedule
    from mojo.helpers.cron import load_app_cron

    existing = list(getattr(schedule, 'scheduled_functions', []))

    sys.modules.pop("mojo.apps.incident.cronjobs", None)
    load_app_cron()

    registered_names = {spec['func'].__name__ for spec in getattr(schedule, 'scheduled_functions', [])}

    assert "sweep_expired_blocks" in registered_names, \
        f"sweep_expired_blocks should be registered; found: {registered_names}"
    assert "prune_events" in registered_names, \
        f"prune_events should be registered; found: {registered_names}"
    assert "refresh_ipsets" in registered_names, \
        f"refresh_ipsets should be registered; found: {registered_names}"
    assert "check_system_health" in registered_names, \
        f"check_system_health should be registered; found: {registered_names}"

    # Verify sweep_expired_blocks fires every 5 minutes
    sweep_spec = next(
        (s for s in schedule.scheduled_functions if s['func'].__name__ == "sweep_expired_blocks"),
        None
    )
    assert sweep_spec is not None, "sweep_expired_blocks spec not found"
    assert sweep_spec['minutes'] == '*/5', \
        f"sweep_expired_blocks should run every 5 minutes (minutes='*/5'), got {sweep_spec['minutes']!r}"

    # Verify refresh_ipsets is weekly (weekdays='0')
    refresh_spec = next(
        (s for s in schedule.scheduled_functions if s['func'].__name__ == "refresh_ipsets"),
        None
    )
    assert refresh_spec is not None, "refresh_ipsets spec not found"
    assert refresh_spec['weekdays'] == '0', \
        f"refresh_ipsets should run on weekday 0 (weekdays='0'), got {refresh_spec['weekdays']!r}"

    # Restore scheduled_functions and pop the freshly-imported module from
    # sys.modules so that subsequent tests that import it will trigger fresh
    # decorator registration, keeping sys.modules and scheduled_functions consistent.
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions[:] = existing
    sys.modules.pop("mojo.apps.incident.cronjobs", None)
