import redis
from mojo.helpers.settings import settings
from urllib.parse import quote

REDIS_POOL = None


def get_connection():
    user = settings.get("REDIS_USERNAME")
    pwd = settings.get("REDIS_PASSWORD")
    host = settings.get("REDIS_SERVER")
    port = settings.get("REDIS_PORT", 6379)
    db = settings.get("REDIS_DB_INDEX", 0)
    # Build auth segment
    if user and pwd:
        auth = f"{quote(user)}:{quote(pwd)}@"
    elif pwd:  # password only
        auth = f":{quote(pwd)}@"
    else:
        auth = ""
    url = f"rediss://{auth}{host}:{port}/{db}"
    global REDIS_POOL
    if REDIS_POOL is None:
        REDIS_POOL = redis.ConnectionPool.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return redis.Redis(connection_pool=REDIS_POOL)
