"""
Redis connection helper (single-node & clustered/Serverless Valkey)

Usage:
    from mojo.helpers.redis import get_connection
    r = get_connection()            # thread-safe client backed by a pool
    p = r.pipeline(transaction=False)  # per-thread pipeline for metrics/logging

Settings used (all optional; follows your existing naming):
    REDIS_URL              # if set, used verbatim (e.g., redis://... or rediss://...)
    REDIS_SERVER           # host (e.g., '127.0.0.1' or 'xxx.serverless.use1.cache.amazonaws.com')
    REDIS_PORT             # default 6379
    REDIS_DB_INDEX         # default 0
    REDIS_USERNAME         # ACL username (Serverless Valkey/Redis)
    REDIS_PASSWORD         # ACL password
    REDIS_SCHEME           # 'redis' or 'rediss' (default 'rediss')
    REDIS_MAX_CONN         # per-process pool size (default 500)
    REDIS_READ_FROM_REPLICAS  # '1'/'0' (cluster only; default '1')

Notes:
- Local single-node dev: URL usually looks like    redis://localhost:6379/0
- Cluster/Serverless prod: URL should look like    rediss://user:pass@<endpoint>:6379/0
  (TLS + ACLs; cluster will be auto-detected and RedisCluster used)
"""

from urllib.parse import quote
import redis
from redis.cluster import RedisCluster  # redis-py provides cluster client

from mojo.helpers.settings import settings

_CLIENT = None  # per-process singleton (thread-safe client; uses a connection pool underneath)


def _build_url() -> str:
    # 1) Allow an explicit URL override
    url = settings.get("REDIS_URL", None)
    if url:
        return url

    # 2) Build from individual parts
    host = settings.get("REDIS_SERVER", "localhost")
    port = int(settings.get("REDIS_PORT", 6379))
    db   = int(settings.get("REDIS_DB_INDEX", 0))
    user = settings.get("REDIS_USERNAME", None)
    pwd  = settings.get("REDIS_PASSWORD", None)
    scheme = settings.get("REDIS_SCHEME", "rediss")  # default to TLS

    if "localhost" in host:
        scheme = "redis"

    if user and pwd:
        auth = f"{quote(user)}:{quote(pwd)}@"
    elif pwd:
        auth = f":{quote(pwd)}@"
    else:
        auth = ""

    return f"{scheme}://{auth}{host}:{port}/{db}"


def _is_cluster(redis_client: "redis.Redis") -> bool:
    """Return True if the target enables cluster mode."""
    try:
        info = redis_client.info("cluster")
        return bool(info.get("cluster_enabled"))
    except Exception:
        # If INFO fails (ACL, network, etc.), assume not cluster and fall back.
        return False


def get_connection():
    """
    Returns a Redis/RedisCluster client backed by an internal connection pool.
    - Standalone (dev): redis.Redis with ConnectionPool
    - Cluster/Serverless (prod): redis.cluster.RedisCluster with ClusterConnectionPool

    The returned client is thread-safe. Create a new Pipeline per thread
    (prefer transaction=False for metrics/logging to avoid cross-slot).
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    url = _build_url()
    max_conn = int(settings.get("REDIS_MAX_CONN", 500))
    connect_timeout = float(settings.get("REDIS_CONNECT_TIMEOUT", 2))
    socket_timeout  = float(settings.get("REDIS_SOCKET_TIMEOUT", 2))

    # Start with a basic client to detect cluster (works for both redis:// and rediss://)
    base = redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
        # Pool created internally; fine for the quick probe
    )

    if _is_cluster(base):
        # Cluster-aware client (uses ClusterConnectionPool internally)
        _CLIENT = RedisCluster.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
            max_connections=max_conn,
            read_from_replicas=str(settings.get("REDIS_READ_FROM_REPLICAS", "1")) in ("1", "true", "True"),
            reinitialize_steps=5,  # resilient to topology changes
        )
    else:
        # Standalone client with an explicit ConnectionPool (controls pool size)
        pool = redis.ConnectionPool.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
            max_connections=max_conn,
        )
        _CLIENT = redis.Redis(connection_pool=pool)

    return _CLIENT
