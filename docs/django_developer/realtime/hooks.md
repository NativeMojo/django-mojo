# Instance Hooks — Django Developer Reference

## Overview

Any model that authenticates via WebSocket can implement optional hook methods. The framework calls them automatically at connection lifecycle events.

## Hook Methods

Add these methods to your `User` model (or any model used as a WebSocket identity):

```python
class User(MojoSecrets, AbstractBaseUser, MojoModel):

    def on_realtime_connected(self):
        """Called after successful WebSocket authentication."""
        self.metadata["realtime_connected"] = True
        self.atomic_save()

    def on_realtime_disconnected(self):
        """Called when the WebSocket connection closes."""
        self.metadata["realtime_connected"] = False
        self.atomic_save()

    def on_realtime_message(self, data):
        """
        Called for unhandled messages (not matched by REALTIME_MESSAGE_HANDLERS).
        data: dict of the message payload
        Return a dict to send back to the client, or None.
        """
        message_type = data.get("message_type")

        if message_type == "echo":
            return {"type": "echo", "payload": data.get("payload")}

        if message_type == "ping_user":
            return {"type": "pong_user", "user_id": self.id}

        return None
```

## Hook Execution Order

1. Client connects
2. Server sends `auth_required`
3. Client sends `authenticate`
4. Server validates token → sets `instance`
5. **`on_realtime_connected()`** called
6. Auto-subscribe to `<instance_kind>:<id>`
7. Server sends `auth_success`

On disconnect:
1. WebSocket closes
2. **`on_realtime_disconnected()`** called

On message:
1. Message arrives
2. Check `REALTIME_MESSAGE_HANDLERS` dict
3. If not found → call **`on_realtime_message(data)`**
4. Return value sent to client if provided

## Reserved Message Types

Do not use these as `message_type` values in `on_realtime_message`:
- `authenticate`, `auth_required`, `auth_success`, `auth_timeout`
- `error`, `notification`, `subscribed`, `unsubscribed`, `pong`
