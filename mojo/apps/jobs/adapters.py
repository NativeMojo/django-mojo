"""
Redis adapter for the jobs system.
Handles connection management and provides typed operations.
"""
import json
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from contextlib import contextmanager

import redis
from redis import ConnectionPool, Redis
from redis.exceptions import RedisError, ConnectionError, TimeoutError
from django.conf import settings

from mojo.helpers import logit


class RedisAdapter:
    """
    Redis adapter with connection pooling and typed operations for jobs system.
    """

    def __init__(self, url: Optional[str] = None, **kwargs):
        """
        Initialize Redis adapter with connection pooling.

        Args:
            url: Redis URL (defaults to JOBS_REDIS_URL from settings)
            **kwargs: Additional Redis client options
        """
        self.url = url or getattr(settings, 'JOBS_REDIS_URL', 'redis://localhost:6379/0')
        self.pool = None
        self.client = None
        self.connect_timeout = kwargs.pop('connect_timeout', 5)
        self.socket_timeout = kwargs.pop('socket_timeout', 5)
        self.retry_on_timeout = kwargs.pop('retry_on_timeout', True)
        self.max_retries = kwargs.pop('max_retries', 3)
        self.kwargs = kwargs
        self._initialize_pool()

    def _initialize_pool(self):
        """Initialize the connection pool."""
        try:
            self.pool = ConnectionPool.from_url(
                self.url,
                socket_connect_timeout=self.connect_timeout,
                socket_timeout=self.socket_timeout,
                retry_on_timeout=self.retry_on_timeout,
                **self.kwargs
            )
            self.client = Redis(connection_pool=self.pool)
            # Test connection
            self.client.ping()
            logit.info(f"Redis adapter connected to {self.url}")
        except RedisError as e:
            logit.error(f"Failed to initialize Redis pool: {e}")
            raise

    def get_client(self) -> Redis:
        """
        Get a Redis client instance.

        Returns:
            Redis client
        """
        if not self.client:
            self._initialize_pool()
        return self.client

    @contextmanager
    def pipeline(self, transaction: bool = True):
        """
        Context manager for Redis pipeline operations.

        Args:
            transaction: Whether to use MULTI/EXEC transaction

        Yields:
            Redis pipeline object
        """
        pipe = self.get_client().pipeline(transaction=transaction)
        try:
            yield pipe
            pipe.execute()
        except RedisError as e:
            logit.error(f"Pipeline execution failed: {e}")
            raise
        finally:
            pipe.reset()

    def _retry_operation(self, func, *args, **kwargs) -> Any:
        """
        Retry a Redis operation with exponential backoff.

        Args:
            func: Function to call
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            RedisError after max retries
        """
        last_error = None
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except (ConnectionError, TimeoutError) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    wait_time = min(0.1 * (2 ** attempt), 1.0)  # Cap at 1 second
                    logit.warn(f"Redis operation failed (attempt {attempt + 1}/{self.max_retries}), "
                              f"retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                    # Try to reconnect
                    try:
                        self._initialize_pool()
                    except Exception:
                        pass  # Will retry with next attempt

        logit.error(f"Redis operation failed after {self.max_retries} attempts")
        raise last_error

    # Stream operations
    def xadd(self, stream: str, fields: Dict[str, Any], id: str = '*',
             maxlen: Optional[int] = None) -> str:
        """
        Add entry to a stream.

        Args:
            stream: Stream key
            fields: Field-value pairs
            id: Entry ID (default '*' for auto-generation)
            maxlen: Trim stream to approximately this length

        Returns:
            Stream entry ID
        """
        # Serialize complex values to JSON
        serialized = {}
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                serialized[k] = json.dumps(v)
            else:
                serialized[k] = v

        return self._retry_operation(
            self.get_client().xadd,
            stream, serialized, id=id, maxlen=maxlen, approximate=True
        )

    def xreadgroup(self, group: str, consumer: str, streams: Dict[str, str],
                   count: Optional[int] = None, block: Optional[int] = None) -> List[Tuple]:
        """
        Read from streams as part of a consumer group.

        Args:
            group: Consumer group name
            consumer: Consumer name
            streams: Dict of stream names to IDs (use '>' for new messages)
            count: Max messages to return
            block: Block for this many milliseconds (None = don't block)

        Returns:
            List of (stream_name, messages) tuples
        """
        return self._retry_operation(
            self.get_client().xreadgroup,
            group, consumer, streams, count=count, block=block
        )

    def xack(self, stream: str, group: str, *ids) -> int:
        """
        Acknowledge messages in a stream.

        Args:
            stream: Stream key
            group: Consumer group name
            *ids: Message IDs to acknowledge

        Returns:
            Number of messages acknowledged
        """
        return self._retry_operation(
            self.get_client().xack,
            stream, group, *ids
        )

    def xclaim(self, stream: str, group: str, consumer: str, min_idle: int,
               *ids, **kwargs) -> List:
        """
        Claim pending messages.

        Args:
            stream: Stream key
            group: Consumer group name
            consumer: Consumer claiming the messages
            min_idle: Minimum idle time in milliseconds
            *ids: Message IDs to claim
            **kwargs: Additional options

        Returns:
            List of claimed messages
        """
        return self._retry_operation(
            self.get_client().xclaim,
            stream, group, consumer, min_idle, *ids, **kwargs
        )

    def xpending(self, stream: str, group: str) -> Dict:
        """
        Get pending message summary for a consumer group.

        Args:
            stream: Stream key
            group: Consumer group name

        Returns:
            Pending message summary
        """
        return self._retry_operation(
            self.get_client().xpending,
            stream, group
        )

    def xinfo_stream(self, stream: str) -> Dict:
        """
        Get stream information.

        Args:
            stream: Stream key

        Returns:
            Stream info dict
        """
        return self._retry_operation(
            self.get_client().xinfo_stream,
            stream
        )

    def xgroup_create(self, stream: str, group: str, id: str = '0',
                      mkstream: bool = True) -> bool:
        """
        Create a consumer group.

        Args:
            stream: Stream key
            group: Consumer group name
            id: Starting message ID
            mkstream: Create stream if it doesn't exist

        Returns:
            True if created
        """
        try:
            self._retry_operation(
                self.get_client().xgroup_create,
                stream, group, id=id, mkstream=mkstream
            )
            return True
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                # Group already exists
                return False
            raise

    # ZSET operations
    def zadd(self, key: str, mapping: Dict[str, float], **kwargs) -> int:
        """
        Add members to a sorted set.

        Args:
            key: ZSET key
            mapping: Dict of member -> score
            **kwargs: Additional options (NX, XX, CH, INCR)

        Returns:
            Number of elements added
        """
        return self._retry_operation(
            self.get_client().zadd,
            key, mapping, **kwargs
        )

    def zpopmin(self, key: str, count: int = 1) -> List[Tuple[str, float]]:
        """
        Pop members with lowest scores.

        Args:
            key: ZSET key
            count: Number of members to pop

        Returns:
            List of (member, score) tuples
        """
        return self._retry_operation(
            self.get_client().zpopmin,
            key, count
        )

    def zcard(self, key: str) -> int:
        """
        Get sorted set cardinality.

        Args:
            key: ZSET key

        Returns:
            Number of members
        """
        return self._retry_operation(
            self.get_client().zcard,
            key
        )

    # Hash operations
    def hset(self, key: str, mapping: Dict[str, Any]) -> int:
        """
        Set hash fields.

        Args:
            key: Hash key
            mapping: Field-value pairs

        Returns:
            Number of fields added
        """
        # Serialize complex values
        serialized = {}
        for k, v in mapping.items():
            if v is None:
                serialized[k] = ''
            elif isinstance(v, bool):
                serialized[k] = '1' if v else '0'
            elif isinstance(v, (dict, list)):
                serialized[k] = json.dumps(v)
            else:
                serialized[k] = str(v)

        return self._retry_operation(
            self.get_client().hset,
            key, mapping=serialized
        )

    def hget(self, key: str, field: str) -> Optional[str]:
        """
        Get hash field value.

        Args:
            key: Hash key
            field: Field name

        Returns:
            Field value or None
        """
        value = self._retry_operation(
            self.get_client().hget,
            key, field
        )
        return value.decode('utf-8') if value else None

    def hgetall(self, key: str) -> Dict[str, str]:
        """
        Get all hash fields.

        Args:
            key: Hash key

        Returns:
            Dict of field -> value
        """
        raw = self._retry_operation(
            self.get_client().hgetall,
            key
        )
        # Decode bytes to strings
        return {
            k.decode('utf-8'): v.decode('utf-8')
            for k, v in raw.items()
        }

    def hdel(self, key: str, *fields) -> int:
        """
        Delete hash fields.

        Args:
            key: Hash key
            *fields: Field names to delete

        Returns:
            Number of fields deleted
        """
        return self._retry_operation(
            self.get_client().hdel,
            key, *fields
        )

    # Key operations
    def set(self, key: str, value: Any, ex: Optional[int] = None,
            px: Optional[int] = None, nx: bool = False, xx: bool = False) -> bool:
        """
        Set a key value.

        Args:
            key: Key name
            value: Value to set
            ex: Expire time in seconds
            px: Expire time in milliseconds
            nx: Only set if key doesn't exist
            xx: Only set if key exists

        Returns:
            True if set, False otherwise
        """
        if isinstance(value, (dict, list)):
            value = json.dumps(value)

        result = self._retry_operation(
            self.get_client().set,
            key, value, ex=ex, px=px, nx=nx, xx=xx
        )
        return result is True or (isinstance(result, bytes) and result == b'OK')

    def get(self, key: str) -> Optional[str]:
        """
        Get a key value.

        Args:
            key: Key name

        Returns:
            Value or None
        """
        value = self._retry_operation(
            self.get_client().get,
            key
        )
        return value.decode('utf-8') if value else None

    def delete(self, *keys) -> int:
        """
        Delete keys.

        Args:
            *keys: Key names to delete

        Returns:
            Number of keys deleted
        """
        return self._retry_operation(
            self.get_client().delete,
            *keys
        )

    def expire(self, key: str, seconds: int) -> bool:
        """
        Set key expiration.

        Args:
            key: Key name
            seconds: TTL in seconds

        Returns:
            True if expiration was set
        """
        return self._retry_operation(
            self.get_client().expire,
            key, seconds
        )

    def pexpire(self, key: str, milliseconds: int) -> bool:
        """
        Set key expiration in milliseconds.

        Args:
            key: Key name
            milliseconds: TTL in milliseconds

        Returns:
            True if expiration was set
        """
        return self._retry_operation(
            self.get_client().pexpire,
            key, milliseconds
        )

    def ttl(self, key: str) -> int:
        """
        Get key TTL in seconds.

        Args:
            key: Key name

        Returns:
            TTL in seconds (-2 if doesn't exist, -1 if no expiry)
        """
        return self._retry_operation(
            self.get_client().ttl,
            key
        )

    def exists(self, *keys) -> int:
        """
        Check if keys exist.

        Args:
            *keys: Key names to check

        Returns:
            Number of keys that exist
        """
        return self._retry_operation(
            self.get_client().exists,
            *keys
        )

    # Pub/Sub operations
    def publish(self, channel: str, message: Union[str, Dict]) -> int:
        """
        Publish message to a channel.

        Args:
            channel: Channel name
            message: Message to publish

        Returns:
            Number of subscribers that received the message
        """
        if isinstance(message, dict):
            message = json.dumps(message)

        return self._retry_operation(
            self.get_client().publish,
            channel, message
        )

    def pubsub(self) -> redis.client.PubSub:
        """
        Get a pub/sub connection.

        Returns:
            PubSub object
        """
        return self.get_client().pubsub()

    # Utility methods
    def ping(self) -> bool:
        """
        Test Redis connection.

        Returns:
            True if connected
        """
        try:
            return self._retry_operation(self.get_client().ping)
        except Exception:
            return False

    def close(self):
        """Close the connection pool."""
        if self.pool:
            self.pool.disconnect()
            self.pool = None
            self.client = None
            logit.info("Redis adapter connection closed")

    def __del__(self):
        """Cleanup on deletion."""
        self.close()


# Module-level singleton
_default_adapter = None


def get_adapter() -> RedisAdapter:
    """
    Get the default Redis adapter instance.

    Returns:
        RedisAdapter instance
    """
    global _default_adapter
    if not _default_adapter:
        _default_adapter = RedisAdapter()
    return _default_adapter


def reset_adapter():
    """Reset the default adapter (useful for testing)."""
    global _default_adapter
    if _default_adapter:
        _default_adapter.close()
        _default_adapter = None
