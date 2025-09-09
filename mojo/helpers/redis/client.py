import redis
from mojo.helpers.settings import settings

def get_redis_config():
    """Get Redis configuration with decode_responses always enabled"""
    # Start with safe defaults
    config = {
        'host': 'localhost',
        'port': 6379,
        'db': 0,
        'decode_responses': True,  # Always decode responses to strings
    }

    # Check if complete REDIS_DB dict is provided
    redis_db_setting = settings.get('REDIS_DB', None)

    if redis_db_setting and isinstance(redis_db_setting, dict):
        # Use the provided dictionary, but ensure decode_responses=True
        config.update(redis_db_setting)
        config['decode_responses'] = True
    else:
        # Use individual settings
        config.update({
            'host': settings.get('REDIS_HOST', 'localhost'),
            'port': settings.get('REDIS_PORT', 6379),
            'db': settings.get('REDIS_DATABASE', 0),
        })

        # Add password if provided
        password = settings.get('REDIS_PASSWORD', None)
        if password:
            config['password'] = password

    return config
REDIS_POOL = None

def get_connection():
    """
    Get a Redis connection using shared connection pooling.

    Returns:
        Redis client instance
    """
    global REDIS_POOL
    if REDIS_POOL is None:
        config = get_redis_config()
        REDIS_POOL = redis.ConnectionPool(**config)
    return redis.Redis(connection_pool=REDIS_POOL)
