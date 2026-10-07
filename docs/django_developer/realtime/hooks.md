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
    return {"subscriptions": ["general_announcements"]}
```

### on_realtime_disconnected()

Called when the WebSocket connection closes.

```python
def on_realtime_disconnected(self):
    """Called when the WebSocket connection closes."""
    pass  # e.g. release something this socket held
```

Every reconnect runs these hooks, so keep them cheap — a hook that saves the
row turns each reconnect into database writes. To know who is online, read
`realtime.is_online(user_type, id)` (Redis) instead of storing a flag.

The built-in account `User` defines **neither** hook (#6562): connecting or
disconnecting writes nothing to `account_user`, and its old
`metadata["realtime_connected"]` / `realtime_connected_at` /
`realtime_disconnected_at` keys are no longer written. Use `User.is_online` or
`realtime.is_online("user", id)`, or the signal below.

### Connection signal

`mojo.apps.realtime.signals.realtime_connection_changed` is the hook point for
code that cannot (or should not) add a method to the identity model — e.g. a
product reacting to account `User` connects. The handler sends it once per
authenticated socket on connect and once on disconnect:

| Argument | Value |
|---|---|
| `sender` | The identity's model class (`User` for account users) |
| `user` | The authenticated identity instance |
| `connected` | `True` on connect, `False` on disconnect |
| `connection_id` | The socket's connection UUID |

```python
from mojo.apps.account.models import User
from mojo.apps.realtime.signals import realtime_connection_changed

def publish_presence(sender, user, connected, connection_id, **kwargs):
    from mojo.apps import realtime
    online = realtime.is_online("user", user.pk)  # already reflects this socket
    ...

realtime_connection_changed.connect(publish_presence, sender=User,
                                    dispatch_uid="myapp.presence")
```

- It fires **after** the Redis presence set changed, so `is_online()` already
  reflects the event. It is per socket, not per identity: decide
  online/offline edges from `is_online()`, not from `connected`.
- Receivers run on an executor thread with a database connection boundary,
  like the hooks; they sit inside every socket's auth and teardown, so keep
  them cheap.
- It is sent with `send_robust`: a receiver that raises is logged and never
  reaches the socket. Nothing is sent (no executor hop) when no receiver is
  connected.

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

Called when the client requests a topic subscription. Return `True` to allow, `False` to deny. If not defined, subscriptions are allowed subject to the optional group-topic policy below. Auto-subscriptions and hook-returned subscriptions bypass this hook, but still pass that policy.

When `REALTIME_GROUP_TOPIC_PERMISSIONS` is configured, the framework independently checks every `group:<id>` subscription and topic-message delivery. Returning `True` here or returning a topic in `subscriptions` cannot bypass that check. A custom hook can still deny a client subscription. The built-in User hook uses the configured permissions in place of its usual group membership / `view_groups` / `manage_groups` rule.

The policy defaults to disabled (`None` or unset), preserving existing behavior. See [Group-topic permissions](architecture.md#group-topic-permissions-opt-in) for configuration and revocation behavior.

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

Hooks run on executor threads, not on the event loop, and may use the ORM.
The framework closes or returns each hook's database connection when it
finishes, so a hook needs no connection handling of its own. See
[Database connections](architecture.md#database-connections).

## Hook Response Contract

All hooks (`on_realtime_connection`, `on_realtime_connected`, `on_realtime_message`) share the same response processing via `_process_hook_response`:

| Return value | Behavior |
|---|---|
| `{"response": {...}}` | Dict is sent directly to the client over the WebSocket |
| `{"subscriptions": ["topic1", ...]}` | Requests subscriptions; configured group-topic permissions still apply |
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
7. **`realtime_connection_changed(connected=True)`** sent (if any receiver)
8. **`on_realtime_connection(connection_data)`** called (or `on_realtime_connected()` fallback)
9. Process hook response -> deliver `response`, process `subscriptions`
10. Server sends `auth_success`, then starts its `ping` every `WS_SERVER_PING_SECONDS`

### Disconnect

1. WebSocket closes
2. Redis cleanup (connection record, topic memberships, online status)
3. **`realtime_connection_changed(connected=False)`** sent (if any receiver)
4. **`on_realtime_disconnected()`** called

### Message

1. Message arrives from client
2. Activity timeout is reset
3. Built-in types handled: `authenticate`, `subscribe`, `unsubscribe`, `ping`, `pong`, `response`
4. Otherwise: check `REALTIME_MESSAGE_HANDLERS` setting
5. If not matched -> **`on_realtime_message(data)`** called
6. Hook response processed and delivered

## Reserved Message Types

Do not use these as client message types — they are handled by the framework:
- `authenticate`, `subscribe`, `unsubscribe`, `ping`, `pong`, `response`

These server -> client types are framework-controlled:
- `auth_required`, `auth_success`, `auth_timeout`
- `error`, `subscribed`, `unsubscribed`, `ping`, `pong`
- `message` (wraps `send_to_user` payloads)
