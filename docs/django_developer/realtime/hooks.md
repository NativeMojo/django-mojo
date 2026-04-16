# Instance Hooks — Django Developer Reference

## Overview

Any model that authenticates via WebSocket can implement optional hook methods. The framework calls them automatically at connection lifecycle events.

## Hook Methods

Add these methods to your model (User, Device, or any model used as a WebSocket identity):

### on_realtime_connection(connection_data)

Primary connection hook. Called after successful authentication, before `auth_success` is sent. Receives connection metadata and can return a response to send to the client and/or request topic subscriptions.

```python
def on_realtime_connection(self, connection_data):
    """
    Called after WebSocket authentication succeeds.

    Args:
        connection_data: dict with keys:
            - connection_id: unique connection UUID
            - remote_ip: client IP address
            - user_agent: client User-Agent string

    Returns:
        dict with optional keys:
            - "response": dict sent directly to the client via WebSocket
            - "subscriptions": list of topic strings to subscribe to
        Or None for no action.
    """
    self.last_seen = timezone.now()
    self.save(update_fields=["last_seen"])

    return {
        "response": {
            "type": "connected",
            "status": "active",
        },
        "subscriptions": [
            "general_announcements",
        ],
    }
```

### on_realtime_connected()

Legacy connection hook. Called only if `on_realtime_connection` is not defined. Takes no arguments. Return value is processed the same way (see Hook Response Contract below).

```python
def on_realtime_connected(self):
    """Called after successful WebSocket authentication (legacy)."""
    self.metadata["realtime_connected"] = True
    self.atomic_save()
```

### on_realtime_disconnected()

Called when the WebSocket connection closes.

```python
def on_realtime_disconnected(self):
    """Called when the WebSocket connection closes."""
    self.metadata["realtime_connected"] = False
    self.atomic_save()
```

### on_realtime_message(data)

Called for incoming client messages not matched by `REALTIME_MESSAGE_HANDLERS` or a built-in type. Return value is processed the same way as connection hooks.

```python
def on_realtime_message(self, data):
    """
    Handle custom messages from the client.

    Args:
        data: dict of the full message payload

    Returns:
        dict with optional "response" and/or "subscriptions" keys,
        or a plain dict (sent directly as a response for backward compat),
        or None for no reply.
    """
    message_type = data.get("type") or data.get("message_type")

    if message_type == "echo":
        return {"response": {"type": "echo", "payload": data.get("payload")}}

    if message_type == "ping_user":
        return {"response": {"type": "pong_user", "user_id": self.id}}

    return None
```

### on_realtime_can_subscribe(topic)

Called when the client requests a topic subscription. Return `True` to allow, `False` to deny. If not defined, all subscriptions are allowed (the auto-subscription to `<user_type>:<id>` bypasses this check).

```python
def on_realtime_can_subscribe(self, topic):
    """
    Gate topic subscriptions.

    Args:
        topic: the topic string the client wants to subscribe to

    Returns:
        bool: True to allow, False to deny
    """
    allowed = {"general_announcements"}
    return topic in allowed
```

## Hook Response Contract

All hooks (`on_realtime_connection`, `on_realtime_connected`, `on_realtime_message`) share the same response processing via `_process_hook_response`:

| Return value | Behavior |
|---|---|
| `{"response": {...}}` | Dict is sent directly to the client over the WebSocket |
| `{"subscriptions": ["topic1", ...]}` | Client is subscribed to each topic |
| `{"response": {...}, "subscriptions": [...]}` | Both actions |
| Plain dict (no `response` key) | Sent directly to client (backward compatibility) |
| `None` | No action |

The `response` dict is delivered **directly over the WebSocket** — not through Redis pub/sub. This makes it reliable for initial state delivery on connect (no race conditions).

## Hook Execution Order

### Authentication

1. Client connects -> server sends `auth_required`
2. Client sends `authenticate` with token
3. Server validates token -> sets `instance` and `user_type`
4. Update connection auth in Redis
5. Register user online in Redis
6. Auto-subscribe to `<user_type>:<id>` topic
7. **`on_realtime_connection(connection_data)`** called (or `on_realtime_connected()` fallback)
8. Process hook response -> deliver `response`, process `subscriptions`
9. Server sends `auth_success`

### Disconnect

1. WebSocket closes
2. Redis cleanup (connection record, topic memberships, online status)
3. **`on_realtime_disconnected()`** called

### Message

1. Message arrives from client
2. Activity timeout is reset
3. Built-in types handled: `authenticate`, `subscribe`, `unsubscribe`, `ping`, `response`
4. Otherwise: check `REALTIME_MESSAGE_HANDLERS` setting
5. If not matched -> **`on_realtime_message(data)`** called
6. Hook response processed and delivered

## Reserved Message Types

Do not use these as client message types — they are handled by the framework:
- `authenticate`, `subscribe`, `unsubscribe`, `ping`, `response`

These server -> client types are framework-controlled:
- `auth_required`, `auth_success`, `auth_timeout`
- `error`, `subscribed`, `unsubscribed`, `pong`
- `message` (wraps `send_to_user` payloads)
