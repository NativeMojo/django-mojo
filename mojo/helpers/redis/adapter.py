"""
Redis connection pooling and typed operations for the Mojo framework.
Provides both simple connections and a full-featured adapter with type safety.
"""
import json
from typing import Any, Dict, List, Optional, Tuple, Union
from contextlib import contextmanager

import redis

from mojo.helpers import logit
from .client import get_connection


class RedisAdapter:
    """
    Redis adapter with typed operations and automatic serialization.
    Uses the framework's connection pooling via get_connection().
    """

    def get_client(self):
        """
        Get a Redis client instance using framework connection pooling.

        Returns:
            Redis client
        """
        return get_connection()

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
        except Exception as e:
            logit.error(f"Pipeline execution failed: {e}")
            raise
        finally:
            pipe.reset()

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

        return self.get_client().xadd(
            stream, serialized, id=id, maxlen=maxlen, approximate=False
        )

    def xreadgroup(self, group: str, consumer: str, streams: Dict[str, str],
                   count: Optional[int] = None, block: Optional[int] = None) -> List[Tuple[str, List]]:
        """
        Read from streams as part of a consumer group.

        Args:
            group: Consumer group name
            consumer: Consumer name
            streams: Dict of stream names to IDs (use '>' for new messages)
            count: Max messages to return
            block: Block for this many milliseconds (None = don't block)

        Returns:
            List of (stream_name, messages) tuples with decoded strings
        """
        return self.get_client().xreadgroup(
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
        return self.get_client().xack(stream, group, *ids)

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
            List of claimed messages with decoded strings
        """
        return self.get_client().xclaim(
            stream, group, consumer, min_idle, *ids, **kwargs
        )

    def xpending(self, stream: str, group: str, start: Optional[str] = None, end: Optional[str] = None,
                 count: Optional[int] = None, consumer: Optional[str] = None) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Get pending message info for a consumer group.

        Args:
            stream: Stream key
            group: Consumer group name
            start: Start ID for range query (optional, enables detailed mode)
            end: End ID for range query (optional)
            count: Max messages to return (optional)
            consumer: Filter by consumer (optional)

        Returns:
            Summary dict if no range specified, or list of detailed pending message dicts
        """
        client = self.get_client()
        if start is not None:
            # Detailed pending info with range
            # Prefer redis-py's xpending_range when available for structured output
            detailed_messages: List[Dict[str, Any]] = []
            used_structured_api = False
            try:
                if hasattr(client, "xpending_range"):
                    # redis-py >= 4 provides xpending_range(name, groupname, min, max, count, consumername=None)
                    res = client.xpending_range(
                        stream, group, start, end or '+', count or 10,
                        consumername=consumer
                    )
                    used_structured_api = True
                    # Normalize to our schema
                    for item in res or []:
                        # redis-py uses keys like 'message_id', 'consumer', 'idle', 'times_delivered'
                        msg_id = item.get('message_id')
                        cons = item.get('consumer')
                        idle = item.get('idle')
                        deliveries = item.get('times_delivered')
                        detailed_messages.append({
                            'message_id': msg_id,
                            'consumer': cons,
                            'idle_time': int(idle or 0),
                            'delivery_count': int(deliveries or 0),
                        })
            except Exception as e:
                logit.debug(f"xpending_range failed for {stream}/{group}: {e}")
                used_structured_api = False
                detailed_messages = []

            if not used_structured_api:
                # Fallback: raw XPENDING command returning list entries
                args = [stream, group, start, end or '+', count or 10]
                if consumer:
                    args.append(consumer)
                try:
                    result = client.execute_command('XPENDING', *args)
                except Exception as e:
                    # Handle case where stream/group doesn't exist or other Redis errors
                    logit.debug(f"XPENDING detailed query failed for {stream}/{group}: {e}")
                    return []

                # Convert detailed response to structured format
                # Each item in result is: [message_id, consumer, idle_time, delivery_count]
                if result:
                    for item in result:
                        try:
                            if isinstance(item, (list, tuple)) and len(item) >= 4:
                                msg_id = item[0]
                                cons = item[1]
                                idle = int(item[2] or 0)
                                deliveries = int(item[3] or 0)
                                detailed_messages.append({
                                    'message_id': msg_id,
                                    'consumer': cons,
                                    'idle_time': idle,
                                    'delivery_count': deliveries
                                })
                        except Exception as ie:
                            logit.debug(f"Failed to parse XPENDING detailed item {item}: {ie}")

            return detailed_messages
        else:
            # Basic pending summary
            try:
                result = client.xpending(stream, group)
                return result
            except Exception as e:
                # Handle case where stream/group doesn't exist or other Redis errors
                logit.debug(f"XPENDING summary query failed for {stream}/{group}: {e}")
                return {'pending': 0, 'min_idle_time': 0, 'max_idle_time': 0, 'consumers': []}

    def xinfo_stream(self, stream: str) -> Dict[str, Any]:
        """
        Get stream information.

        Args:
            stream: Stream key

        Returns:
            Stream info dict with decoded strings
        """
        return self.get_client().xinfo_stream(stream)

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
            True if created, False if already exists
        """
        try:
            self.get_client().xgroup_create(
                stream, group, id=id, mkstream=mkstream
            )
            return True
        except Exception as e:
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
        return self.get_client().zadd(key, mapping, **kwargs)

    def zpopmin(self, key: str, count: int = 1) -> List[Tuple[str, float]]:
        """
        Pop members with lowest scores.

        Args:
            key: ZSET key
            count: Number of members to pop

        Returns:
            List of (member, score) tuples with decoded member names
        """
        return self.get_client().zpopmin(key, count)

    def zcard(self, key: str) -> int:
        """
        Get sorted set cardinality.

        Args:
            key: ZSET key

        Returns:
            Number of members
        """
        return self.get_client().zcard(key)

    def zscore(self, key: str, member: str) -> Optional[float]:
        """
        Get the score of a member in a sorted set.

        Args:
            key: ZSET key
            member: Member whose score to retrieve

        Returns:
            The score as a float, or None if the member does not exist
        """
        return self.get_client().zscore(key, member)

    # List operations (Plan B)
    def rpush(self, key: str, *values: Any) -> int:
        """
        Push one or more values to the right end of a list.

        Args:
            key: List key
            *values: One or more values to push

        Returns:
            The length of the list after the push operations
        """
        return self.get_client().rpush(key, *values)

    def brpop(self, keys: List[str], timeout: int = 1) -> Optional[Tuple[str, str]]:
        """
        Blocking right pop on one or more lists.

        Args:
            keys: List of keys to BRPOP from (first non-empty wins)
            timeout: Timeout in seconds (0 = block indefinitely)

        Returns:
            (key, value) tuple as strings, or None if timed out
        """
        return self.get_client().brpop(keys, timeout=timeout)

    def llen(self, key: str) -> int:
        """
        Get the length of a list.

        Args:
            key: List key

        Returns:
            Length of the list
        """
        return self.get_client().llen(key)

    # ZSET helpers (Plan B)
    def zrem(self, key: str, member: str) -> int:
        """
        Remove a member from a sorted set.

        Args:
            key: ZSET key
            member: Member to remove

        Returns:
            Number of members removed (0 or 1)
        """
        return self.get_client().zrem(key, member)

    def zrangebyscore(self, key: str, min_score: float, max_score: float, limit: Optional[int] = None) -> List[str]:
        """
        Return members in a sorted set within the given scores.

        Args:
            key: ZSET key
            min_score: Minimum score (inclusive)
            max_score: Maximum score (inclusive)
            limit: Optional maximum number of members to return

        Returns:
            List of members as strings
        """
        client = self.get_client()
        if limit is not None:
            return client.zrangebyscore(key, min_score, max_score, start=0, num=int(limit))
        else:
            return client.zrangebyscore(key, min_score, max_score)

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

        return self.get_client().hset(key, mapping=serialized)

    def hget(self, key: str, field: str) -> Optional[str]:
        """
        Get hash field value.

        Args:
            key: Hash key
            field: Field name

        Returns:
            Field value or None
        """
        return self.get_client().hget(key, field)

    def hgetall(self, key: str) -> Dict[str, str]:
        """
        Get all hash fields.

        Args:
            key: Hash key

        Returns:
            Dict of field -> value
        """
        return self.get_client().hgetall(key)

    def hdel(self, key: str, *fields) -> int:
        """
        Delete hash fields.

        Args:
            key: Hash key
            *fields: Field names to delete

        Returns:
            Number of fields deleted
        """
        return self.get_client().hdel(key, *fields)

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

        result = self.get_client().set(
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
        return self.get_client().get(key)

    def delete(self, *keys) -> int:
        """
        Delete keys.

        Args:
            *keys: Key names to delete

        Returns:
            Number of keys deleted
        """
        return self.get_client().delete(*keys)

    def expire(self, key: str, seconds: int) -> bool:
        """
        Set key expiration.

        Args:
            key: Key name
            seconds: TTL in seconds

        Returns:
            True if expiration was set
        """
        return self.get_client().expire(key, seconds)

    def pexpire(self, key: str, milliseconds: int) -> bool:
        """
        Set key expiration in milliseconds.

        Args:
            key: Key name
            milliseconds: TTL in milliseconds

        Returns:
            True if expiration was set
        """
        return self.get_client().pexpire(key, milliseconds)

    def ttl(self, key: str) -> int:
        """
        Get key TTL in seconds.

        Args:
            key: Key name

        Returns:
            TTL in seconds (-2 if doesn't exist, -1 if no expiry)
        """
        return self.get_client().ttl(key)

    def exists(self, *keys) -> int:
        """
        Check if keys exist.

        Args:
            *keys: Key names to check

        Returns:
            Number of keys that exist
        """
        return self.get_client().exists(*keys)

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

        return self.get_client().publish(channel, message)

    def pubsub(self):
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
            return self.get_client().ping()
        except Exception:
            return False


# Framework-level singleton
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
    _default_adapter = None
