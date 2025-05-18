from redis import ConnectionPool, StrictRedis
from mojo.helpers.settings import settings
REDIS_POOL = None


def get_connection():
    global REDIS_POOL
    if REDIS_POOL is None:
        REDIS_POOL = ConnectionPool(**settings.REDIS_DB)
    return StrictRedis(connection_pool=REDIS_POOL)
