# django-mojo/mojo/apps/realtime/utils.py
import re
import time
from typing import Any, Dict, Optional, Union

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


__all__ = [
    "normalize_topic",
    "publish_to_topic",
    "publish_to_instance",
    "publish_broadcast",
]


MAX_GROUP_LEN = 200
DEFAULT_TOPIC = "general_announcements"


def normalize_topic(topic: Optional[str]) -> str:
    """
    Convert an external topic name (e.g., 'user:123') to a Channels-safe group name.

    Channels group names are limited to alphanumeric characters, underscore, hyphen, and dot.
    All other characters will be replaced by an underscore. Result is truncated to MAX_GROUP_LEN.

    Examples:
      - 'user:123' -> 'user_123'
      - 'customer/77' -> 'customer_77'

    Args:
        topic: External topic name provided by the caller.

    Returns:
        A Channels-safe group name string.
    """
    topic = str(topic or "")
    if not topic:
        return DEFAULT_TOPIC
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", topic)[:MAX_GROUP_LEN] or DEFAULT_TOPIC


def publish_to_topic(topic: str, payload: Optional[Dict[str, Any]] = None, dispatch_type: str = "notification_message") -> None:
    """
    Publish a realtime message to all WebSocket clients subscribed to the given topic.

    The topic is the external name (e.g., 'user:123', 'customer:77'). This function
    normalizes the topic to a Channels-safe group name and sends a Channels dispatch
    event with the given `dispatch_type`. By default it uses 'notification_message'
    so it is routed to the consumer's 'notification_message' handler.

    Args:
        topic: External topic name.
        payload: Dict payload to send to clients (e.g., title/message/priority/etc.).
        dispatch_type: Channels event dispatch type to invoke on the consumer.
                       Defaults to 'notification_message'.

    Notes:
        - Event shape sent to the consumer:
            {
                "type": "<dispatch_type>",        # Channels dispatch key
                "topic": "<external-topic>",
                "timestamp": <epoch-seconds>,
                ...payload
            }
        - The consumer is responsible for serializing to JSON and sending to the client.
    """
    channel_layer = get_channel_layer()
    group = normalize_topic(topic)
    data = payload.copy() if isinstance(payload, dict) else {}
    if "dispatch_type" in data:
        dispatch_type = data["dispatch_type"]
    event = {
        "type": dispatch_type,
        "topic": topic,
        "timestamp": time.time(),
        **data,
    }
    async_to_sync(channel_layer.group_send)(group, event)


def publish_to_instance(
    kind: str,
    instance_id: Union[int, str],
    payload: Optional[Dict[str, Any]] = None,
    dispatch_type: str = "notification_message"
) -> None:
    """
    Publish a realtime message to a specific instance by kind and id.

    This builds the external topic as '<kind>:<id>' (e.g., 'user:42', 'terminal:abc')
    and delegates to publish_to_topic.

    Args:
        kind: Instance kind (e.g., 'user', 'customer', 'terminal').
        instance_id: Instance identifier (int or str).
        payload: Dict payload to send.
    """
    topic = f"{kind}:{instance_id}"
    publish_to_topic(topic, payload, dispatch_type=dispatch_type)


def publish_broadcast(payload: Optional[Dict[str, Any]] = None) -> None:
    """
    Publish a broadcast message to all listeners of the default topic.

    Args:
        payload: Dict payload to send.
    """
    publish_to_topic(DEFAULT_TOPIC, payload)
