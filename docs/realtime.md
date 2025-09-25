# Realtime System (Django Channels) — Developer Guide

This document explains how the realtime WebSocket system works, how to integrate it into your Django project, and how to use it from both the server and client sides.

It covers:
- Architecture and concepts
- Authentication and identity
- Topics, subscriptions, and notifications
- Message protocol
- Instance hooks and per-message handlers
- Utilities and publishing helpers
- Settings and configuration (Channels, routing, handlers)
- Testing utilities and examples
- Security, reliability, and troubleshooting

---

## Overview

The goal is a single, generic WebSocket endpoint that can be used by multiple apps with consistent, message-based authentication, and a flexible model identity abstraction.

Key concepts:
- One WebSocket endpoint for all apps: `ws/realtime/`
- Message-based auth using the exact same bearer handlers as HTTP middleware
- Generic identity:
  - `instance` — the authenticated Django model (e.g., `User`, `Customer`, `Terminal`, etc.)
  - `instance_kind` — the normalized bearer name (e.g., `"user"`, `"customer"`) from your HTTP middleware mapping
- Topic-based pub/sub:
  - External topic names (e.g., `user:123`) normalized to Channels-safe groups under the hood
  - Auto-subscription to the instance’s own topic (`"<instance_kind>:<id>"`)
- Pluggable message handling:
  - Built-in actions: `authenticate`, `subscribe`, `unsubscribe`, `ping`
  - Central message handlers (via settings)
  - Instance hooks (e.g., `on_realtime_message`, `on_realtime_connected`, `on_realtime_disconnected`)

---

## Files and Modules

- Consumer (WebSocket protocol and logic)
  - `mojo/apps/realtime/consumers.py` (class `AuthenticatedConsumer`)
- Routing (URL patterns for WebSocket)
  - `mojo/apps/realtime/routing.py` (exports `websocket_urlpatterns`)
- Shared Auth Utilities (reuse HTTP middleware mappings)
  - `mojo/apps/realtime/auth.py`
- Publish Helpers (server-side notifications)
  - `mojo/apps/realtime/utils.py`
- Tests and Test Client
  - `tests/test_realtime/basic.py`
  - `testit/ws_client.py`

---

## Authentication

Realtime uses a message-only, split-field authentication flow:
- The server accepts the socket and sends `{"type":"auth_required", "timeout_seconds": 30}`.
- The client must authenticate within the timeout using:
  - `{"type":"authenticate","token":"<token>","prefix":"bearer"}`
  - `prefix` is optional and defaults to `"bearer"`.

The auth flow shares bearer handler resolution with HTTP middleware:
- It uses the same:
  - `AUTH_BEARER_HANDLERS`
  - `AUTH_BEARER_HANDLERS_MAP`
  - `AUTH_BEARER_NAME_MAP`
- If a handler isn’t preloaded, it is dynamically resolved via `modules.load_function`.

On success:
- `instance` is attached to scope under the same key as HTTP (e.g., `scope["user"] = instance`)
- `instance_kind` is set to the mapped name (e.g., `"user"`)
- `on_realtime_connected()` is called on the instance (if present)
- Auto-subscription to `"<instance_kind>:<id>"`

On failure:
- An `error` is sent and the connection is closed.

---

## Instance Hooks

Any authenticated model (e.g., `User`) can optionally implement:

- `on_realtime_connected(self)`
  - Called after successful authentication.
- `on_realtime_message(self, data)`
  - Called for unhandled, non-reserved messages (see “Message Protocol”).
  - May return a dict to send back to the client (serialized to JSON).
- `on_realtime_disconnected(self)`
  - Called when the connection closes.

Example (User model already includes test-friendly implementations):
- `on_realtime_connected` sets flags in `metadata` and saves.
- `on_realtime_message` supports:
  - `{"message_type": "echo", "payload": {...}}` -> returns `{"type": "echo", "user_id": ..., "payload": {...}}`
  - `{"message_type": "set_meta", "key": "...", "value": "..."}` -> updates metadata, returns `{"type":"ack"}`
- `on_realtime_disconnected` clears the `realtime_connected` flag and saves.

---

## Topics and Subscriptions

- External topic names are used by clients: `user:123`, `customer:77`, `general_announcements`.
- Internally, topics are normalized to Channels-safe group names:
  - Only `[a-zA-Z0-9_.-]` are allowed; everything else becomes `_`.
  - Example: `user:123` -> `user_123`
- Auto-subscription: after authentication, if the instance has an `id`, the consumer subscribes it to:
  - `"<instance_kind>:<id>"`
  - Example: authenticated user id 42 -> `user:42`
- Authorization: the consumer’s `get_available_topics()` returns a list of external topics the instance may subscribe to:
  - Always includes `general_announcements`
  - Includes `admin_alerts` if the instance has staff privileges
  - Adds the instance topic `<instance_kind>:<id>` if present
- On subscribe, we check the requested external topic is in `available_topics`.

---

## Message Protocol

Server messages:
- `{"type":"auth_required","timeout_seconds":30}`
- `{"type":"auth_success","instance_kind":"user","instance_id":123,"available_topics":[...]}`
- `{"type":"auth_timeout","message":"..."}`
- `{"type":"error","message":"..."}`
- `{"type":"subscribed","topic":"user:123","group":"user_123"}`
- `{"type":"unsubscribed","topic":"user:123","group":"user_123"}`
- `{"type":"notification","topic":"user:123","title":"...","message":"...","timestamp":...,"priority":"normal"}`
- `{"type":"pong","instance_kind":"user","instance":"username"}`

Client messages:
- Authenticate:
  - `{"type":"authenticate","token":"<token>","prefix":"bearer"}`  (prefix is optional)
- Subscribe:
  - `{"action":"subscribe","topic":"<external-topic>"}`  e.g., `{"action":"subscribe","topic":"user:123"}`
- Unsubscribe:
  - `{"action":"unsubscribe","topic":"<external-topic>"}`
- Ping:
  - `{"action":"ping"}` (application-level ping/pong)
- Generic (routed to handlers or `instance.on_realtime_message`):
  - `{"message_type":"<your_message_type>", ...}`
  - or fallback using `type` if not reserved (avoid clashing with reserved types)

Reserved types:
- `authenticate`, `auth_required`, `auth_success`, `auth_timeout`, `error`, `notification`, `subscribed`, `unsubscribed`, `pong`

---

## Settings: Central Message Handlers

You can map `message_type` values to callables via settings, e.g.:

```python
# settings.py
REALTIME_MESSAGE_HANDLERS = {
    "echo_global": "your_app.realtime_handlers.echo_handler",
    "refresh_state": "your_app.realtime_handlers.refresh_state",
}
```

Handler signature:
- The callable may be sync or async. It will be invoked safely.
- Arguments:
  - `consumer` — the active consumer instance
  - `instance` — the authenticated model (e.g., `User`)
  - `instance_kind` — e.g., `"user"`
  - `data` — the message payload (dict)
- Return:
  - A dict to send to the client (optional)

Example:
```python
# your_app/realtime_handlers.py
def echo_handler(consumer, instance, instance_kind, data):
    return {
        "type": "echo_global",
        "echo": data.get("payload"),
        "instance_kind": instance_kind,
        "instance_id": getattr(instance, "id", None),
    }
```

If no central handler matches, the consumer falls back to calling `instance.on_realtime_message(data)` if defined.

---

## Server-Side Publish Helpers

Use these functions to send messages to subscribers:

```python
from mojo.apps.realtime.utils import (
    publish_to_topic,        # publish_to_topic("user:123", {...})
    publish_to_instance,     # publish_to_instance("user", 123, {...})
    publish_broadcast,       # publish_broadcast({...}) -> general_announcements
)
```

Event shape sent to the consumer:
```json
{
  "type": "notification_message",   // channels-level dispatch key
  "topic": "user:123",
  "timestamp": 1712345678.9,
  "...": "your payload (e.g., title/message/priority/etc.)"
}
```

The consumer translates these to client-facing `{"type":"notification", ...}` messages.

---

## Project Integration

1) Install/enable WebSocket support in your ASGI server
- You need a supported WS implementation (e.g., install an ASGI server bundle that includes websocket support).

2) Configure Channels layer (Redis recommended)
```python
# settings.py
ASGI_APPLICATION = "your_project.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",  # or Redis
        # For production, use Redis:
        # "BACKEND": "channels_redis.core.RedisChannelLayer",
        # "CONFIG": {"hosts": [("127.0.0.1", 6379)]},
    },
}
```

3) Root ASGI app wiring
Create a minimal `asgi.py` to serve both HTTP and WebSocket:

```python
# your_project/asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from mojo.apps.realtime.routing import websocket_urlpatterns

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "your_project.settings")

django_asgi = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi,
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)   # exposes ws/realtime/
    ),
})
```

4) Realtime URL routing
`mojo/apps/realtime/routing.py` already exports:

```python
from django.urls import path
from .consumers import AuthenticatedConsumer

websocket_urlpatterns = [
    path("ws/realtime/", AuthenticatedConsumer.as_asgi()),
]
```

---

## Client Examples

JavaScript (Browser):
```javascript
const ws = new WebSocket("wss://your-domain/ws/realtime/");

ws.addEventListener("open", () => {
  const token = localStorage.getItem("access_token");
  ws.send(JSON.stringify({
    type: "authenticate",
    token // prefix defaults to "bearer"
  }));
});

ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  switch (msg.type) {
    case "auth_success":
      ws.send(JSON.stringify({ action: "subscribe", topic: `user:${msg.instance_id}` }));
      break;
    case "notification":
      console.log("Notification:", msg);
      break;
    case "pong":
      console.log("Pong:", msg);
      break;
    case "error":
      console.error("Error:", msg.message);
      break;
  }
});

// Example ping
setInterval(() => ws.send(JSON.stringify({ action: "ping" })), 30000);
```

Python (test client from `testit/ws_client.py`):
```python
from testit.ws_client import WsClient

ws_url = WsClient.build_url_from_host("http://127.0.0.1:8001", path="ws/realtime/")
ws = WsClient(ws_url)
ws.connect()
auth = ws.authenticate("<access-token>")
ws.subscribe("user:123")
msg = ws.wait_for_type("notification", timeout=10.0)
ws.close()
```

---

## Tests

- Tests live in `tests/test_realtime/basic.py` and use:
  - `testit` runner style
  - `testit/ws_client.py` for WebSocket interactions
- Scenarios covered:
  - Quick availability: connect and assert `auth_required`
  - Full flow: authenticate -> subscribe -> ping -> publish -> receive
  - Instance hooks: `echo`, `set_meta` via `User.on_realtime_message`

To run:
- Ensure your ASGI server is running at the host used by `testit/runner.py` (it reads dev_server.conf to build `opts.host`).
- Execute the test runner as you normally do for your project.

---

## Security & Reliability Considerations

- Tokens should be short-lived and securely handled. Message-based auth avoids passing access tokens in query strings (which may be logged by proxies).
- Topic authorization is enforced by `get_available_topics()`. Keep it conservative.
- Keep instance hooks fast. Heavy work should be delegated to background tasks or async I/O.
- Authentication timeout (`~30s`) ensures idle unauthenticated sockets don’t linger.
- Consider Redis channel layer for production scalability and multiple workers.
- For high availability:
  - Enable proper retry/backoff on the client side
  - Use application-level ping/pong (already supported) or server-level keepalives
  - Protect endpoints with rate-limiting upstream if necessary

---

## Extensibility

- Add central `REALTIME_MESSAGE_HANDLERS` for cross-cutting message types.
- Use instance hooks for domain-specific behavior without modifying the consumer.
- Introduce additional topics in `get_available_topics(instance)` as needed.
- Use `publish_*` helpers to simplify server-side pushes.

---

## Troubleshooting

- 404 on `/ws/realtime/`:
  - Ensure root ASGI and Channels routing are configured and your server is running in ASGI mode.
- “Unsupported upgrade request”:
  - Ensure the ASGI server includes a WebSocket implementation.
- No messages received after subscribe:
  - Verify you subscribe to the external topic (e.g., `user:123`) and server publishes to the same external topic.
- Auth fails:
  - Verify HTTP middleware and realtime share the same `AUTH_BEARER_*` settings.
  - Check that `validate_bearer_token` handler exists and is callable.

---

## Reference Summary

- Endpoint: `ws/realtime/`
- Built-in actions:
  - `authenticate`, `subscribe`, `unsubscribe`, `ping`
- Server messages:
  - `auth_required`, `auth_success`, `auth_timeout`, `error`, `subscribed`, `unsubscribed`, `notification`, `pong`
- Instance hooks:
  - `on_realtime_connected`, `on_realtime_message(data)`, `on_realtime_disconnected`
- Settings:
  - `REALTIME_MESSAGE_HANDLERS = {"message_type": "path.to.function"}`
- Publish helpers:
  - `publish_to_topic(topic, payload)`, `publish_to_instance(kind, instance_id, payload)`, `publish_broadcast(payload)`
- Topic naming:
  - External: e.g., `user:123`
  - Internal (normalized to Channels group): `user_123`

If you need further examples (e.g., a complete `asgi.py`, settings, or advanced handler patterns), add them to this doc or create `docs/realtime-examples/` with runnable snippets.