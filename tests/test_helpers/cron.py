from testit import helpers as th
import datetime
from unittest.mock import patch, MagicMock

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

@th.django_unit_test()
def test_find_scheduled_functions(opts):
    """Test finding scheduled functions"""
    from mojo.decorators.cron import schedule
    from mojo.helpers.cron import find_scheduled_functions

    # Clear any existing scheduled functions
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions.clear()
    else:
        schedule.scheduled_functions = []

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
        mock_datetime.datetime.now.return_value = opts.test_time

        # Find functions that should run at 11:50
        funcs = find_scheduled_functions()

        # test_func1 should match (11:50)
        # test_func2 should match (every minute)
        # test_func3 should not match (9:30)
        assert len(funcs) == 2, f"Expected 2 functions to match at 11:50, got {len(funcs)}"
        assert test_func1 in funcs, "test_func1 (11:50) should be in matched functions"
        assert test_func2 in funcs, "test_func2 (every minute) should be in matched functions"
        assert test_func3 not in funcs, "test_func3 (9:30) should not be in matched functions"


@th.django_unit_test()
def test_load_app_cron(opts):
    """Test loading cronjobs from apps"""
    from mojo.helpers.cron import load_app_cron
    from unittest.mock import patch, MagicMock

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
    """Test the run_now function"""
    from mojo.helpers.cron import run_now
    from mojo.decorators.cron import schedule

    # Clear existing functions
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions.clear()
    else:
        schedule.scheduled_functions = []

    # Track function executions
    executed = []

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
    with patch('mojo.helpers.cron.datetime') as mock_datetime:
        mock_datetime.datetime.now.return_value = opts.test_time

        # Run scheduled functions
        run_now()

        # Check which functions executed
        assert 'always' in executed, "always_run should have executed"
        assert 'specific' in executed, "specific_time should have executed"
        assert 'other' not in executed, "other_time should not have executed"
        assert len(executed) == 2, f"Expected 2 functions to execute, got {len(executed)}: {executed}"


@th.django_unit_test()
def test_decorator_registration(opts):
    """Test that the @schedule decorator properly registers functions"""
    from mojo.decorators.cron import schedule

    # Clear any existing functions
    if hasattr(schedule, 'scheduled_functions'):
        schedule.scheduled_functions.clear()
    else:
        schedule.scheduled_functions = []

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
