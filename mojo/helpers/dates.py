from datetime import datetime, timedelta
import pytz
import objict
from .settings import settings


def parse_datetime(value, timezone=None):
    if isinstance(value, datetime):
        date = value
    else:
        date = objict.parse_date(value)
    if date.tzinfo is None:
        date = pytz.UTC.localize(date)
    if timezone is not None and isinstance(timezone, str):
        local_tz = pytz.timezone(timezone)
    else:
        local_tz = pytz.UTC
    return date.astimezone(local_tz)


def get_local_day(timezone, dt_utc=None, hour=0):
    if dt_utc is None:
        dt_utc = datetime.now(tz=pytz.UTC)
    local_tz = pytz.timezone(timezone)
    local_dt = dt_utc.astimezone(local_tz)
    start_of_day = local_tz.localize(datetime(
        local_dt.year, local_dt.month, local_dt.day, hour, 0, 0))
    end_of_day = start_of_day + timedelta(days=1)
    return start_of_day.astimezone(pytz.UTC), end_of_day.astimezone(pytz.UTC)


def get_local_time(timezone, dt_utc=None):
    """Convert a passed in datetime to the group's timezone."""
    if dt_utc is None:
        dt_utc = datetime.now(tz=pytz.UTC)
    if dt_utc.tzinfo is None:
        dt_utc = pytz.UTC.localize(dt_utc)
    local_tz = pytz.timezone(timezone)
    return dt_utc.astimezone(local_tz)

def get_utc_hour(timezone, local_hour, now_utc=None):
    """
    Convert a local hour (0-23) in a given timezone to UTC hour.

    Args:
        timezone: Timezone string (e.g., 'America/New_York', 'Europe/London')
        local_hour: Hour in local time (0-23)
        now_utc: Optional UTC datetime to use as reference (default: now)

    Returns:
        int: Hour in UTC (0-23)

    Example:
        # 9 AM in New York (EST/EDT) -> UTC hour
        get_utc_hour('America/New_York', 9)  # Returns 14 or 13 depending on DST
    """
    if not 0 <= local_hour <= 23:
        raise ValueError("local_hour must be between 0 and 23")

    local_tz = pytz.timezone(timezone)
    if now_utc is None:
        now_utc = datetime.now(tz=pytz.UTC)
    now_local = now_utc.astimezone(local_tz)

    # Create a datetime for the specified hour today in the local timezone
    local_dt = local_tz.localize(datetime(
        now_local.year, now_local.month, now_local.day, local_hour, 0, 0))

    # Convert to UTC and return the hour
    utc_dt = local_dt.astimezone(pytz.UTC)
    return utc_dt.hour

def get_utc_operating_day(timezone, local_hour, now_utc=None):
    """
    Get the UTC time range for a business's operating day.

    An operating day runs from a specified hour (e.g., 10 PM) to the same hour
    the next calendar day. This is useful for businesses that close late and
    want their "day" to roll over after closing time rather than at midnight.

    Args:
        timezone: Timezone string (e.g., 'America/New_York', 'Europe/London')
        local_hour: Hour when the operating day starts/ends (0-23)
                   E.g., 22 for 10 PM means operating day runs from 10 PM to 10 PM
        now_utc: Optional UTC datetime to use as reference (default: now)

    Returns:
        tuple: (start_utc, end_utc) - UTC datetime objects for the current operating day

    Example:
        # Business closes at 10 PM local time
        # Operating day runs from yesterday 10 PM to today 10 PM
        start, end = get_utc_operating_day('America/New_York', 22)

        # If current time is 2024-01-15 3:00 PM EST:
        # - Operating day started: 2024-01-14 10:00 PM EST (2024-01-15 03:00 AM UTC)
        # - Operating day ends:    2024-01-15 10:00 PM EST (2024-01-16 03:00 AM UTC)

        # Business opens at 6 AM, closes at midnight (cutover at 6 AM)
        start, end = get_utc_operating_day('Europe/London', 6)
    """
    if not 0 <= local_hour <= 23:
        raise ValueError("local_hour must be between 0 and 23")

    local_tz = pytz.timezone(timezone)
    if now_utc is None:
        now_utc = datetime.now(tz=pytz.UTC)

    now_local = now_utc.astimezone(local_tz)

    # Create datetime for the cutover hour today
    cutover_today = local_tz.localize(datetime(
        now_local.year, now_local.month, now_local.day, local_hour, 0, 0))

    # Determine if we're before or after the cutover hour
    if now_local < cutover_today:
        # We're before the cutover, so operating day started yesterday at this hour
        start_of_operating_day = cutover_today - timedelta(days=1)
        end_of_operating_day = cutover_today
    else:
        # We're after the cutover, so operating day started today at this hour
        start_of_operating_day = cutover_today
        end_of_operating_day = cutover_today + timedelta(days=1)

    # Convert to UTC
    return start_of_operating_day.astimezone(pytz.UTC), end_of_operating_day.astimezone(pytz.UTC)

def utcnow():
    """look at django setting to get proper datetime aware or not"""
    if settings.USE_TZ:
        return datetime.now(tz=pytz.UTC)
    return datetime.utcnow()


def add(when=None, seconds=None, minutes=None, hours=None, days=None):
    if when is None:
        when = utcnow()
    elapsed_time = timedelta(
        seconds=seconds or 0,
        minutes=minutes or 0,
        hours=hours or 0,
        days=days or 0
    )
    return when + elapsed_time


def subtract(when=None, seconds=None, minutes=None, hours=None, days=None):
    if when is None:
        when = utcnow()
    elapsed_time = timedelta(
        seconds=seconds or 0,
        minutes=minutes or 0,
        hours=hours or 0,
        days=days or 0
    )
    return when - elapsed_time


def has_time_elsapsed(when, seconds=None, minutes=None, hours=None, days=None):
    return utcnow() >= add(when, seconds, minutes, hours, days)


def is_today(when, timezone=None):
    if timezone is None:
        timezone = 'UTC'

    # Convert when to the specified timezone
    if when.tzinfo is None:
        when = pytz.UTC.localize(when)
    local_tz = pytz.timezone(timezone)
    when_local = when.astimezone(local_tz)

    # Get today's date in the specified timezone
    now_utc = utcnow()
    now_local = now_utc.astimezone(local_tz)

    # Compare dates
    return when_local.date() == now_local.date()
