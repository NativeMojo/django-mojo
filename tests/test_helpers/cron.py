from testit import helpers as th
import datetime
from unittest import mock

@th.unit_setup()
def setup_cron_tests(opts):
    """Setup test data for cron tests"""
    opts.test_time = datetime.datetime(2024, 1, 15, 11, 50, 30)  # Monday, Jan 15, 2024, 11:50:30
    opts.test_functions = []


# ============================================================================
# Basic Pattern Matching Tests (Pure Python - No Django Required)
# ============================================================================

@th.django_unit_test()
def test_matches_wildcard(opts):
    """Test wildcard pattern matching"""
    from mojo.helpers.cron import matches

    # Wildcard should match any value
    assert matches('*', 0) == True, "Wildcard should match 0"
    assert matches('*', 30) == True, "Wildcard should match 30"
    assert matches('*', 59) == True, "Wildcard should match 59"
    assert matches('*', 100) == True, "Wildcard should match any value"


@th.django_unit_test()
def test_matches_single_value(opts):
    """Test single value pattern matching"""
    from mojo.helpers.cron import matches

    # Single value should match exactly
    assert matches('5', 5) == True, "Should match exact value 5"
    assert matches('5', 4) == False, "Should not match 4 when expecting 5"
    assert matches('5', 6) == False, "Should not match 6 when expecting 5"
    assert matches('30', 30) == True, "Should match exact value 30"
    assert matches('0', 0) == True, "Should match exact value 0"


@th.django_unit_test()
def test_matches_comma_separated(opts):
    """Test comma-separated value pattern matching"""
    from mojo.helpers.cron import matches

    # Comma-separated values
    assert matches('0,15,30,45', 0) == True, "Should match 0 in list"
    assert matches('0,15,30,45', 15) == True, "Should match 15 in list"
    assert matches('0,15,30,45', 30) == True, "Should match 30 in list"
    assert matches('0,15,30,45', 45) == True, "Should match 45 in list"
    assert matches('0,15,30,45', 10) == False, "Should not match 10 not in list"
    assert matches('0,15,30,45', 60) == False, "Should not match 60 not in list"

    # With spaces
    assert matches('0, 15, 30, 45', 15) == True, "Should handle spaces in comma-separated list"


@th.django_unit_test()
def test_matches_ranges(opts):
    """Test range pattern matching"""
    from mojo.helpers.cron import matches

    # Basic ranges
    assert matches('0-5', 0) == True, "Should match start of range"
    assert matches('0-5', 3) == True, "Should match middle of range"
    assert matches('0-5', 5) == True, "Should match end of range"
    assert matches('0-5', 6) == False, "Should not match outside range"
    assert matches('0-5', -1) == False, "Should not match negative outside range"

    # Larger ranges
    assert matches('10-20', 10) == True, "Should match start of larger range"
    assert matches('10-20', 15) == True, "Should match middle of larger range"
    assert matches('10-20', 20) == True, "Should match end of larger range"
    assert matches('10-20', 9) == False, "Should not match before range"
    assert matches('10-20', 21) == False, "Should not match after range"


@th.django_unit_test()
def test_matches_steps(opts):
    """Test step pattern matching (*/n)"""
    from mojo.helpers.cron import matches

    # Every 5 minutes
    assert matches('*/5', 0) == True, "*/5 should match 0"
    assert matches('*/5', 5) == True, "*/5 should match 5"
    assert matches('*/5', 10) == True, "*/5 should match 10"
    assert matches('*/5', 15) == True, "*/5 should match 15"
    assert matches('*/5', 30) == True, "*/5 should match 30"
    assert matches('*/5', 55) == True, "*/5 should match 55"
    assert matches('*/5', 1) == False, "*/5 should not match 1"
    assert matches('*/5', 7) == False, "*/5 should not match 7"
    assert matches('*/5', 59) == False, "*/5 should not match 59"

    # Every 15 minutes
    assert matches('*/15', 0) == True, "*/15 should match 0"
    assert matches('*/15', 15) == True, "*/15 should match 15"
    assert matches('*/15', 30) == True, "*/15 should match 30"
    assert matches('*/15', 45) == True, "*/15 should match 45"
    assert matches('*/15', 10) == False, "*/15 should not match 10"
    assert matches('*/15', 50) == False, "*/15 should not match 50"

    # Every minute (*/1)
    assert matches('*/1', 0) == True, "*/1 should match any minute"
    assert matches('*/1', 30) == True, "*/1 should match any minute"
    assert matches('*/1', 59) == True, "*/1 should match any minute"


@th.django_unit_test()
def test_matches_range_with_steps(opts):
    """Test range with step pattern matching (start-end/step)"""
    from mojo.helpers.cron import matches

    # 10-50/5 means every 5 between 10 and 50
    assert matches('10-50/5', 10) == True, "10-50/5 should match 10"
    assert matches('10-50/5', 15) == True, "10-50/5 should match 15"
    assert matches('10-50/5', 20) == True, "10-50/5 should match 20"
    assert matches('10-50/5', 25) == True, "10-50/5 should match 25"
    assert matches('10-50/5', 30) == True, "10-50/5 should match 30"
    assert matches('10-50/5', 50) == True, "10-50/5 should match 50"
    assert matches('10-50/5', 5) == False, "10-50/5 should not match 5 (before range)"
    assert matches('10-50/5', 11) == False, "10-50/5 should not match 11 (not on step)"
    assert matches('10-50/5', 51) == False, "10-50/5 should not match 51 (after range)"
    assert matches('10-50/5', 55) == False, "10-50/5 should not match 55 (outside range)"

    # 0-30/10
    assert matches('0-30/10', 0) == True, "0-30/10 should match 0"
    assert matches('0-30/10', 10) == True, "0-30/10 should match 10"
    assert matches('0-30/10', 20) == True, "0-30/10 should match 20"
    assert matches('0-30/10', 30) == True, "0-30/10 should match 30"
    assert matches('0-30/10', 5) == False, "0-30/10 should not match 5"
    assert matches('0-30/10', 40) == False, "0-30/10 should not match 40"


@th.django_unit_test()
def test_matches_complex_patterns(opts):
    """Test complex combined patterns"""
    from mojo.helpers.cron import matches

    # Combination of patterns
    assert matches('0-5,10,15,20-25', 0) == True, "Complex pattern should match 0"
    assert matches('0-5,10,15,20-25', 3) == True, "Complex pattern should match 3"
    assert matches('0-5,10,15,20-25', 5) == True, "Complex pattern should match 5"
    assert matches('0-5,10,15,20-25', 10) == True, "Complex pattern should match 10"
    assert matches('0-5,10,15,20-25', 15) == True, "Complex pattern should match 15"
    assert matches('0-5,10,15,20-25', 22) == True, "Complex pattern should match 22"
    assert matches('0-5,10,15,20-25', 25) == True, "Complex pattern should match 25"
    assert matches('0-5,10,15,20-25', 7) == False, "Complex pattern should not match 7"
    assert matches('0-5,10,15,20-25', 18) == False, "Complex pattern should not match 18"
    assert matches('0-5,10,15,20-25', 30) == False, "Complex pattern should not match 30"

    # With steps
    assert matches('*/15,5,10', 0) == True, "Pattern with step should match 0"
    assert matches('*/15,5,10', 5) == True, "Pattern with step should match 5"
    assert matches('*/15,5,10', 10) == True, "Pattern with step should match 10"
    assert matches('*/15,5,10', 15) == True, "Pattern with step should match 15"
    assert matches('*/15,5,10', 30) == True, "Pattern with step should match 30"
    assert matches('*/15,5,10', 45) == True, "Pattern with step should match 45"
    assert matches('*/15,5,10', 7) == False, "Pattern with step should not match 7"


@th.django_unit_test()
def test_matches_invalid_patterns(opts):
    """Test handling of invalid patterns"""
    from mojo.helpers.cron import matches

    # Invalid patterns should return False
    assert matches('invalid', 5) == False, "Invalid pattern should not match"
    assert matches('abc', 10) == False, "Non-numeric pattern should not match"
    assert matches('5-', 5) == False, "Incomplete range should not match"
    assert matches('-10', 5) == False, "Invalid range should not match"
    assert matches('10-5', 7) == False, "Backwards range should not match"
    assert matches('*/', 0) == False, "Step without value should not match"
    assert matches('10-20/abc', 15) == False, "Step with non-numeric should not match"


@th.django_unit_test()
def test_match_time(opts):
    """Test the match_time function"""
    from mojo.helpers.cron import match_time

    # Test time: Monday, Jan 15, 2024, 11:50:30
    test_time = opts.test_time

    # Should match: every minute
    cron_spec = {
        'minutes': '*',
        'hours': '*',
        'days': '*',
        'months': '*',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == True, "Should match wildcard spec"

    # Should match: specific minute
    cron_spec = {
        'minutes': '50',
        'hours': '*',
        'days': '*',
        'months': '*',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == True, "Should match minute 50"

    # Should not match: different minute
    cron_spec = {
        'minutes': '45',
        'hours': '*',
        'days': '*',
        'months': '*',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == False, "Should not match minute 45"

    # Should match: every 5 minutes at 50
    cron_spec = {
        'minutes': '*/5',
        'hours': '*',
        'days': '*',
        'months': '*',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == True, "Should match */5 at minute 50"

    # Should match: specific hour and minute
    cron_spec = {
        'minutes': '50',
        'hours': '11',
        'days': '*',
        'months': '*',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == True, "Should match 11:50"

    # Should not match: different hour
    cron_spec = {
        'minutes': '50',
        'hours': '9',
        'days': '*',
        'months': '*',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == False, "Should not match 9:50"

    # Should match: Monday (weekday 0)
    cron_spec = {
        'minutes': '*',
        'hours': '*',
        'days': '*',
        'months': '*',
        'weekdays': '0'
    }
    assert match_time(test_time, cron_spec) == True, "Should match Monday"

    # Should match: 15th day of month
    cron_spec = {
        'minutes': '*',
        'hours': '*',
        'days': '15',
        'months': '*',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == True, "Should match 15th day"

    # Should match: January (month 1)
    cron_spec = {
        'minutes': '*',
        'hours': '*',
        'days': '*',
        'months': '1',
        'weekdays': '*'
    }
    assert match_time(test_time, cron_spec) == True, "Should match January"


@th.django_unit_test()
def test_cronjobs_example(opts):
    """Test the specific examples from cronjobs.py"""
    from mojo.helpers.cron import match_time

    # Test prune_events schedule (9:45 daily)
    prune_spec = {
        'minutes': '45',
        'hours': '9',
        'days': '*',
        'months': '*',
        'weekdays': '*',
        'func': lambda: None
    }

    # Should match at 9:45
    time_945 = datetime.datetime(2024, 1, 15, 9, 45, 0)
    assert match_time(time_945, prune_spec) == True, "prune_events should fire at 9:45"

    # Should not match at 11:50
    time_1150 = datetime.datetime(2024, 1, 15, 11, 50, 0)
    assert match_time(time_1150, prune_spec) == False, "prune_events should not fire at 11:50"

    # Test run_example schedule (every minute)
    example_spec = {
        'minutes': '*',
        'hours': '*',
        'days': '*',
        'months': '*',
        'weekdays': '*',
        'func': lambda: None
    }

    # Should match at any time
    assert match_time(time_945, example_spec) == True, "run_example should fire at 9:45"
    assert match_time(time_1150, example_spec) == True, "run_example should fire at 11:50"

    # Should match every single minute
    for minute in range(60):
        test_time = datetime.datetime(2024, 1, 15, 11, minute, 0)
        assert match_time(test_time, example_spec) == True, f"run_example should fire at minute {minute}"


@th.django_unit_test()
def test_every_5_minutes_schedule(opts):
    """Test that */5 pattern works correctly for 'every 5 minutes'"""
    from mojo.helpers.cron import match_time

    spec_every_5 = {
        'minutes': '*/5',
        'hours': '*',
        'days': '*',
        'months': '*',
        'weekdays': '*',
        'func': lambda: None
    }

    # Should match at minutes: 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55
    matching_minutes = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
    for minute in matching_minutes:
        test_time = datetime.datetime(2024, 1, 15, 11, minute, 0)
        assert match_time(test_time, spec_every_5) == True, f"*/5 should match minute {minute}"

    # Should not match at other minutes
    non_matching_minutes = [1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 14, 16, 17, 18, 19, 21, 51, 52, 53, 54, 56, 57, 58, 59]
    for minute in non_matching_minutes:
        test_time = datetime.datetime(2024, 1, 15, 11, minute, 0)
        assert match_time(test_time, spec_every_5) == False, f"*/5 should not match minute {minute}"


# ============================================================================
# Django-Dependent Tests (Require Django Context)
# ============================================================================

# test_find_scheduled_functions, test_load_app_cron, test_run_now and
# test_run_now_records_failure_and_reraises moved to
# tests/test_helpers_extended_serial/cron.py (maestro item #2558): they
# mock.patch mojo.helpers.cron module attributes (datetime,
# apps.get_app_configs, importlib.import_module, _write_heartbeat) and swap
# the shared schedule.scheduled_functions registry — process-global, unsafe
# under the parallel default tier.


@th.django_unit_test()
def test_cron_heartbeat_records_are_isolated_by_run(opts):
    """Overlapping completions cannot overwrite a newer run's state."""
    from mojo.helpers import cron

    client = mock.Mock()
    pipe = client.pipeline.return_value
    cron._write_heartbeat("older", {"run_id": "older", "state": "completed"}, 1, client)
    cron._write_heartbeat("newer", {"run_id": "newer", "state": "started"}, 2, client)
    keys = [call.args[0] for call in pipe.set.call_args_list]
    assert keys == [cron._heartbeat_key("older"), cron._heartbeat_key("newer")], \
        f"Each run needs an isolated heartbeat key, got {keys}"
    assert pipe.expire.call_count == 2, "The heartbeat index must also expire"


@th.django_unit_test()
def test_cron_heartbeat_write_is_fail_open(opts):
    """Redis failure must never prevent scheduled work from running."""
    from mojo.helpers import cron

    client = mock.Mock()
    client.pipeline.side_effect = RuntimeError("redis unavailable")
    cron._write_heartbeat("run", {"run_id": "run"}, 1, client)
    assert client.pipeline.call_count == 1, "The failed Redis write should be attempted once"


@th.django_unit_test()
def test_decorator_registration(opts):
    """Test that the @schedule decorator properly registers functions"""
    from mojo.decorators.cron import schedule

    # Save existing state so we can restore it after the test
    existing = list(getattr(schedule, 'scheduled_functions', []))

    # Clear any existing functions
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions.clear()
    else:
        schedule.scheduled_functions = []

    try:
        # Define functions with decorator
        @schedule(minutes='0,30', hours='*')
        def half_hourly():
            return "half_hourly"

        @schedule(minutes='0', hours='0', days='1', months='*', weekdays='*')
        def monthly():
            return "monthly"

        # Check that functions were registered
        assert hasattr(schedule, 'scheduled_functions'), "schedule should have scheduled_functions attribute"
        assert len(schedule.scheduled_functions) == 2, f"Should have 2 scheduled functions, got {len(schedule.scheduled_functions)}"

        # Check first function registration
        func1 = schedule.scheduled_functions[0]
        assert func1['func'] == half_hourly, "First function should be half_hourly"
        assert func1['minutes'] == '0,30', "First function minutes should be '0,30'"
        assert func1['hours'] == '*', "First function hours should be '*'"

        # Check second function registration
        func2 = schedule.scheduled_functions[1]
        assert func2['func'] == monthly, "Second function should be monthly"
        assert func2['minutes'] == '0', "Second function minutes should be '0'"
        assert func2['hours'] == '0', "Second function hours should be '0'"
        assert func2['days'] == '1', "Second function days should be '1'"
    finally:
        # Restore original registered functions so parallel tests are not affected
        if hasattr(schedule, 'scheduled_functions'):
            schedule.scheduled_functions[:] = existing
        else:
            schedule.scheduled_functions = existing


# ============================================================================
# Omitted finer fields resolve to "0", not "*" (maestro item #9773)
# ============================================================================
# `resolve_fields` is pure, so the resolution matrix never touches the shared
# registry; the one stored-spec test adds its own entry and removes only that
# entry by identity. The warning test passes its own specs, `warn=` recorder
# and `warned=` set — nothing under mojo.helpers is patched.


def _all_star(**overrides):
    spec = {'minutes': '*', 'hours': '*', 'days': '*', 'months': '*', 'weekdays': '*'}
    spec.update(overrides)
    return spec


@th.django_unit_test()
def test_schedule_resolves_omitted_minutes_to_zero(opts):
    """@schedule(hours="3") means 03:00 once, not every minute of hour 3."""
    from mojo.decorators.cron import schedule, resolve_fields
    from mojo.helpers.cron import match_time

    resolved = resolve_fields(hours="3")
    assert resolved == _all_star(minutes='0', hours='3'), \
        f"hours given and minutes omitted must resolve minutes to '0', got {resolved}"
    assert match_time(datetime.datetime(2024, 1, 15, 3, 0), resolved), \
        "The resolved schedule must still fire at 03:00"
    assert not match_time(datetime.datetime(2024, 1, 15, 3, 1), resolved), \
        "The resolved schedule must not fire at 03:01 — that was the production misfire"

    # Decorating appends to the shared registry, which other modules in this
    # process read concurrently: find our entry by identity and remove only
    # it — never [-1], never a snapshot restore (that would drop a cronjobs
    # module another thread registered meanwhile), never attribute reassignment
    # (testit/isolation.py counts that as a cold site).
    @schedule(hours="3")
    def nightly():
        return "nightly"

    registry = schedule.scheduled_functions
    try:
        stored = [spec for spec in registry if spec['func'] is nightly]
        assert len(stored) == 1, f"The decorator must register the function once, got {stored}"
        assert stored[0]['minutes'] == '0' and stored[0]['hours'] == '3', \
            f"The registry must hold the resolved spec so run_now sees it, got {stored[0]}"
        assert stored[0]['per_node'] is False, "per_node must keep its default"
    finally:
        registry[:] = [spec for spec in registry if spec['func'] is not nightly]


@th.django_unit_test()
def test_schedule_resolves_omitted_hours_and_minutes_to_zero(opts):
    """A day, month or weekday with no hour means 00:00 on that day."""
    from mojo.decorators.cron import resolve_fields

    assert resolve_fields(days="1") == _all_star(minutes='0', hours='0', days='1'), \
        f"days given must resolve hours and minutes to '0', got {resolve_fields(days='1')}"
    assert resolve_fields(months="6") == _all_star(minutes='0', hours='0', months='6'), \
        f"months given must resolve hours and minutes to '0', got {resolve_fields(months='6')}"
    assert resolve_fields(weekdays="0") == _all_star(minutes='0', hours='0', weekdays='0'), \
        f"weekdays given must resolve hours and minutes to '0', got {resolve_fields(weekdays='0')}"
    assert resolve_fields(weekdays="0", hours="3") == _all_star(minutes='0', hours='3', weekdays='0'), \
        "An explicit hour beside a weekday is kept and only minutes collapses"


@th.django_unit_test()
def test_schedule_explicit_values_win(opts):
    """Anything written explicitly stays exactly as written, including '*'."""
    from mojo.decorators.cron import resolve_fields

    assert resolve_fields(minutes="*/5", hours="3") == _all_star(minutes='*/5', hours='3'), \
        "An explicit step pattern must never be rewritten"
    assert resolve_fields(minutes="*", hours="3") == _all_star(minutes='*', hours='3'), \
        "An explicit minutes='*' beside an hour must be honoured — explicit wins"
    assert resolve_fields(minutes="0,30") == _all_star(minutes='0,30'), \
        "A lone explicit minutes value stays as written"
    assert resolve_fields(hours="*") == _all_star(), \
        "hours='*' names no hour, so nothing collapses: still every minute"
    assert resolve_fields(minutes="*/10", days="*") == _all_star(minutes='*/10'), \
        "days='*' names no day, so the omitted hour stays '*'"
    assert resolve_fields(days="1", hours="*") == _all_star(minutes='0', hours='*', days='1'), \
        "An explicit hours='*' beside a named day is kept while omitted minutes still collapses"
    assert resolve_fields(minutes="*/5", weekdays="5,6") == _all_star(minutes='*/5', hours='0', weekdays='5,6'), \
        "A named weekday collapses the omitted hour even beside explicit minutes (the warning names it)"


@th.django_unit_test()
def test_schedule_bare_is_every_minute(opts):
    """@schedule() with nothing given is unchanged: every minute."""
    from mojo.decorators.cron import resolve_fields

    assert resolve_fields() == _all_star(), \
        f"Nothing given must stay every-minute, got {resolve_fields()}"


@th.django_unit_test()
def test_warn_overfiring_schedules_names_offenders_once(opts):
    """Loading warns, once per function, about schedules that name an hour or
    day yet still fire more than once per hour — and never refuses them."""
    from mojo.helpers import cron

    def every_minute_of_three():
        pass

    def every_five_in_nine():
        pass

    def nightly():
        pass

    def every_minute():
        pass

    def every_five():
        pass

    specs = [
        dict(_all_star(minutes='*', hours='3'), func=every_minute_of_three, per_node=False),
        dict(_all_star(minutes='*/5', hours='9'), func=every_five_in_nine, per_node=False),
        dict(_all_star(minutes='0', hours='3'), func=nightly, per_node=False),
        dict(_all_star(), func=every_minute, per_node=False),
        dict(_all_star(minutes='*/5'), func=every_five, per_node=False),
    ]
    lines = []
    warned = set()

    offenders = cron.warn_overfiring_schedules(specs, warn=lines.append, warned=warned)

    expected = [cron._qualified_name(every_minute_of_three),
                cron._qualified_name(every_five_in_nine)]
    assert offenders == expected, \
        f"Only the two schedules naming an hour with many minutes offend, got {offenders}"
    assert len(lines) == 2, f"One warning line per offender, got {lines}"
    assert expected[0] in lines[0] and "60 times an hour" in lines[0] \
        and "minutes=* hours=3" in lines[0], \
        f"The line must name the function, the rate and the resolved spec, got {lines[0]!r}"
    assert expected[1] in lines[1] and "12 times an hour" in lines[1], \
        f"A */5 beside an hour fires 12 times an hour, got {lines[1]!r}"
    assert not any(f"{cron._qualified_name(f)} names" in line
                   for f in (nightly, every_minute, every_five) for line in lines), \
        f"Clean schedules (one minute, or no coarser field) must not be named, got {lines}"

    again = cron.warn_overfiring_schedules(specs, warn=lines.append, warned=warned)
    assert again == [] and len(lines) == 2, \
        "A repeat load in the same process must not repeat the warning"


@th.django_unit_test()
def test_warn_overfiring_schedules_never_raises(opts):
    """A malformed minutes value fails on its own tick as before; the guard
    logs it and must never be what stops load_app_cron."""
    from mojo.helpers import cron

    def divide_by_zero():
        pass

    def nightly():
        pass

    specs = [
        dict(_all_star(minutes='*/0', hours='3'), func=divide_by_zero, per_node=False),
        dict(_all_star(minutes='*', hours='3'), func=nightly, per_node=False),
    ]
    lines = []

    offenders = cron.warn_overfiring_schedules(specs, warn=lines.append, warned=set())

    assert offenders == [cron._qualified_name(nightly)], \
        f"The schedule after the malformed one must still be checked, got {offenders}"
    assert len(lines) == 2 and "could not evaluate" in lines[0] \
        and cron._qualified_name(divide_by_zero) in lines[0], \
        f"The malformed schedule must be logged by name, not raised, got {lines}"


# ============================================================================
# Real-module integration tests — catch import-time decorator bugs
# ============================================================================
# test_load_app_cron_real_modules_no_errors and test_incident_cronjobs_registered
# moved to tests/test_helpers_extended_serial/cron.py: they pop entries out of
# sys.modules, a process-global mutation that is unsafe under the parallel
# default tier (maestro item #1839).


# ============================================================================
# Fleet-once claim (maestro item #2710)
# ============================================================================
# These inject a client through claim_fleet_once's redis_client= seam rather
# than patching anything under mojo.helpers — the default tier forbids
# process-global mutation, and the seam exists so it is never needed.


class _StubRedis:
    """Records SET calls and returns a scripted result."""

    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def set(self, key, value, nx=None, ex=None):
        self.calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if self.error is not None:
            raise self.error
        return self.result


@th.django_unit_test()
def test_claim_fleet_once_wins_and_shapes_the_key(opts):
    """A won claim runs the function, and the key is per-function per-minute."""
    from mojo.helpers import cron

    client = _StubRedis(result=True)
    won = cron.claim_fleet_once("pkg.mod.func", "202601021504",
                                node_id="node-a", redis_client=client)

    assert won is True, "A successful SET NX must let this node run the function"
    assert len(client.calls) == 1, \
        f"Expected exactly one SET for one claim, got {len(client.calls)}"
    call = client.calls[0]
    assert call["key"] == "mojo:cron:once:pkg.mod.func:202601021504", \
        f"Claim key must scope to function and minute, got {call['key']!r}"
    assert call["value"] == "node-a", \
        f"The claim should record which node won, got {call['value']!r}"
    assert call["nx"] is True, \
        "The claim must be exclusive (nx=True), otherwise every node overwrites and runs"
    assert call["ex"] == cron.CRON_FLEET_ONCE_TTL, \
        f"Claim must expire so a dead winner cannot wedge later minutes, got {call['ex']}"


@th.django_unit_test()
def test_claim_fleet_once_loses_when_another_node_holds_it(opts):
    """redis-py returns None (not False) when NX finds the key already set."""
    from mojo.helpers import cron

    client = _StubRedis(result=None)
    won = cron.claim_fleet_once("pkg.mod.func", "202601021504", redis_client=client)

    assert won is False, "A lost race must skip the function on this node"


@th.django_unit_test()
def test_claim_fleet_once_fails_open_when_redis_errors(opts):
    """Redis down must degrade to single-node behavior, never to silence."""
    from mojo.helpers import cron

    client = _StubRedis(error=RuntimeError("redis unavailable"))
    won = cron.claim_fleet_once("pkg.mod.func", "202601021504", redis_client=client)

    assert won is True, \
        "A failed claim must fall back to running: a missed cert renewal is " \
        "worse than a duplicated sweep"


@th.django_unit_test()
def test_claim_fleet_once_has_exactly_one_winner_under_concurrency(opts):
    """The fleet contract itself, against real Redis: N nodes race, one runs."""
    import threading
    import uuid as uuid_module
    from mojo.helpers import cron
    from mojo.helpers.redis import get_connection

    # Unique per run: a re-run inside the 120s TTL must not see a stale claim.
    func_name = f"tests.fleet_once.{uuid_module.uuid4().hex}"
    bucket = "202601021504"
    key = f"{cron.CRON_FLEET_ONCE_PREFIX}:{func_name}:{bucket}"
    client = get_connection()
    client.delete(key)

    node_count = 8
    results = []
    results_lock = threading.Lock()
    ready = threading.Barrier(node_count, timeout=15)

    def race(index):
        ready.wait()
        won = cron.claim_fleet_once(func_name, bucket, node_id=f"node-{index}")
        with results_lock:
            results.append(won)

    try:
        threads = [threading.Thread(target=race, args=(i,))
                   for i in range(node_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == node_count, \
            f"Every racing node should report a result, got {len(results)}"
        winners = [r for r in results if r]
        assert len(winners) == 1, \
            f"Exactly one node may win the claim, got {len(winners)} winners"
    finally:
        client.delete(key)


# test_decorator_registers_per_node_flag lives in
# tests/test_helpers_extended_serial/cron.py: asserting what @schedule records
# means swapping the shared schedule.scheduled_functions registry, which is
# process-global and over this package's cold_budget in the default tier.


@th.django_unit_test()
def test_edge_cases_and_boundaries(opts):
    """Test edge cases and boundary values"""
    from mojo.helpers.cron import matches

    # Boundary values for minutes (0-59)
    assert matches('0', 0) == True, "Should match minute 0"
    assert matches('59', 59) == True, "Should match minute 59"
    assert matches('0-59', 30) == True, "Should match any minute in full range"

    # Boundary values for hours (0-23)
    assert matches('0', 0) == True, "Should match hour 0"
    assert matches('23', 23) == True, "Should match hour 23"
    assert matches('0-23', 12) == True, "Should match any hour in full range"

    # Boundary values for days (1-31)
    assert matches('1', 1) == True, "Should match day 1"
    assert matches('31', 31) == True, "Should match day 31"
    assert matches('1-31', 15) == True, "Should match any day in full range"

    # Boundary values for months (1-12)
    assert matches('1', 1) == True, "Should match month 1"
    assert matches('12', 12) == True, "Should match month 12"
    assert matches('1-12', 6) == True, "Should match any month in full range"

    # Boundary values for weekdays (0-6, where 0=Monday)
    assert matches('0', 0) == True, "Should match weekday 0 (Monday)"
    assert matches('6', 6) == True, "Should match weekday 6 (Sunday)"
    assert matches('0-6', 3) == True, "Should match any weekday in full range"

    # Step patterns at boundaries
    assert matches('*/30', 0) == True, "*/30 should match 0"
    assert matches('*/30', 30) == True, "*/30 should match 30"
    assert matches('*/30', 60) == True, "*/30 should technically match 60 (though minute 60 doesn't exist)"
