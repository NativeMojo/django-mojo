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

    triage_spec = next(
        (s for s in schedule.scheduled_functions if s['func'].__name__ == "triage_new_incidents"),
        None
    )
    assert triage_spec is not None, "triage_new_incidents spec not found"
    assert triage_spec['minutes'] == '0', \
        f"triage_new_incidents must run only at minute zero, got {triage_spec['minutes']!r}"
    assert triage_spec['hours'] == '9,18', \
        f"triage_new_incidents must run only at 09:00 and 18:00, got {triage_spec['hours']!r}"

    # Restore scheduled_functions and pop the freshly-imported module from
    # sys.modules so that subsequent tests that import it will trigger fresh
    # decorator registration, keeping sys.modules and scheduled_functions consistent.
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions[:] = existing
    sys.modules.pop("mojo.apps.incident.cronjobs", None)


@th.django_unit_test()
def test_incident_triage_gates_use_exact_policy_route(opts):
    from mojo.apps.incident import cronjobs
    from mojo.apps.incident.models.event import _autonomous_llm_enabled

    route = {
        "ready": True, "error_code": "", "provider": "anthropic",
        "credential": "admin", "model": "policy-model",
    }
    with patch(
            "mojo.apps.account.services.llm_safety.autonomous_triage_state",
            return_value=(True, None)), patch(
                "mojo.apps.account.services.llm_safety.route_state",
                return_value=route):
        assert cronjobs._llm_triage_enabled() is True, \
            "an enabled admin-routed triage feature must not require the handler key"
        assert _autonomous_llm_enabled() is True, \
            "event admission must accept the same exact ready route"

    route["ready"] = False
    route["error_code"] = "credential_missing"
    with patch(
            "mojo.apps.account.services.llm_safety.autonomous_triage_state",
            return_value=(True, None)), patch(
                "mojo.apps.account.services.llm_safety.route_state",
                return_value=route):
        assert cronjobs._llm_triage_enabled() is False, \
            "a present legacy key must not admit a policy route that is not ready"
        assert _autonomous_llm_enabled() is False, \
            "event admission must fail closed with the policy route"


@th.django_unit_test()
def test_incident_triage_sweep_publish_is_minute_idempotent(opts):
    from mojo.apps.incident import cronjobs

    scheduled_at = datetime.datetime(2026, 8, 31, 9, 0)
    with patch.object(cronjobs, "_llm_triage_enabled", return_value=True), \
            patch.object(cronjobs.jobs, "publish", return_value="job") as publish:
        cronjobs.triage_new_incidents(now=scheduled_at)
        cronjobs.triage_new_incidents(now=scheduled_at)

    keys = [call.kwargs.get("idempotency_key") for call in publish.call_args_list]
    assert len(keys) == 2, f"duplicate delivery should make two idempotent attempts, got {keys}"
    assert keys[0] == keys[1], \
        f"the same scheduled minute must use the same idempotency key, got {keys}"


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

            # Win every fleet-once claim explicitly. Against real Redis this
            # pinned 2024 minute is one claim bucket, so a second run inside
            # the 120s TTL would lose the race and skip.
            run_now(redis_client=_ScriptedRedis())

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
                # Win the claim explicitly — see test_run_now above.
                run_now(redis_client=_ScriptedRedis())
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


# ---------------------------------------------------------------------------
# Fleet-once cron (maestro item #2710)
#
# These swap the shared schedule.scheduled_functions registry, which is why
# they live in this serial tier rather than beside the claim-level tests in
# tests/test_helpers/cron.py.
# ---------------------------------------------------------------------------


class _ScriptedRedis:
    """A client whose SET result is scripted per call, or which raises."""

    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = []

    def set(self, key, value, nx=None, ex=None):
        self.calls.append(key)
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else True


@th.django_unit_test()
def test_decorator_registers_per_node_flag(opts):
    """Functions are fleet-once unless they opt out explicitly."""
    from mojo.decorators.cron import schedule

    existing = list(getattr(schedule, 'scheduled_functions', []))
    try:
        @schedule(minutes='0')
        def fleet_once_job():
            return "fleet"

        @schedule(minutes='0', per_node=True)
        def per_node_job():
            return "node"

        added = {spec['func'].__name__: spec
                 for spec in schedule.scheduled_functions[len(existing):]}
        assert added['fleet_once_job']['per_node'] is False, \
            "A function that says nothing must default to fleet-once"
        assert added['per_node_job']['per_node'] is True, \
            "per_node=True must be recorded on the spec so run_now can honor it"
    finally:
        schedule.scheduled_functions[:] = existing


@th.django_unit_test()
def test_run_now_runs_fleet_once_once_and_per_node_everywhere(opts):
    """The acceptance criterion: N nodes tick the same minute against one Redis.

    The fleet-once function must execute exactly once across all of them; the
    per_node function must execute on every one.
    """
    import threading
    import uuid as uuid_module
    from mojo.decorators.cron import schedule
    from mojo.helpers import cron
    from mojo.helpers.redis import get_connection

    node_count = 6
    # A fixed minute keeps the bucket deterministic; the uuid keeps this run's
    # claim keys distinct from any previous run still inside the 120s TTL.
    now = datetime.datetime(2026, 1, 2, 15, 4, 0)
    marker = uuid_module.uuid4().hex
    fleet_calls = []
    node_calls = []
    calls_lock = threading.Lock()

    existing = list(getattr(schedule, 'scheduled_functions', []))
    client = get_connection()
    try:
        schedule.scheduled_functions = []

        @schedule(minutes='4', hours='15')
        def fleet_once_task():
            with calls_lock:
                fleet_calls.append(1)

        @schedule(minutes='4', hours='15', per_node=True)
        def per_node_task():
            with calls_lock:
                node_calls.append(1)

        # Unique qualified names so the claim keys cannot collide across runs.
        fleet_once_task.__name__ = f"fleet_once_task_{marker}"
        per_node_task.__name__ = f"per_node_task_{marker}"
        keys = [
            f"{cron.CRON_FLEET_ONCE_PREFIX}:"
            f"{cron._qualified_name(spec['func'])}:{now.strftime('%Y%m%d%H%M')}"
            for spec in schedule.scheduled_functions
        ]
        for key in keys:
            client.delete(key)

        ready = threading.Barrier(node_count, timeout=15)

        def tick(index):
            ready.wait()
            cron.run_now(now=now, node_id=f"node-{index}")

        threads = [threading.Thread(target=tick, args=(i,))
                   for i in range(node_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(fleet_calls) == 1, (
            f"A fleet-once function must run exactly once across {node_count} "
            f"nodes ticking the same minute, ran {len(fleet_calls)} times")
        assert len(node_calls) == node_count, (
            f"A per_node function must run on every node, ran "
            f"{len(node_calls)} times across {node_count} nodes")
    finally:
        for key in keys:
            client.delete(key)
        schedule.scheduled_functions[:] = existing


@th.django_unit_test()
def test_run_now_reclaims_the_next_minute_after_a_winner_dies(opts):
    """A winner dying mid-run must not suppress the function in later minutes."""
    import uuid as uuid_module
    from mojo.decorators.cron import schedule
    from mojo.helpers import cron
    from mojo.helpers.redis import get_connection

    marker = uuid_module.uuid4().hex
    first = datetime.datetime(2026, 1, 2, 15, 4, 0)
    second = first + datetime.timedelta(minutes=1)
    attempts = []

    existing = list(getattr(schedule, 'scheduled_functions', []))
    client = get_connection()
    keys = []
    try:
        schedule.scheduled_functions = []

        @schedule(minutes='*')
        def dies_mid_run():
            attempts.append(1)
            raise RuntimeError("node died mid-run")

        dies_mid_run.__name__ = f"dies_mid_run_{marker}"
        name = cron._qualified_name(schedule.scheduled_functions[0]['func'])
        keys = [f"{cron.CRON_FLEET_ONCE_PREFIX}:{name}:{m.strftime('%Y%m%d%H%M')}"
                for m in (first, second)]
        for key in keys:
            client.delete(key)

        for moment in (first, second):
            try:
                cron.run_now(now=moment, node_id="node-a")
            except RuntimeError:
                pass
            else:
                assert False, "run_now must preserve the scheduled task exception"

        assert len(attempts) == 2, (
            "The next minute is a fresh claim bucket, so a crashed winner must "
            f"not suppress it — expected 2 attempts, got {len(attempts)}")
    finally:
        for key in keys:
            client.delete(key)
        schedule.scheduled_functions[:] = existing


@th.django_unit_test()
def test_run_now_runs_everything_when_redis_is_down(opts):
    """Single-node behavior must survive Redis being unreachable."""
    from mojo.decorators.cron import schedule
    from mojo.helpers import cron

    ran = []
    existing = list(getattr(schedule, 'scheduled_functions', []))
    try:
        schedule.scheduled_functions = []

        @schedule(minutes='*')
        def still_runs():
            ran.append(1)

        broken = _ScriptedRedis(error=RuntimeError("redis unavailable"))
        cron.run_now(now=datetime.datetime(2026, 1, 2, 15, 4, 0),
                     redis_client=broken, node_id="node-a")

        assert len(ran) == 1, (
            "A failed claim must fall back to running the function, not to "
            f"skipping it — ran {len(ran)} times")
    finally:
        schedule.scheduled_functions[:] = existing


@th.django_unit_test()
def test_run_now_heartbeat_separates_ran_from_skipped(opts):
    """Fleet observability must not read a skip as a failure."""
    from unittest.mock import patch as _patch
    from mojo.decorators.cron import schedule
    from mojo.helpers import cron

    existing = list(getattr(schedule, 'scheduled_functions', []))
    try:
        schedule.scheduled_functions = []

        @schedule(minutes='*')
        def loses_the_race():
            assert False, "A function whose claim was lost must not execute"

        # None is what redis-py returns when NX finds the key already present.
        lost = _ScriptedRedis(results=[None])
        with _patch('mojo.helpers.cron._write_heartbeat') as heartbeat:
            cron.run_now(now=datetime.datetime(2026, 1, 2, 15, 4, 0),
                         redis_client=lost, node_id="node-b")

        final = heartbeat.call_args_list[-1].args[1]
        assert final["state"] == "completed", \
            f"A run that only skipped is still a completed run, got {final['state']!r}"
        assert final["failed_count"] == 0, \
            "A skip is not a failure and must not be counted as one"
        assert final["skipped_count"] == 1, \
            f"The skip must be recorded, got skipped_count={final['skipped_count']}"
        assert final["executed"] == [], \
            f"Nothing ran on this node, got executed={final['executed']}"
        assert len(final["skipped"]) == 1 and "loses_the_race" in final["skipped"][0], \
            f"The skipped function must be named, got {final['skipped']}"
        assert final["node"] == "node-b", \
            f"The heartbeat must say which node reported it, got {final['node']!r}"
    finally:
        schedule.scheduled_functions[:] = existing
