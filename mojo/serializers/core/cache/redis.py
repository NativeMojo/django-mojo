"""
Redis Cache Backend for Django-MOJO Serializers

Distributed Redis-based cache implementation with TTL support, connection pooling,
and high availability. Provides shared caching across multiple Django processes
and servers with automatic expiration and advanced Redis features.

Key Features:
- Distributed caching across multiple processes/servers
- Native Redis TTL support with automatic expiration
- JSON serialization for cross-platform compatibility
- Connection pooling for high performance
- Configurable key prefixes for multi-tenancy
- Pipeline operations for batch operations
- Thread-safe operations
- Graceful error handling and fallback behavior
- Comprehensive statistics tracking

Usage:
    cache = RedisCacheBackend(
        host='localhost',
        port=6379,
        db=2,
        key_prefix='prod:mojo:serializer:'
    )

    # Same interface as other backends
    cache.set("Event_123_default", serialized_data, ttl=300)
    data = cache.get("Event_123_default")

Configuration:
    MOJO_SERIALIZER_CACHE = {
        'backend': 'redis',
        'redis': {
            'host': 'localhost',
            'port': 6379,
            'db': 2,
            'key_prefix': 'mojo:serializer:',
            'socket_timeout': 1.0,
            'connection_pool_kwargs': {
                'max_connections': 50
            }
        }
    }
"""

# Use ujson for optimal performance, fallback to standard json
try:
    import ujson as json
except ImportError:
    import json
import time
import threading
from typing import Any, Optional, Dict
# Use logit with graceful fallback
try:
    from mojo.helpers import logit
    logger = logit.get_logger("redis_cache", "redis_cache.log")
except Exception:
    import logging
    logger = logging.getLogger("redis_cache")

from .base import CacheBackend

# Redis imports with graceful fallback
try:
    import redis
    from redis.connection import ConnectionPool
    from redis.exceptions import RedisError, ConnectionError, TimeoutError
    HAS_REDIS = True
except ImportError:
    redis = None
    ConnectionPool = None
    RedisError = Exception
    ConnectionError = Exception
    TimeoutError = Exception
    HAS_REDIS = False


class RedisCacheBackend(CacheBackend):
    """
    Redis-based distributed cache backend with connection pooling and high availability.

    Provides high-performance distributed caching using Redis with native TTL support,
    connection pooling, JSON serialization, and comprehensive error handling.

    Features:
    - Distributed caching across multiple processes/servers
    - Native Redis TTL with automatic expiration
    - JSON serialization for cross-platform compatibility
    - Connection pooling for high performance
    - Configurable key prefixes for multi-tenancy
    - Thread-safe operations with connection sharing
    - Graceful error handling and fallback behavior
    - Pipeline operations for batch operations
    - Comprehensive monitoring and statistics
    """

    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 0,
                 key_prefix: str = 'mojo:serializer:', password: Optional[str] = None,
                 socket_timeout: float = 1.0, socket_connect_timeout: float = 1.0,
                 connection_pool_kwargs: Optional[Dict] = None, enable_stats: bool = True,
                 **kwargs):
        """
        Initialize Redis cache backend with connection pooling.

        :param host: Redis server hostname
        :param port: Redis server port
        :param db: Redis database number
        :param key_prefix: Prefix for all cache keys
        :param password: Redis authentication password
        :param socket_timeout: Socket timeout in seconds
        :param socket_connect_timeout: Connection timeout in seconds
        :param connection_pool_kwargs: Additional connection pool parameters
        :param enable_stats: Enable statistics tracking
        :param kwargs: Additional Redis connection parameters
        """
        if not HAS_REDIS:
            raise ImportError(
                "Redis backend requires the 'redis' package. "
                "Install with: pip install redis"
            )

        self.host = host
        self.port = port
        self.db = db
        self.key_prefix = key_prefix
        self.password = password
        self.socket_timeout = socket_timeout
        self.socket_connect_timeout = socket_connect_timeout
        self.enable_stats = enable_stats

        # Prepare connection pool kwargs
        pool_kwargs = {
            'host': host,
            'port': port,
            'db': db,
            'password': password,
            'socket_timeout': socket_timeout,
            'socket_connect_timeout': socket_connect_timeout,
            'decode_responses': False,  # We handle JSON decoding manually
            'max_connections': 50,      # Default max connections
        }

        if connection_pool_kwargs:
            pool_kwargs.update(connection_pool_kwargs)

        # Additional Redis client kwargs
        self.redis_kwargs = kwargs

        # Create connection pool
        try:
            self._connection_pool = ConnectionPool(**pool_kwargs)
            self._redis_client = redis.Redis(connection_pool=self._connection_pool, **self.redis_kwargs)

            # Test connection
            self._redis_client.ping()
            logger.info(f"Redis cache backend initialized: {host}:{port}/{db}")

        except Exception as e:
            logger.error(f"Failed to initialize Redis cache backend: {e}")
            raise

        # Thread-safe statistics tracking
        self._stats = {
            'hits': 0,
            'misses': 0,
            'sets': 0,
            'deletes': 0,
            'evictions': 0,  # Redis-managed evictions
            'expired_items': 0,
            'errors': 0,
            'connection_errors': 0,
            'timeout_errors': 0,
            'json_errors': 0
        }
        self._stats_lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        """
        Retrieve item from Redis cache with JSON deserialization.

        Uses Redis GET command with automatic TTL checking. Returns None
        for cache misses, expired items, or connection errors.
        """
        try:
            redis_key = f"{self.key_prefix}{key}"
            json_data = self._redis_client.get(redis_key)

            if json_data is None:
                self._increment_stat('misses')
                return None

            # Deserialize from JSON
            try:
                value = json.loads(json_data.decode('utf-8'))
                self._increment_stat('hits')
                return value
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(f"JSON decode error for key '{key}': {e}")
                self._increment_stat('json_errors')
                # Remove corrupted entry
                self._redis_client.delete(redis_key)
                self._increment_stat('misses')
                return None

        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection error for get '{key}': {e}")
            self._increment_stat('connection_errors')
            self._increment_stat('misses')
            return None

        except RedisError as e:
            logger.warning(f"Redis error for get '{key}': {e}")
            self._increment_stat('errors')
            self._increment_stat('misses')
            return None

    def set(self, key: str, value: Any, ttl: int = 0) -> bool:
        """
        Store item in Redis cache with TTL and JSON serialization.

        Uses Redis SETEX for TTL or SET for no expiration. Handles JSON
        serialization and connection errors gracefully.
        """
        if ttl == 0:
            return True  # TTL of 0 means no caching - safe default

        try:
            redis_key = f"{self.key_prefix}{key}"

            # Serialize to JSON
            try:
                json_data = json.dumps(value, default=str)  # default=str handles dates/decimals
            except (TypeError, ValueError) as e:
                logger.warning(f"JSON encode error for key '{key}': {e}")
                self._increment_stat('json_errors')
                return False

            # Store in Redis with TTL
            if ttl > 0:
                success = self._redis_client.setex(redis_key, ttl, json_data)
            else:
                success = self._redis_client.set(redis_key, json_data)

            if success:
                self._increment_stat('sets')
                return True
            else:
                self._increment_stat('errors')
                return False

        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection error for set '{key}': {e}")
            self._increment_stat('connection_errors')
            return False

        except RedisError as e:
            logger.warning(f"Redis error for set '{key}': {e}")
            self._increment_stat('errors')
            return False

    def delete(self, key: str) -> bool:
        """
        Remove specific item from Redis cache.

        Uses Redis DEL command and returns True if the key was actually deleted.
        """
        try:
            redis_key = f"{self.key_prefix}{key}"
            deleted_count = self._redis_client.delete(redis_key)

            if deleted_count > 0:
                self._increment_stat('deletes')
                return True
            return False

        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection error for delete '{key}': {e}")
            self._increment_stat('connection_errors')
            return False

        except RedisError as e:
            logger.warning(f"Redis error for delete '{key}': {e}")
            self._increment_stat('errors')
            return False

    def clear(self) -> bool:
        """
        Clear all cache items with matching key prefix.

        Uses Redis SCAN for memory-efficient key discovery and DEL for removal.
        This is safer than KEYS * on production Redis instances.
        """
        try:
            pattern = f"{self.key_prefix}*"
            deleted_count = 0

            # Use SCAN for memory-efficient key iteration
            for key in self._redis_client.scan_iter(match=pattern, count=1000):
                try:
                    if self._redis_client.delete(key):
                        deleted_count += 1
                except RedisError:
                    continue  # Skip failed deletes

            if deleted_count > 0:
                logger.info(f"Cleared {deleted_count} keys from Redis cache")

            return True

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"Redis connection error for clear: {e}")
            self._increment_stat('connection_errors')
            return False

        except RedisError as e:
            logger.error(f"Redis error for clear: {e}")
            self._increment_stat('errors')
            return False

    def stats(self) -> Dict[str, Any]:
        """
        Get comprehensive Redis cache statistics.

        Combines local operation statistics with Redis server information
        for complete monitoring and debugging capabilities.
        """
        with self._stats_lock:
            total_requests = self._stats['hits'] + self._stats['misses']
            hit_rate = (self._stats['hits'] / total_requests) if total_requests > 0 else 0.0

            base_stats = {
                # Backend identification
                'backend': 'redis',

                # Connection information
                'host': self.host,
                'port': self.port,
                'db': self.db,
                'key_prefix': self.key_prefix,

                # Performance metrics
                'hit_rate': hit_rate,
                'total_requests': total_requests,

                # Operation statistics
                **self._stats.copy()
            }

            # Add Redis server information if available
            try:
                redis_info = self._redis_client.info()
                base_stats.update({
                    'redis_version': redis_info.get('redis_version', 'unknown'),
                    'redis_memory_used': redis_info.get('used_memory', 0),
                    'redis_memory_used_human': redis_info.get('used_memory_human', '0B'),
                    'redis_connected_clients': redis_info.get('connected_clients', 0),
                    'redis_total_commands_processed': redis_info.get('total_commands_processed', 0),
                    'redis_keyspace_hits': redis_info.get('keyspace_hits', 0),
                    'redis_keyspace_misses': redis_info.get('keyspace_misses', 0),
                    'redis_evicted_keys': redis_info.get('evicted_keys', 0),
                    'redis_expired_keys': redis_info.get('expired_keys', 0),
                    'connection_pool_created_connections': self._connection_pool.created_connections,
                    'connection_pool_available_connections': len(self._connection_pool._available_connections),
                    'connection_pool_in_use_connections': len(self._connection_pool._in_use_connections),
                })

                # Calculate Redis cache hit rate
                redis_hits = redis_info.get('keyspace_hits', 0)
                redis_misses = redis_info.get('keyspace_misses', 0)
                redis_total = redis_hits + redis_misses
                if redis_total > 0:
                    base_stats['redis_hit_rate'] = redis_hits / redis_total
                else:
                    base_stats['redis_hit_rate'] = 0.0

            except Exception as e:
                logger.warning(f"Could not get Redis info: {e}")
                base_stats['redis_info_error'] = str(e)

            return base_stats

    def ping(self) -> bool:
        """
        Test Redis connection health.

        Sends Redis PING command and returns True if Redis responds.
        """
        try:
            return self._redis_client.ping()
        except Exception:
            return False

    def get_key_count(self) -> int:
        """
        Get approximate count of keys with our prefix.

        Uses Redis EVAL with Lua script for efficient counting.
        """
        try:
            # Lua script to count keys with prefix
            lua_script = """
            local count = 0
            local keys = redis.call('SCAN', 0, 'MATCH', ARGV[1], 'COUNT', 1000)
            for i = 1, #keys[2] do
                count = count + 1
            end
            return count
            """

            pattern = f"{self.key_prefix}*"
            count = self._redis_client.eval(lua_script, 0, pattern)
            return count or 0

        except Exception as e:
            logger.warning(f"Error getting key count: {e}")
            return 0

    def batch_get(self, keys: list) -> Dict[str, Any]:
        """
        Get multiple keys in a single Redis operation using pipeline.

        More efficient than individual get operations for multiple keys.
        """
        if not keys:
            return {}

        try:
            redis_keys = [f"{self.key_prefix}{key}" for key in keys]

            # Use pipeline for batch operations
            pipe = self._redis_client.pipeline(transaction=False)
            for redis_key in redis_keys:
                pipe.get(redis_key)

            results = pipe.execute()

            # Process results
            batch_results = {}
            for i, (original_key, json_data) in enumerate(zip(keys, results)):
                if json_data is not None:
                    try:
                        value = json.loads(json_data.decode('utf-8'))
                        batch_results[original_key] = value
                        self._increment_stat('hits')
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._increment_stat('json_errors')
                        self._increment_stat('misses')
                else:
                    self._increment_stat('misses')

            return batch_results

        except Exception as e:
            logger.warning(f"Error in batch_get: {e}")
            self._increment_stat('errors')
            return {}

    def batch_set(self, items: Dict[str, Any], ttl: int = 0) -> bool:
        """
        Set multiple keys in a single Redis operation using pipeline.

        More efficient than individual set operations for multiple keys.
        """
        if not items or ttl == 0:
            return True

        try:
            # Use pipeline for batch operations
            pipe = self._redis_client.pipeline(transaction=False)

            for key, value in items.items():
                redis_key = f"{self.key_prefix}{key}"

                try:
                    json_data = json.dumps(value, default=str)
                    if ttl > 0:
                        pipe.setex(redis_key, ttl, json_data)
                    else:
                        pipe.set(redis_key, json_data)
                except (TypeError, ValueError):
                    logger.warning(f"JSON encode error for key '{key}' in batch_set")
                    self._increment_stat('json_errors')
                    continue

            results = pipe.execute()

            # Count successful operations
            success_count = sum(1 for result in results if result)
            self._stats['sets'] += success_count

            return len(results) == success_count

        except Exception as e:
            logger.warning(f"Error in batch_set: {e}")
            self._increment_stat('errors')
            return False

    def close(self):
        """
        Close Redis connection and cleanup resources.

        Closes connection pool and resets connection state.
        """
        try:
            if self._connection_pool:
                self._connection_pool.disconnect()
                logger.info("Redis cache backend connections closed")
        except Exception as e:
            logger.warning(f"Error closing Redis connections: {e}")
        finally:
            self._connection_pool = None
            self._redis_client = None

    def _increment_stat(self, stat_name: str):
        """
        Thread-safe statistics increment.

        :param stat_name: Statistics counter name
        """
        if self.enable_stats:
            with self._stats_lock:
                self._stats[stat_name] += 1

    def __del__(self):
        """
        Cleanup on object destruction.
        """
        try:
            self.close()
        except Exception:
            pass  # Ignore cleanup errors during destruction
