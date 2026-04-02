"""
Realtime manager functions - stateless interface for Django to interact with WebSocket system.

All connection state, user online status, and messaging is handled through Redis.
These functions provide a clean API for Django views/models to send messages,
check online status, and manage realtime connections.
"""

import json
import time
import uuid

def get_redis():
    from mojo.helpers.redis.client import get_connection
    return get_connection()



def broadcast(message_data):
    """
    Broadcast message to all connected clients.

    Args:
        message_data: Dict with message content
    """
    redis_client = get_redis()
    message = {
        "type": "broadcast",
        "data": message_data,
        "timestamp": time.time()
    }
    redis_client.publish("realtime:broadcast", json.dumps(message))


def publish_topic(topic, message_data):
    """
    Publish message to specific topic subscribers.

    Args:
        topic: Topic name (e.g., "user:123", "general")
        message_data: Dict with message content
    """
    redis_client = get_redis()
    message = {
        "type": "topic_message",
        "topic": topic,
        "data": message_data,
        "timestamp": time.time()
    }
    redis_client.publish(f"realtime:topic:{topic}", json.dumps(message))


def send_to_user(user_type, user_id, message_data):
    """
    Send direct message to specific user (all their connections).

    Args:
        user_type: Type of user (e.g., "user", "customer")
        user_id: User's ID
        message_data: Dict with message content
    """
    connections = get_user_connections(user_type, user_id)
    for conn_id in connections:
        send_to_connection(conn_id, message_data)


def send_to_connection(connection_id, message_data):
    """
    Send message to specific connection.

    Args:
        connection_id: Unique connection identifier
        message_data: Dict with message content
    """
    redis_client = get_redis()
    message = {
        "type": "direct_message",
        "data": message_data,
        "timestamp": time.time()
    }
    redis_client.publish(f"realtime:messages:{connection_id}", json.dumps(message))


def send_event_to_user(user_type, user_id, event_data):
    """
    Send an event directly to a user's connections without wrapping.

    Unlike send_to_user (which wraps in {"type": "message", "data": ...}),
    this sends event_data as-is to the client. Use this when the payload
    already has a meaningful ``type`` field (e.g., "assistant_response")
    and you want the client to receive exactly that shape.

    Args:
        user_type: Type of user (e.g., "user")
        user_id: User's ID
        event_data: Dict sent directly to the client (must include "type")
    """
    connections = get_user_connections(user_type, user_id)
    for conn_id in connections:
        send_event_to_connection(conn_id, event_data)


def send_event_to_connection(connection_id, event_data):
    """
    Send an event directly to a connection without wrapping.

    The client receives event_data as-is, not nested inside
    {"type": "message", "data": ...}.

    Args:
        connection_id: Unique connection identifier
        event_data: Dict sent directly to the client
    """
    redis_client = get_redis()
    message = {
        "type": "direct_event",
        "data": event_data,
        "timestamp": time.time()
    }
    redis_client.publish(f"realtime:messages:{connection_id}", json.dumps(message))


def is_online(user_type, user_id):
    """
    Check if user is currently online.

    Args:
        user_type: Type of user (e.g., "user", "customer")
        user_id: User's ID

    Returns:
        bool: True if user has active connections
    """
    redis_client = get_redis()
    key = f"realtime:online:{user_type}:{user_id}"
    return redis_client.exists(key) > 0


def get_auth_count(user_type=None):
    """
    Get count of authenticated connections.

    Args:
        user_type: Optional filter by user type

    Returns:
        int: Number of authenticated connections
    """
    redis_client = get_redis()
    if user_type:
        pattern = f"realtime:online:{user_type}:*"
    else:
        pattern = "realtime:online:*"
    return len(redis_client.keys(pattern))


def get_user_connections(user_type, user_id):
    """
    Get all connection IDs for a user.

    Supports both legacy JSON string value and the newer Redis Set value.

    Args:
        user_type: Type of user (e.g., "user", "customer")
        user_id: User's ID

    Returns:
        list: List of connection IDs (strings)
    """
    redis_client = get_redis()
    key = f"realtime:online:{user_type}:{user_id}"

    # Prefer set semantics
    try:
        key_type = redis_client.type(key)
        if isinstance(key_type, (bytes, bytearray)):
            key_type = key_type.decode()
    except Exception:
        key_type = None

    if key_type == "set":
        members = redis_client.smembers(key) or set()
        return [
            m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
            for m in members
        ]

    # Fallback to legacy JSON string
    data = redis_client.get(key)
    if not data:
        return []
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode()
        except Exception:
            return []
    try:
        user_data = json.loads(data)
        ids = user_data.get("connection_ids", [])
        # Ensure strings
        return [i.decode() if isinstance(i, (bytes, bytearray)) else str(i) for i in ids]
    except Exception:
        return []


def get_topic_subscribers(topic):
    """
    Get connection IDs subscribed to topic.

    Args:
        topic: Topic name

    Returns:
        list: List of connection IDs subscribed to topic (strings)
    """
    redis_client = get_redis()
    members = redis_client.smembers(f"realtime:topic:{topic}") or set()
    return [
        m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
        for m in members
    ]


def get_redis_info(connection_id):
    """
    Get information about a specific connection.

    Args:
        connection_id: Unique connection identifier

    Returns:
        dict: Connection information or None if not found
    """
    redis_client = get_redis()
    key = f"realtime:connections:{connection_id}"
    data = redis_client.get(key)
    if data:
        return json.loads(data)
    return None


def get_online_users(user_type=None):
    """
    Get list of all online users.

    Args:
        user_type: Optional filter by user type

    Returns:
        list: List of (user_type, user_id) tuples for online users
    """
    redis_client = get_redis()
    if user_type:
        pattern = f"realtime:online:{user_type}:*"
    else:
        pattern = "realtime:online:*"

    online_users = []
    for key in redis_client.keys(pattern):
        # Normalize to string key
        if isinstance(key, (bytes, bytearray)):
            try:
                key = key.decode()
            except Exception:
                continue
        # Parse key: realtime:online:{user_type}:{user_id}
        parts = key.split(":", 3)
        if len(parts) == 4:
            _, _, u_type, u_id = parts
            online_users.append((u_type, u_id))

    return online_users


def disconnect_user(user_type, user_id):
    """
    Force disconnect all connections for a user.

    Args:
        user_type: Type of user (e.g., "user", "customer")
        user_id: User's ID
    """
    connections = get_user_connections(user_type, user_id)
    for conn_id in connections:
        send_to_connection(conn_id, {
            "type": "disconnect",
            "reason": "forced_disconnect"
        })


def request(user_type, user_id, data, timeout=30):
    """
    Send a message to a user over realtime and wait for their response.

    The client receives a message with type="request" and a unique request_id.
    They must respond with type="response" and the same request_id.

    Args:
        user_type: Type of user (e.g., "user")
        user_id: User's ID
        data: Dict with message content
        timeout: Seconds to wait for response (default 30)

    Returns:
        dict with response data, or None if timeout or user offline
    """
    connections = get_user_connections(user_type, user_id)
    if not connections:
        return None

    redis_client = get_redis()
    request_id = str(uuid.uuid4())
    response_key = f"realtime:response:{request_id}"

    # Send request to all user's connections
    send_to_user(user_type, user_id, {
        "type": "request",
        "request_id": request_id,
        "data": data,
        "timestamp": time.time()
    })

    # Block until response arrives or timeout
    result = redis_client.blpop(response_key, timeout=timeout)

    # Clean up in case of race
    redis_client.delete(response_key)

    if result is None:
        return None

    # BLPOP returns (key, value)
    _, raw = result
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def wait_for_event(user_type, user_id, match, timeout=30):
    """
    Wait for an incoming message from a user that matches specific fields.

    The client does not need to know Django is waiting — any message that
    matches all key:value pairs in `match` will be captured.

    Args:
        user_type: Type of user (e.g., "user")
        user_id: User's ID
        match: Dict of field:value pairs that must all match
        timeout: Seconds to wait (default 30)

    Returns:
        dict with the matched message data, or None if timeout
    """
    redis_client = get_redis()
    waiter_id = str(uuid.uuid4())
    waiters_key = f"realtime:waiters:{user_type}:{user_id}"
    waiter_match_key = f"realtime:waiter:{waiter_id}"
    waiter_result_key = f"realtime:waiter:{waiter_id}:result"

    # Register waiter with match criteria
    ttl = timeout + 10
    redis_client.sadd(waiters_key, waiter_id)
    redis_client.expire(waiters_key, ttl)
    redis_client.setex(waiter_match_key, ttl, json.dumps(match))

    try:
        # Block until a matching message arrives or timeout
        result = redis_client.blpop(waiter_result_key, timeout=timeout)

        if result is None:
            return None

        _, raw = result
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    finally:
        # Clean up waiter registration
        redis_client.srem(waiters_key, waiter_id)
        redis_client.delete(waiter_match_key)
        redis_client.delete(waiter_result_key)
        # Remove set if empty
        if redis_client.scard(waiters_key) == 0:
            redis_client.delete(waiters_key)
