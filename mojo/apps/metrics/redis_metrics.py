from . import utils
from mojo.helpers import redis
import datetime
from objict import objict


def record(slug, when=None, count=1, group=None, category=None, account="global",
                   min_granularity="hours", max_granularity="years", *args):
    """
    Records metrics in Redis by incrementing counters for various time granularities.

    Args:
        slug (str): The base identifier for the metric.
        when (datetime): The time at which the event occurred.
        count (int, optional): The count to increment the metric by. Defaults to 0.
        group (optional): An unused parameter for future categorization.
        category (optional): Put your slug into a category for easy group of metrics.
        account (optional): Put a specific account other then GLOBAL
        min_granularity (str, optional): The minimum time granularity (e.g., "hours").
            Defaults to "hours".
        max_granularity (str, optional): The maximum time granularity (e.g., "years").
            Defaults to "years".
        *args: Additional arguments to be used in slug generation.

    Returns:
        None
    """
    if when is None:
        # TODO add settings.METRICS_TIMEZONE
        when = datetime.datetime.now()
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
        generated_slug = utils.generate_slug(slug, when, granularity, account, *args)
        # Add count to the slug in Redis
        pipeline.incr(generated_slug, count)
        exp_at = utils.get_expires_at(granularity, slug, category)
        if exp_at:
            pipeline.expireat(generated_slug, exp_at)
    pipeline.execute()


def fetch(slug, dt_start=None, dt_end=None, granularity="hours", redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    if isinstance(slug, (list, set)):
        resp = objict()
        for s in slug:
            resp[s] = fetch(s, dt_start, dt_end, granularity, redis_con, account)
        return resp
    dr_slugs = utils.generate_slugs_for_range(slug, dt_start, dt_end, granularity, account)
    return [int(met) if met is not None else 0 for met in redis_con.mget(dr_slugs)]


def add_metrics_slug(slug, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(f"mets:{account}:slugs", slug)


def add_category_slug(category, slug, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(utils.generate_category_slug(account, category), slug)
    redis_con.sadd(utils.generate_category_key(account), category)


def get_category_slugs(category, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() for s in redis_con.smembers(utils.generate_category_slug(account, category))}


def delete_category(category, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    category_slug = utils.generate_category_slug(account, category)
    pipeline = redis_con.pipeline()
    pipeline.delete(category_slug)  # Deletes the entire set
    pipeline.srem(utils.generate_category_key(account), category)  # Remove the category name from index
    pipeline.execute()


def get_categories(redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() for s in redis_con.smembers(utils.generate_category_key(account))}


def fetch_by_category(category, dt_start=None, dt_end=None, granularity="hours", redis_con=None, account="global"):
    return fetch(get_category_slugs(category, redis_con, account))
