from datetime import datetime, timedelta
import pytz
import objict


def parse_datetime(value, timezone=None):
    date = objict.parse_date(value)
    if date.tzinfo is None:
        date = pytz.UTC.localize(date)
    if timezone is not None:
        local_tz = pytz.timezone(timezone)
    else:
        local_tz = pytz.UTC
    return date.astimezone(local_tz)


def get_local_day(timezone, dt_utc=None):
    if dt_utc is None:
        dt_utc = datetime.now(tz=pytz.UTC)
    local_tz = pytz.timezone(timezone)
    local_dt = dt_utc.astimezone(local_tz)
    start_of_day = local_tz.localize(datetime(
        local_dt.year, local_dt.month, local_dt.day, 0, 0, 0))
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
