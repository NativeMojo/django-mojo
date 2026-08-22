"""Real-module cron integration tests moved out of tests/test_helpers/cron.py.

These force fresh imports by popping entries out of sys.modules — a
process-global mutation every parallel module shares — so they run only in the
opt-in serial tier (maestro item #1839).

The scheduler-execution tests further down (find/run/load) moved here too
(maestro item #2558): they mock.patch mojo.helpers.cron module attributes
(datetime, apps.get_app_configs, importlib.import_module, _write_heartbeat)
and swap the shared schedule.scheduled_functions registry — both process-global.
"""
import datetime
from unittest.mock import patch, MagicMock

from testit import helpers as th


# Monday, Jan 15, 2024, 11:50:30 — the fixed "now" the scheduler tests pin.
TEST_TIME = datetime.datetime(2024, 1, 15, 11, 50, 30)


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


# ---------------------------------------------------------------------------
# Scheduler execution tests moved from tests/test_helpers/cron.py
# (maestro item #2558): they patch mojo.helpers.cron module attributes and
# swap the shared schedule.scheduled_functions registry — process-global on
# both counts, so they run only in this opt-in serial tier.
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_find_scheduled_functions(opts):
    """Test finding scheduled functions (moved from tests/test_helpers/cron.py,
    item #2558 — patches mojo.helpers.cron.datetime and swaps the shared
    schedule registry)."""
    from mojo.decorators.cron import schedule
    from mojo.helpers.cron import find_scheduled_functions

    # Save existing state so we can restore it after the test
    existing = list(getattr(schedule, 'scheduled_functions', []))

    # Clear any existing scheduled functions
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions.clear()
    else:
        schedule.scheduled_functions = []

    try:
        # Define test functions with different schedules
        @schedule(minutes='50', hours='11')
        def test_func1():
            return "test1"

        @schedule(minutes='*')
        def test_func2():
            return "test2"

        @schedule(minutes='30', hours='9')
        def test_func3():
            return "test3"

        # Mock datetime.now to return our test time (11:50)
        with patch('mojo.helpers.cron.datetime') as mock_datetime:
            mock_datetime.datetime.now.return_value = TEST_TIME

            # Find functions that should run at 11:50
            funcs = find_scheduled_functions()

            # test_func1 should match (11:50)
            # test_func2 should match (every minute)
            # test_func3 should not match (9:30)
            assert len(funcs) == 2, f"Expected 2 functions to match at 11:50, got {len(funcs)}"
            assert test_func1 in funcs, "test_func1 (11:50) should be in matched functions"
            assert test_func2 in funcs, "test_func2 (every minute) should be in matched functions"
            assert test_func3 not in funcs, "test_func3 (9:30) should not be in matched functions"
    finally:
        # Restore original registered functions so other tests are not affected
        if hasattr(schedule, 'scheduled_functions'):
            schedule.scheduled_functions[:] = existing
        else:
            schedule.scheduled_functions = existing


@th.django_unit_test()
def test_load_app_cron(opts):
    """Test loading cronjobs from apps (moved from tests/test_helpers/cron.py,
    item #2558 — patches mojo.helpers.cron.apps.get_app_configs and
    mojo.helpers.cron.importlib.import_module)."""
    from mojo.helpers.cron import load_app_cron

    # Mock Django apps
    mock_app1 = MagicMock()
    mock_app1.name = 'testapp1'

    mock_app2 = MagicMock()
    mock_app2.name = 'testapp2'

    mock_app3 = MagicMock()
    mock_app3.name = 'testapp3'

    with patch('mojo.helpers.cron.apps.get_app_configs') as mock_get_configs:
        mock_get_configs.return_value = [mock_app1, mock_app2, mock_app3]

        with patch('mojo.helpers.cron.importlib.import_module') as mock_import:
            # Simulate testapp1 has cronjobs, testapp2 doesn't, testapp3 has cronjobs
            def import_side_effect(module_name):
                if module_name == 'testapp1.cronjobs':
                    return MagicMock()  # Module exists
                elif module_name == 'testapp2.cronjobs':
                    raise ImportError()  # Module doesn't exist
                elif module_name == 'testapp3.cronjobs':
                    return MagicMock()  # Module exists
                else:
                    raise ImportError()

            mock_import.side_effect = import_side_effect

            # Load app cron jobs
            load_app_cron()

            # Verify it tried to import all three
            assert mock_import.call_count == 3, f"Should have tried to import 3 cronjobs modules, got {mock_import.call_count}"
            mock_import.assert_any_call('testapp1.cronjobs')
            mock_import.assert_any_call('testapp2.cronjobs')
            mock_import.assert_any_call('testapp3.cronjobs')


@th.django_unit_test()
def test_run_now(opts):
    """Test the run_now function (moved from tests/test_helpers/cron.py,
    item #2558 — patches mojo.helpers.cron.datetime and _write_heartbeat and
    swaps the shared schedule registry)."""
    from mojo.helpers.cron import run_now
    from mojo.decorators.cron import schedule

    # Save existing state so we can restore it after the test
    existing = list(getattr(schedule, 'scheduled_functions', []))

    # Clear existing functions
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions.clear()
    else:
        schedule.scheduled_functions = []

    # Track function executions
    executed = []

    try:
        @schedule(minutes='*')  # Runs every minute
        def always_run():
            executed.append('always')

        @schedule(minutes='50', hours='11')  # Runs at 11:50
        def specific_time():
            executed.append('specific')

        @schedule(minutes='30', hours='9')  # Runs at 9:30
        def other_time():
            executed.append('other')

        # Mock datetime to return 11:50
        with patch('mojo.helpers.cron.datetime') as mock_datetime, \
             patch('mojo.helpers.cron._write_heartbeat') as heartbeat:
            mock_datetime.datetime.now.return_value = TEST_TIME
            mock_datetime.timezone = datetime.timezone

            # Run scheduled functions
            run_now()

            # Check which functions executed
            assert 'always' in executed, "always_run should have executed"
            assert 'specific' in executed, "specific_time should have executed"
            assert 'other' not in executed, "other_time should not have executed"
            assert len(executed) == 2, f"Expected 2 functions to execute, got {len(executed)}: {executed}"
            states = [call.args[1]["state"] for call in heartbeat.call_args_list]
            assert states == ["started", "completed"], f"Expected start/completion heartbeats, got {states}"
    finally:
        # Restore original registered functions so other tests are not affected
        if hasattr(schedule, 'scheduled_functions'):
            schedule.scheduled_functions[:] = existing
        else:
            schedule.scheduled_functions = existing


@th.django_unit_test()
def test_run_now_records_failure_and_reraises(opts):
    """A task failure is observable without changing run_now's exception
    contract (moved from tests/test_helpers/cron.py, item #2558 — patches
    mojo.helpers.cron._write_heartbeat and swaps the shared schedule
    registry)."""
    from mojo.helpers.cron import run_now
    from mojo.decorators.cron import schedule

    existing = list(getattr(schedule, 'scheduled_functions', []))
    schedule.scheduled_functions = []
    try:
        @schedule(minutes='*')
        def broken():
            raise RuntimeError("expected")

        with patch('mojo.helpers.cron._write_heartbeat') as heartbeat:
            try:
                run_now()
            except RuntimeError:
                pass
            else:
                assert False, "run_now must preserve the scheduled task exception"
        states = [call.args[1]["state"] for call in heartbeat.call_args_list]
        assert states == ["started", "failed"], f"Expected a failed completion heartbeat, got {states}"
        failure = heartbeat.call_args_list[-1].args[1]
        assert failure["failure"] == "RuntimeError", "Heartbeat must record only the exception class"
    finally:
        schedule.scheduled_functions[:] = existing
