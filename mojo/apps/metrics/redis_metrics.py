from . import utils
from mojo.helpers import redis, dates
from mojo.helpers.settings import settings
import datetime
from objict import objict, nobjict


def record(slug, when=None, count=1, category=None, account="global",
                   min_granularity="hours", max_granularity="years", timezone=None):
    """
    Records metrics in Redis by incrementing counters for various time granularities.

    Args:
        slug (str): The base identifier for the metric.
        when (datetime, optional): The time at which the event occurred. Defaults to current time if not provided.
        count (int, optional): The count to increment the metric by. Defaults to 1.
        category (str, optional): The category to which the metric belongs. Useful for grouping similar metrics.
        account (str, optional): The account under which the metric is recorded. Defaults to "global".
        min_granularity (str, optional): The minimum time granularity (e.g., "hours"). Defaults to "hours".
        max_granularity (str, optional): The maximum time granularity (e.g., "years"). Defaults to "years".
        timezone (str, optional): The timezone to use as the base for the granularity calculations.
        *kwargs: Additional arguments to be used in slug generation.

    Returns:
        None: This function doesn't return a value. It performs its operations on Redis directly.
    """
    when = utils.normalize_datetime(when, timezone)
    # Get Redis connection
    redis_conn = redis.get_connection()
    pipeline = redis_conn.pipeline()
    if category is not None:
        add_category_slug(category, slug, pipeline, account)
    add_metrics_slug(slug, pipeline, account)
    # Generate granularities
    granularities = utils.generate_granularities(min_granularity, max_granularity)
    # Process each granularity
    for granularity in granularities:
        # Generate slug for the current granularity
        generated_slug = utils.generate_slug(slug, when, granularity, account)
        # Add count to the slug in Redis
        pipeline.incr(generated_slug, count)
        exp_at = utils.get_expires_at(granularity, slug, category)
        if exp_at:
            pipeline.expireat(generated_slug, exp_at)
    pipeline.execute()


def fetch(slug, dt_start=None, dt_end=None, granularity="hours",
          redis_con=None, account="global", with_labels=False, dr_slugs=None):
    """
    Fetches metrics from Redis based on slugs within a specified date range and granularity.

    Args:
        slug (str or list): The slug(s) identifying metrics to fetch.
        dt_start (datetime, optional): The start of the date range. Defaults to None.
        dt_end (datetime, optional): The end of the date range. Defaults to None.
        granularity (str, optional): The time granularity to use. Defaults to "hours".
        redis_con: The Redis connection instance (optional).
        account (str, optional): The account under which the metric is recorded. Defaults to "global".
        with_labels (bool, optional): If True, includes timestamp labels in response data. Defaults to False.
        dr_slugs: Pre-generated slugs for the date range. Defaults to None.

    Returns:
        list or nobjict: Returns a list of values or a structured nobjict with periods and data if `with_labels` is True.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    if isinstance(slug, (list, set)):
        resp = nobjict()
        if with_labels:
            resp.data = {}
            resp.labels = utils.periods_from_dr_slugs(utils.generate_slugs_for_range(
                slug[0], dt_start, dt_end, granularity, account))
        for s in slug:
            values = fetch(s, dt_start, dt_end, granularity, redis_con, account)
            if with_labels:
                resp.data[s] = values
            else:
                resp[s] = values
        return resp
    dr_slugs = utils.generate_slugs_for_range(slug, dt_start, dt_end, granularity, account)
    values = [int(met) if met is not None else 0 for met in redis_con.mget(dr_slugs)]
    if not with_labels:
        return values
    return nobjict(labels=utils.periods_from_dr_slugs(dr_slugs), data={slug: values})


def fetch_values(slugs, when=None, granularity="hours", redis_con=None, account="global", timezone=None):
    """
    Fetches specific metric values for multiple slugs at a single point in time.

    Args:
        slugs (str or list): The slug(s) to fetch values for. If string, should be comma-separated.
        when (datetime): The specific datetime to fetch values for.
        granularity (str, optional): The time granularity to use. Defaults to "hours".
        redis_con: The Redis connection instance (optional).
        account (str, optional): The account under which the metrics are recorded. Defaults to "global".

    Returns:
        dict: A dictionary containing:
            - 'data': dict mapping slug names to their values
            - 'slugs': list of slug names that were queried
            - 'when': the datetime that was queried (as string)
            - 'granularity': the granularity used
            - 'account': the account queried
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    when = utils.normalize_datetime(when, timezone)
    # Handle comma-separated string input
    if isinstance(slugs, str):
        if ',' in slugs:
            slugs = [s.strip() for s in slugs.split(',')]
        else:
            slugs = [slugs]

    # Generate Redis keys for each slug at the specific datetime
    redis_keys = []
    for slug in slugs:
        redis_key = utils.generate_slug(slug, when, granularity, account)
        redis_keys.append(redis_key)

    # Fetch all values with single MGET operation
    values = redis_con.mget(redis_keys)

    # Build response data dictionary
    data = {}
    for i, slug in enumerate(slugs):
        value = values[i]
        data[slug] = int(value) if value is not None else 0

    return {
        'data': data,
        'slugs': slugs,
        'when': when.isoformat() if hasattr(when, 'isoformat') else str(when),
        'granularity': granularity,
        'account': account
    }


def set_value(slug, value, redis_con=None, account="global"):
    """
    Sets a simple key-value pair in Redis for global storage (not time-series).

    Args:
        slug (str): The key identifier for the value.
        value: The value to store (will be converted to string).
        redis_con: The Redis connection instance (optional).
        account (str, optional): The account under which the value is stored. Defaults to "global".

    Returns:
        None
    """
    if redis_con is None:
        redis_con = redis.get_connection()

    key = utils.generate_value_key(slug, account)
    redis_con.set(key, str(value))


def get_value(slug, redis_con=None, account="global", default=None):
    """
    Retrieves a simple value from Redis for global storage (not time-series).

    Args:
        slug (str): The key identifier for the value.
        redis_con: The Redis connection instance (optional).
        account (str, optional): The account under which the value is stored. Defaults to "global".
        default: The default value to return if key doesn't exist. Defaults to None.

    Returns:
        str or default: The stored value as string, or default if key doesn't exist.
    """
    if redis_con is None:
        redis_con = redis.get_connection()

    key = utils.generate_value_key(slug, account)
    value = redis_con.get(key)

    if value is not None:
        return value.decode('utf-8')
    return default


def add_metrics_slug(slug, redis_con=None, account="global"):
    """
    Adds a metric slug to a Redis set for the specified account.

    Args:
        slug (str): The slug to add.
        redis_con: The Redis connection instance (optional).
        account (str): The account to which the slug should be added. Defaults to "global".

    Returns:
        None
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(utils.generate_slugs_key(account), slug)


def delete_metrics_slug(slug, account="global", redis_con=None):
    """
    Deletes a specific slug from the Redis set for a given account.

    Args:
        slug (str): The slug to delete.
        redis_con: The Redis connection instance (optional).
        account (str): The account from which the slug should be removed. Defaults to "global".

    Returns:
        None
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.srem(utils.generate_slugs_key(account), slug)

    # now lets delete all keys with our slug prefix
    prefix = utils.generate_slug_prefix(slug, account)
    return __delete_keys_with_prefix(prefix, redis_con)


def __delete_keys_with_prefix(prefix, redis_conn):
    cursor = b'0'
    total_deleted = 0
    while cursor != b'0':
        cursor, keys = redis_conn.scan(cursor=cursor, match=f"{prefix}*")
        if keys:
            total_deleted += len(keys)
            redis_conn.delete(*keys)
    return total_deleted

def get_account_slugs(account, redis_con=None):
    """
    Retrieves all slugs associated with a specific account from Redis.

    Args:
        account (str): The account for which to retrieve slugs.
        redis_con: The Redis connection instance (optional).

    Returns:
        set: A set of decoded slugs belonging to the specified account.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() for s in redis_con.smembers(utils.generate_slugs_key(account))}


def add_category_slug(category, slug, redis_con=None, account="global"):
    """
    Adds a slug to a category set in Redis and indexes the category.

    Args:
        category (str): The category to which the slug should be added.
        slug (str): The slug to add.
        redis_con: The Redis connection instance (optional).
        account (str): The account under which the category resides. Defaults to "global".

    Returns:
        None
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(utils.generate_category_slug(account, category), slug)
    redis_con.sadd(utils.generate_category_key(account), category)


def get_category_slugs(category, redis_con=None, account="global"):
    """
    Retrieves all slugs associated with a specific category from Redis.

    Args:
        category (str): The category for which to retrieve slugs.
        redis_con: The Redis connection instance (optional).
        account (str): The account under which the category resides. Defaults to "global".

    Returns:
        set: A set of decoded slugs belonging to the specified category.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() for s in redis_con.smembers(utils.generate_category_slug(account, category))}


def delete_category(category, redis_con=None, account="global"):
    """
    Deletes a specific category from Redis, including all associated slugs.

    Args:
        category (str): The category to delete.
        redis_con: The Redis connection instance (optional).
        account (str): The account under which the category resides. Defaults to "global".

    Returns:
        None
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    category_slug = utils.generate_category_slug(account, category)
    pipeline = redis_con.pipeline()
    pipeline.delete(category_slug)  # Deletes the entire set
    pipeline.srem(utils.generate_category_key(account), category)  # Remove the category name from index
    pipeline.execute()


def get_categories(redis_con=None, account="global"):
    """
    Retrieves all categories for a specific account from Redis.

    Args:
        redis_con: The Redis connection instance (optional).
        account (str): The account for which to retrieve categories. Defaults to "global".

    Returns:
        set: A set of decoded category names belonging to the specified account.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() for s in redis_con.smembers(utils.generate_category_key(account))}


def fetch_by_category(category, dt_start=None, dt_end=None, granularity="hours",
    redis_con=None, account="global", with_labels=False):
    """
    Fetches metrics for all slugs within a specified category, date range, and granularity.

    Args:
        category (str): The category for which to fetch metrics.
        dt_start (datetime, optional): The start date for fetching metrics.
        dt_end (datetime, optional): The end date for fetching metrics.
        granularity (str, optional): The granularity of the metrics. Defaults to "hours".
        redis_con: The Redis connection instance (optional).
        account (str, optional): The account under which the category resides. Defaults to "global".
        with_labels (bool, optional): If True, includes timestamp labels in response data. Defaults to False.

    Returns:
        list or nobjict: Fetches and returns metric data using the `fetch` function.
    """
    return fetch(get_category_slugs(category, redis_con, account), with_labels=with_labels, account=account)


def set_view_perms(account, perms, redis_con=None):
    """
    Sets view permissions for a specific account.

    Args:
        account (str): The account for which to set permissions.
        perms (str or list, optional): Permissions to set. If list, it will be converted to a comma-separated string.
        redis_con: The Redis connection instance (optional).

    Returns:
        None
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    view_perm_key = utils.generate_perm_view_key(account)
    if perms is None:
        redis_con.delete(view_perm_key)
    else:
        add_account(account, redis_con)
        if isinstance(perms, list):
            perms = ','.join(perms)
        redis_con.set(view_perm_key, perms)


def set_write_perms(account, perms, redis_con=None):
    """
    Sets write permissions for a specific account.

    Args:
        account (str): The account for which to set permissions.
        perms (str or list, optional): Permissions to set. If list, it will be converted to a comma-separated string.
        redis_con: The Redis connection instance (optional).

    Returns:
        None
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    write_perm_key = utils.generate_perm_write_key(account)
    if perms is None:
        redis_con.delete(write_perm_key)
    else:
        add_account(account, redis_con)
        if isinstance(perms, list):
            perms = ','.join(perms)
        redis_con.set(write_perm_key, perms)


def get_view_perms(account, redis_con=None):
    """
    Retrieves view permissions for a specific account.

    Args:
        account (str): The account for which to retrieve permissions.
        redis_con: The Redis connection instance (optional).

    Returns:
        str or list or None: The permissions for the specified account. Returns None if no permissions are set.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    view_perm_key = utils.generate_perm_view_key(account)
    perms = redis_con.get(view_perm_key)
    if perms:
        perms = perms.decode('utf-8')
        if ',' in perms:
            perms = perms.split(',')
    return perms


def get_write_perms(account, redis_con=None):
    """
    Retrieves write permissions for a specific account.

    Args:
        account (str): The account for which to retrieve permissions.
        redis_con: The Redis connection instance (optional).

    Returns:
        str or list or None: The permissions for the specified account. Returns None if no permissions are set.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    write_perm_key = utils.generate_perm_write_key(account)
    perms = redis_con.get(write_perm_key)
    if perms:
        perms = perms.decode('utf-8')
        if ',' in perms:
            perms = perms.split(',')
    return perms

def add_account(account, redis_con=None):
    """
    Adds a new account to the system.

    Args:
        account (str): The account to add.
        redis_con: The Redis connection instance (optional).

    Returns:
        bool: True if the account was added successfully, False otherwise.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    accounts_key = utils.generate_accounts_key()
    return redis_con.sadd(accounts_key, account) == 1

def list_accounts(redis_con=None):
    """
    Lists all accounts in the system.

    Args:
        redis_con: The Redis connection instance (optional).

    Returns:
        list: A list of all accounts in the system.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    accounts_key = utils.generate_accounts_key()
    return [account.decode('utf-8') for account in redis_con.smembers(accounts_key)]

def delete_account(account, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    set_view_perms(account, None, redis_con)
    set_write_perms(account, None, redis_con)
    accounts_key = utils.generate_accounts_key()
    return redis_con.srem(accounts_key, account)



def get_accounts_with_permissions(redis_con=None):
    """
    Scans Redis to find all accounts that have view or write permissions configured.

    Args:
        redis_con: The Redis connection instance (optional).

    Returns:
        list: A list of dictionaries containing account information and their permissions.
    """
    if redis_con is None:
        redis_con = redis.get_connection()

    accounts = {}

    # Scan for view permission keys
    cursor = b'0'
    while cursor != b'0':
        cursor, keys = redis_con.scan(cursor=cursor, match="mets:*:perm:v")
        for key in keys:
            key_str = key.decode('utf-8')
            # Extract account from key: mets:{account}:perm:v
            parts = key_str.split(':')
            if len(parts) >= 4:
                account = parts[1]
                if account not in accounts:
                    accounts[account] = {"account": account, "view_permissions": None, "write_permissions": None}

                perms = redis_con.get(key)
                if perms:
                    perms = perms.decode('utf-8')
                    if ',' in perms:
                        perms = perms.split(',')
                    accounts[account]["view_permissions"] = perms

    # Scan for write permission keys
    cursor = b'0'
    while cursor != b'0':
        cursor, keys = redis_con.scan(cursor=cursor, match="mets:*:perm:w")
        for key in keys:
            key_str = key.decode('utf-8')
            # Extract account from key: mets:{account}:perm:w
            parts = key_str.split(':')
            if len(parts) >= 4:
                account = parts[1]
                if account not in accounts:
                    accounts[account] = {"account": account, "view_permissions": None, "write_permissions": None}

                perms = redis_con.get(key)
                if perms:
                    perms = perms.decode('utf-8')
                    if ',' in perms:
                        perms = perms.split(',')
                    accounts[account]["write_permissions"] = perms

    return list(accounts.values())
