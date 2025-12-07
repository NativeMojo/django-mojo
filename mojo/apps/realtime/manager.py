"""
Realtime manager functions - stateless interface for Django to interact with WebSocket system.

All connection state, user online status, and messaging is handled through Redis.
These functions provide a clean API for Django views/models to send messages,
check online status, and manage realtime connections.
"""

import json
import time

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
