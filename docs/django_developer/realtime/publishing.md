# Publishing Messages — Django Developer Reference

## Import

```python
from mojo.apps.realtime.utils import (
    publish_to_topic,
    publish_to_instance,
    publish_broadcast,
)
```

## publish_to_topic()

Send a message to all clients subscribed to a topic:

```python
publish_to_topic("user:42", {
    "type": "notification",
    "title": "Your order shipped",
    "message": "Order #123 is on its way!"
})
```

## publish_to_instance()

Shorthand for targeting a specific model instance:

```python
# Send to user 42's topic ("user:42")
publish_to_instance("user", 42, {
    "type": "notification",
    "title": "Payment received",
    "message": "Your payment of $99 was processed."
})
```

## publish_broadcast()

Send to all connected clients (general_announcements topic):

```python
publish_broadcast({
    "type": "announcement",
    "message": "System maintenance in 10 minutes"
})
```

## Message Shape

```python
# Messages are delivered with this structure to clients
{
    "type": "notification",    # or any custom type string
    "topic": "user:42",
    "timestamp": 1712345678.9,
    # ... your payload fields
}
```

## From a Model's on_rest_saved

Trigger realtime events after REST saves:

```python
def on_rest_saved(self, changed_fields, created):
    if "status" in changed_fields:
        from mojo.apps.realtime.utils import publish_to_instance
        publish_to_instance("user", self.user_id, {
            "type": "status_update",
            "order_id": self.id,
            "new_status": self.status
        })
```

## send_to_user / send_event_to_user

The `manager` module provides lower-level functions for sending directly to a user's WebSocket connections:

```python
from mojo.apps.realtime.manager import send_to_user, send_event_to_user
```

**`send_to_user()`** — wraps the payload in `{"type": "message", "data": ...}`:

```python
send_to_user("user", 42, {"title": "Hello", "body": "World"})
# Client receives: {"type": "message", "data": {"title": "Hello", "body": "World"}}
```

**`send_event_to_user()`** — sends the payload directly, no wrapping. Use this when your payload has its own `type` field and you want the client to receive it as-is:

```python
send_event_to_user("user", 42, {"type": "assistant_response", "response": "..."})
# Client receives: {"type": "assistant_response", "response": "..."}
```

The difference matters for client-side event routing. `send_event_to_user` avoids the need to unwrap a `message` envelope.

## Group Broadcast

Broadcast to all members of a group:

```python
from mojo.apps.realtime.utils import publish_to_topic

# All clients subscribed to group:7
publish_to_topic("group:7", {
    "type": "group_message",
    "body": "New policy update"
})
```
