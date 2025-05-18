from . import utils
from mojo.helpers import redis
from mojo.helpers.settings import settings
import datetime

def record(slug, when=None, count=0, group=None, category=None,
                   min_granulariy="hours", max_granularity="years", *args):
    """
    Records metrics in Redis by incrementing counters for various time granularities.

    Args:
        slug (str): The base identifier for the metric.
        when (datetime): The time at which the event occurred.
        count (int, optional): The count to increment the metric by. Defaults to 0.
        group (optional): An unused parameter for future categorization.
        category (optional): Put your slug into a category for easy group of metrics.
        min_granulariy (str, optional): The minimum time granularity (e.g., "hours").
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
        add_category_slug(category, slug, pipeline)
    add_metrics_slug(slug, pipeline)
    # Generate granularities
    granularities = utils.generate_granularities(min_granulariy, max_granularity)
    # Process each granularity
    for granularity in granularities:
        # Generate slug for the current granularity
        generated_slug = utils.generate_slug(slug, when, granularity, *args)
        # Add count to the slug in Redis
        pipeline.incr(generated_slug, count)
        exp_at = utils.get_expires_at(granularity, slug, category)
        if exp_at:
            pipeline.expireat(exp_at)
    pipeline.execute()


def add_metrics_slug(slug, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(f"mets:slugs", slug)


def add_category_slug(category, slug, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(utils.generate_category_slug(category), slug)
    redis_con.sadd(utils.CATEGORY_KEY, category)


def get_category_slugs(category, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    return [s.decode() for s in redis_con.smembers(utils.generate_category_slug(category))]


def get_categories(redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    return redis_con.smembers(utils.CATEGORY_KEY)


def get_metrics(slug, dt_start, dt_end, granularity, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    dr_slugs = utils.generate_slugs_for_range(slug, dt_start, dt_end, granularity)
    return [int(met) if met is not None else 0 for met in redis_con.mget(dr_slugs)]
