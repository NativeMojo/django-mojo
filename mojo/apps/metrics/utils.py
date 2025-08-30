import time
from datetime import timedelta
import datetime
from mojo.helpers.settings import settings
from mojo.helpers import dates

GRANULARITIES = ['minutes', 'hours', 'days', 'weeks', 'months', 'years']
GRANULARITY_PREFIX_MAP = {
    'minutes': 'min',
    'hours': 'hr',
    'days': 'day',
    'weeks': 'wk',
    'months': 'mon',
    'years': 'yr'
}

GRANULARITY_EXPIRES_DAYS = {
    'minutes': 1,  # 24 hours?
    'hours': 3,  # 72 hours?
    'days': 360,
    'weeks': 360,
    'months': None,
    'years': None
}

GRANULARITY_OFFSET_MAP = {
    'minutes': timedelta(minutes=1),
    'hours': timedelta(hours=1),
    'days': timedelta(days=1),
    'weeks': timedelta(weeks=1),
    'months': 'months',
    'years': 'years'
}

GRANULARITY_END_MAP = {
    'minutes': timedelta(minutes=29),
    'hours': timedelta(hours=24),
    'days': timedelta(days=30),
    'weeks': timedelta(weeks=11),
    'months': timedelta(days=12*30),
    'years': timedelta(days=11*360)
}


DEFAULT_MIN_GRANULARITY = settings.get("METRICS_DEFAULT_MIN_GRANULARITY", "hours")
DEFAULT_MAX_GRANULARITY = settings.get("METRICS_DEFAULT_MAX_GRANULARITY", "years")

METRICS_TIMEZONE = settings.get("METRICS_TIMEZONE", "America/Los_Angeles")

def generate_granularities(min_granularity=DEFAULT_MIN_GRANULARITY,
                           max_granularity=DEFAULT_MAX_GRANULARITY):
    """
    Generate a list of granularities between a minimum and maximum level.

    Args:
        min_granularity (str): The minimum granularity level.
        max_granularity (str): The maximum granularity level.

    Returns:
        list: A list of granularities from min_granularity to max_granularity.

    Raises:
        ValueError: If the specified granularities are invalid or
        min_granularity is greater than max_granularity.
    """
    all_granularities = ['minutes', 'hours', 'days', 'weeks', 'months', 'years']

    if min_granularity not in all_granularities or max_granularity not in all_granularities:
        raise ValueError(
            "Invalid granularity. Choose from 'minutes', 'hours', 'days', "
            "'weeks', 'months', 'years'."
        )

    min_index = all_granularities.index(min_granularity)
    max_index = all_granularities.index(max_granularity)
    if min_index > max_index:
        raise ValueError("min_granularity must be less than or equal to max_granularity.")
    return all_granularities[min_index:max_index + 1]


def generate_slug(slug, date, granularity, account="global"):
    """
    Generate a slug for a given date and granularity.

    Args:
        date: The date to format.
        granularity (str): The granularity level for the slug.
        *args: Additional strings to include in the slug prefix, separated by
        colons.

    Returns:
        str: A formatted slug.

    Raises:
        ValueError: If the specified granularity is invalid for slug generation.
    """
    if granularity not in ['minutes', 'hours', 'days', 'weeks', 'months', 'years']:
        raise ValueError("Invalid granularity for slug generation.")
    gran_prefix = GRANULARITY_PREFIX_MAP.get(granularity)
    if granularity == 'minutes':
        date_slug = date.strftime('%Y-%m-%dT%H-%M')
    elif granularity == 'hours':
        date_slug = date.strftime('%Y-%m-%dT%H')
    elif granularity == 'days':
        date_slug = date.strftime('%Y-%m-%d')
    elif granularity == 'weeks':
        date_slug = date.strftime('%Y-%U')
    elif granularity == 'months':
        date_slug = date.strftime('%Y-%m')
    elif granularity == 'years':
        date_slug = date.strftime('%Y')
    else:
        raise ValueError("Unhandled granularity.")
    prefix = generate_slug_prefix(slug, account)
    return f"{prefix}:{gran_prefix}:{date_slug}"


def generate_slug_prefix(slug, account):
    # this is the slug without the date
    slug = normalize_slug(slug)
    return f"mets:{account}::{slug}"


def generate_slugs_for_range(slug, dt_start, dt_end, granularity, account="global"):
    """
    Generate slugs for dates in a specified range with the given granularity.

    Args:
        slug (str): The base slug to use in generating slugs for the range.
        dt_start (datetime): The start date of the range.
        dt_end (datetime): The end date of the range.
        granularity (str): The granularity level for iteration.

    Returns:
        list: A list of generated slugs for each date/time in the range at the specified granularity.

    Raises:
        ValueError: If the specified granularity is invalid.
    """


    if granularity not in GRANULARITY_OFFSET_MAP:
        raise ValueError("Invalid granularity for slug generation.")
    dt_start, dt_end = get_date_range(dt_start, dt_end, granularity)
    current = dt_start
    slugs = []

    if granularity in ['minutes', 'hours', 'days', 'weeks']:
        delta = GRANULARITY_OFFSET_MAP[granularity]
        while current <= dt_end:
            slugs.append(generate_slug(slug, current, granularity, account))
            current += delta
    elif granularity == 'months':
        while current <= dt_end:
            slugs.append(generate_slug(slug, current, granularity, account))
            current = datetime.datetime(current.year + (current.month // 12), ((current.month % 12) + 1), 1, tzinfo=current.tzinfo)
    elif granularity == 'years':
        while current <= dt_end:
            slugs.append(generate_slug(slug, current, granularity, account))
            current = datetime.datetime(current.year + 1, 1, 1, tzinfo=current.tzinfo)
    return slugs


def generate_category_slug(account, category):
    return f"mets:{account}:c:{category}"


def generate_category_key(account):
    return f"mets:{account}:cats"

def generate_slugs_key(account):
    return f"mets:{account}:slugs"


def generate_perm_write_key(account):
    return f"mets:{account}:perm:w"


def generate_perm_view_key(account):
    return f"mets:{account}:perm:v"

def generate_accounts_key():
    return f"mets:_accounts_"

def generate_value_key(slug, account):
    """
    Generate a Redis key for storing simple key-value pairs.

    Args:
        slug (str): The slug identifier for the value.
        account (str): The account under which the value is stored.

    Returns:
        str: The Redis key for the value.
    """
    normalized_slug = normalize_slug(slug)
    return f"mets:{account}:val:{normalized_slug}"


def normalize_slug(slug):
    return slug.replace(':', '|')


def periods_from_dr_slugs(slugs):
    if isinstance(slugs, (list, set)):
        result = []
        for s in slugs:
            result.append(period_from_dr_slug(s))
        return result

def period_from_dr_slug(slug):
    parts = slug.split(":")
    gran_lbl = parts[-2]
    period = parts[-1]
    if gran_lbl == GRANULARITY_PREFIX_MAP["hours"]:
        return f"{period[-2:]}:00"
    if gran_lbl == GRANULARITY_PREFIX_MAP["minutes"]:
        return period[-5:].replace('-', ':')
    return period


def get_expires_at(granularity, slug, category=None):
    days = GRANULARITY_EXPIRES_DAYS.get(granularity, None)
    if days is None:
        return None
    return int(time.time() + (days * 24 * 60 * 60))

def normalize_datetime(when, timezone=None):
    if when is not None and when.tzinfo is not None:
        return when
    if timezone is None:
        timezone = METRICS_TIMEZONE
    return dates.get_local_time(timezone, when)


def get_date_range(dt_start, dt_end, granularity):
    if dt_start is None and dt_end is None:
        dt_end = normalize_datetime(None)
        dt_start = dt_end - GRANULARITY_END_MAP[granularity]
    elif dt_end is None:
        dt_end = dt_start + GRANULARITY_END_MAP[granularity]
    elif dt_start is None:
        dt_start = dt_end - GRANULARITY_END_MAP[granularity]
    return dt_start, dt_end
