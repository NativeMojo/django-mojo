import time
from datetime import timedelta
import datetime


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

DEFAULT_MIN_GRANULARITY = "hours"
DEFAULT_MAX_GRANULARITY = "years"
CATEGORY_KEY = "mets:categories"


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


def generate_slug(slug, date, granularity, *args):
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
    prefix = GRANULARITY_PREFIX_MAP.get(granularity)
    if granularity == 'minutes':
        slug = date.strftime('%Y-%m-%dT%H:%M')
    elif granularity == 'hours':
        slug = date.strftime('%Y-%m-%dT%H')
    elif granularity == 'days':
        slug = date.strftime('%Y-%m-%d')
    elif granularity == 'weeks':
        slug = date.strftime('%Y-%U')
    elif granularity == 'months':
        slug = date.strftime('%Y-%m')
    elif granularity == 'years':
        slug = date.strftime('%Y')
    else:
        raise ValueError("Unhandled granularity.")
    arg_prefix = ':'.join(args)
    if arg_prefix:
        prefix = f"mets:{prefix}:{arg_prefix}"
    return f"mets:{prefix}:{slug}"


def generate_slugs_for_range(slug, dt_start, dt_end, granularity):
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
    granularity_map = {
        'minutes': timedelta(minutes=1),
        'hours': timedelta(hours=1),
        'days': timedelta(days=1),
        'weeks': timedelta(weeks=1),
        'months': 'months',
        'years': 'years'
    }

    if granularity not in granularity_map:
        raise ValueError("Invalid granularity for slug generation.")

    current = dt_start
    slugs = []

    if granularity in ['minutes', 'hours', 'days', 'weeks']:
        delta = granularity_map[granularity]
        while current <= dt_end:
            slugs.append(generate_slug(slug, current, granularity))
            current += delta
    elif granularity == 'months':
        while current <= dt_end:
            slugs.append(generate_slug(slug, current, granularity))
            current = datetime.datetime(current.year + (current.month // 12), ((current.month % 12) + 1), 1)
    elif granularity == 'years':
        while current <= dt_end:
            slugs.append(generate_slug(slug, current, granularity))
            current = datetime.datetime(current.year + 1, 1, 1)
    return slugs


def generate_category_slug(category):
    return f"mets:c:{category}"


def get_expires_at(granularity, slug, category=None):
    days = GRANULARITY_EXPIRES_DAYS.get(granularity, None)
    if days is None:
        return None
    return time.time() + (days * 24 * 60 * 60)
