from testit import helpers as th
from datetime import datetime
import pytz


@th.django_unit_test()
def test_get_utc_hour_est(opts):
    """Test converting local hour to UTC for EST timezone"""
    from mojo.helpers import dates

    # Winter time (EST, UTC-5)
    winter_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=pytz.UTC)

    # 9 AM EST should be 14:00 UTC (9 + 5)
    utc_hour = dates.get_utc_hour('America/New_York', 9, now_utc=winter_time)
    assert utc_hour == 14, f"Expected 14, got {utc_hour}"

    # 5 PM EST should be 22:00 UTC (17 + 5)
    utc_hour = dates.get_utc_hour('America/New_York', 17, now_utc=winter_time)
    assert utc_hour == 22, f"Expected 22, got {utc_hour}"

    # Midnight EST should be 5:00 UTC
    utc_hour = dates.get_utc_hour('America/New_York', 0, now_utc=winter_time)
    assert utc_hour == 5, f"Expected 5, got {utc_hour}"


@th.django_unit_test()
def test_get_utc_hour_edt(opts):
    """Test converting local hour to UTC for EDT timezone (DST)"""
    from mojo.helpers import dates

    # Summer time (EDT, UTC-4)
    summer_time = datetime(2024, 7, 15, 12, 0, 0, tzinfo=pytz.UTC)

    # 9 AM EDT should be 13:00 UTC (9 + 4)
    utc_hour = dates.get_utc_hour('America/New_York', 9, now_utc=summer_time)
    assert utc_hour == 13, f"Expected 13, got {utc_hour}"

    # 5 PM EDT should be 21:00 UTC (17 + 4)
    utc_hour = dates.get_utc_hour('America/New_York', 17, now_utc=summer_time)
    assert utc_hour == 21, f"Expected 21, got {utc_hour}"


@th.django_unit_test()
def test_get_utc_hour_london(opts):
    """Test converting local hour to UTC for London timezone"""
    from mojo.helpers import dates

    # Winter time (GMT, UTC+0)
    winter_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=pytz.UTC)

    # 9 AM GMT should be 9:00 UTC
    utc_hour = dates.get_utc_hour('Europe/London', 9, now_utc=winter_time)
    assert utc_hour == 9, f"Expected 9, got {utc_hour}"

    # Summer time (BST, UTC+1)
    summer_time = datetime(2024, 7, 15, 12, 0, 0, tzinfo=pytz.UTC)

    # 9 AM BST should be 8:00 UTC (9 - 1)
    utc_hour = dates.get_utc_hour('Europe/London', 9, now_utc=summer_time)
    assert utc_hour == 8, f"Expected 8, got {utc_hour}"


@th.django_unit_test()
def test_get_utc_hour_tokyo(opts):
    """Test converting local hour to UTC for Tokyo timezone"""
    from mojo.helpers import dates

    # JST is always UTC+9 (no DST)
    ref_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=pytz.UTC)

    # 9 AM JST should be 0:00 UTC (9 - 9)
    utc_hour = dates.get_utc_hour('Asia/Tokyo', 9, now_utc=ref_time)
    assert utc_hour == 0, f"Expected 0, got {utc_hour}"

    # 5 PM JST should be 8:00 UTC (17 - 9)
    utc_hour = dates.get_utc_hour('Asia/Tokyo', 17, now_utc=ref_time)
    assert utc_hour == 8, f"Expected 8, got {utc_hour}"

    # Midnight JST should be 15:00 UTC previous day
    utc_hour = dates.get_utc_hour('Asia/Tokyo', 0, now_utc=ref_time)
    assert utc_hour == 15, f"Expected 15, got {utc_hour}"


@th.django_unit_test()
def test_get_utc_hour_validation(opts):
    """Test that get_utc_hour validates input"""
    from mojo.helpers import dates

    try:
        dates.get_utc_hour('America/New_York', -1)
        assert False, "Expected ValueError for hour < 0"
    except ValueError as e:
        assert "must be between 0 and 23" in str(e)

    try:
        dates.get_utc_hour('America/New_York', 24)
        assert False, "Expected ValueError for hour > 23"
    except ValueError as e:
        assert "must be between 0 and 23" in str(e)


@th.django_unit_test()
def test_get_utc_operating_day_before_cutover(opts):
    """Test operating day when current time is before cutover hour"""
    from mojo.helpers import dates

    # Current time: 2024-01-15 3:00 PM EST (before 10 PM cutover)
    ref_time = datetime(2024, 1, 15, 20, 0, 0, tzinfo=pytz.UTC)  # 3 PM EST

    # Cutover at 10 PM (22:00)
    start_utc, end_utc = dates.get_utc_operating_day('America/New_York', 22, now_utc=ref_time)

    # Operating day should be: 2024-01-14 10:00 PM EST to 2024-01-15 10:00 PM EST
    # In UTC: 2024-01-15 03:00 AM to 2024-01-16 03:00 AM
    assert start_utc == datetime(2024, 1, 15, 3, 0, 0, tzinfo=pytz.UTC), \
        f"Expected start 2024-01-15 03:00:00+00:00, got {start_utc}"
    assert end_utc == datetime(2024, 1, 16, 3, 0, 0, tzinfo=pytz.UTC), \
        f"Expected end 2024-01-16 03:00:00+00:00, got {end_utc}"


@th.django_unit_test()
def test_get_utc_operating_day_after_cutover(opts):
    """Test operating day when current time is after cutover hour"""
    from mojo.helpers import dates

    # Current time: 2024-01-15 11:00 PM EST (after 10 PM cutover)
    ref_time = datetime(2024, 1, 16, 4, 0, 0, tzinfo=pytz.UTC)  # 11 PM EST

    # Cutover at 10 PM (22:00)
    start_utc, end_utc = dates.get_utc_operating_day('America/New_York', 22, now_utc=ref_time)

    # Operating day should be: 2024-01-15 10:00 PM EST to 2024-01-16 10:00 PM EST
    # In UTC: 2024-01-16 03:00 AM to 2024-01-17 03:00 AM
    assert start_utc == datetime(2024, 1, 16, 3, 0, 0, tzinfo=pytz.UTC), \
        f"Expected start 2024-01-16 03:00:00+00:00, got {start_utc}"
    assert end_utc == datetime(2024, 1, 17, 3, 0, 0, tzinfo=pytz.UTC), \
        f"Expected end 2024-01-17 03:00:00+00:00, got {end_utc}"


@th.django_unit_test()
def test_get_utc_operating_day_at_cutover(opts):
    """Test operating day when current time is exactly at cutover hour"""
    from mojo.helpers import dates

    # Current time: 2024-01-15 10:00 PM EST (exactly at cutover)
    ref_time = datetime(2024, 1, 16, 3, 0, 0, tzinfo=pytz.UTC)  # 10 PM EST

    # Cutover at 10 PM (22:00)
    start_utc, end_utc = dates.get_utc_operating_day('America/New_York', 22, now_utc=ref_time)

    # At cutover, should start new operating day
    # Operating day: 2024-01-15 10:00 PM EST to 2024-01-16 10:00 PM EST
    # In UTC: 2024-01-16 03:00 AM to 2024-01-17 03:00 AM
    assert start_utc == datetime(2024, 1, 16, 3, 0, 0, tzinfo=pytz.UTC), \
        f"Expected start 2024-01-16 03:00:00+00:00, got {start_utc}"
    assert end_utc == datetime(2024, 1, 17, 3, 0, 0, tzinfo=pytz.UTC), \
        f"Expected end 2024-01-17 03:00:00+00:00, got {end_utc}"


@th.django_unit_test()
def test_get_utc_operating_day_morning_cutover(opts):
    """Test operating day with morning cutover (e.g., 6 AM)"""
    from mojo.helpers import dates

    # Current time: 2024-01-15 3:00 AM EST (before 6 AM cutover)
    ref_time = datetime(2024, 1, 15, 8, 0, 0, tzinfo=pytz.UTC)  # 3 AM EST

    # Cutover at 6 AM (06:00)
    start_utc, end_utc = dates.get_utc_operating_day('America/New_York', 6, now_utc=ref_time)

    # Operating day should be: 2024-01-14 6:00 AM EST to 2024-01-15 6:00 AM EST
    # In UTC: 2024-01-14 11:00 AM to 2024-01-15 11:00 AM
    assert start_utc == datetime(2024, 1, 14, 11, 0, 0, tzinfo=pytz.UTC), \
        f"Expected start 2024-01-14 11:00:00+00:00, got {start_utc}"
    assert end_utc == datetime(2024, 1, 15, 11, 0, 0, tzinfo=pytz.UTC), \
        f"Expected end 2024-01-15 11:00:00+00:00, got {end_utc}"


@th.django_unit_test()
def test_get_utc_operating_day_dst_transition(opts):
    """Test operating day during DST transition"""
    from mojo.helpers import dates

    # DST spring forward: 2024-03-10 2:00 AM EST -> 3:00 AM EDT
    # Test a day during DST (summer)
    summer_ref = datetime(2024, 7, 15, 20, 0, 0, tzinfo=pytz.UTC)  # 4 PM EDT

    # Cutover at 10 PM (22:00)
    start_utc, end_utc = dates.get_utc_operating_day('America/New_York', 22, now_utc=summer_ref)

    # During EDT (UTC-4), 10 PM local = 2:00 AM UTC next day
    # Operating day: 2024-07-14 10:00 PM EDT to 2024-07-15 10:00 PM EDT
    # In UTC: 2024-07-15 02:00 AM to 2024-07-16 02:00 AM
    assert start_utc == datetime(2024, 7, 15, 2, 0, 0, tzinfo=pytz.UTC), \
        f"Expected start 2024-07-15 02:00:00+00:00, got {start_utc}"
    assert end_utc == datetime(2024, 7, 16, 2, 0, 0, tzinfo=pytz.UTC), \
        f"Expected end 2024-07-16 02:00:00+00:00, got {end_utc}"


@th.django_unit_test()
def test_get_utc_operating_day_midnight_cutover(opts):
    """Test operating day with midnight cutover (same as calendar day)"""
    from mojo.helpers import dates

    # Current time: 2024-01-15 6:00 PM EST
    ref_time = datetime(2024, 1, 15, 23, 0, 0, tzinfo=pytz.UTC)  # 6 PM EST

    # Cutover at midnight (0:00)
    start_utc, end_utc = dates.get_utc_operating_day('America/New_York', 0, now_utc=ref_time)

    # Operating day should be: 2024-01-15 midnight EST to 2024-01-16 midnight EST
    # In UTC: 2024-01-15 05:00 AM to 2024-01-16 05:00 AM
    assert start_utc == datetime(2024, 1, 15, 5, 0, 0, tzinfo=pytz.UTC), \
        f"Expected start 2024-01-15 05:00:00+00:00, got {start_utc}"
    assert end_utc == datetime(2024, 1, 16, 5, 0, 0, tzinfo=pytz.UTC), \
        f"Expected end 2024-01-16 05:00:00+00:00, got {end_utc}"


@th.django_unit_test()
def test_get_utc_operating_day_validation(opts):
    """Test that get_utc_operating_day validates input"""
    from mojo.helpers import dates

    try:
        dates.get_utc_operating_day('America/New_York', -1)
        assert False, "Expected ValueError for hour < 0"
    except ValueError as e:
        assert "must be between 0 and 23" in str(e)

    try:
        dates.get_utc_operating_day('America/New_York', 24)
        assert False, "Expected ValueError for hour > 23"
    except ValueError as e:
        assert "must be between 0 and 23" in str(e)


@th.django_unit_test()
def test_get_utc_operating_day_24_hour_span(opts):
    """Test that operating day always spans exactly 24 hours"""
    from mojo.helpers import dates

    ref_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=pytz.UTC)

    for hour in [0, 6, 12, 18, 22]:
        start_utc, end_utc = dates.get_utc_operating_day('America/New_York', hour, now_utc=ref_time)
        duration = end_utc - start_utc
        assert duration.total_seconds() == 86400, \
            f"Expected 24 hours (86400 seconds), got {duration.total_seconds()} for hour {hour}"
