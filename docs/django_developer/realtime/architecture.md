# Realtime Architecture & Setup — Django Developer Reference

## Overview

The realtime system provides a single generic WebSocket endpoint (`ws/realtime/`) using Django Channels. It shares the same bearer authentication as HTTP middleware.

**Key concepts:**
- One endpoint for all apps
- Message-based authentication (same JWT as HTTP)
- Topic-based pub/sub (`user:123`, `general_announcements`)
- Auto-subscription to own topic after auth
- Pluggable message handlers via settings or model hooks

## Requirements

```
channels>=4.0
channels_redis>=4.0
```

## Setup

### 1. Install the App

```python
INSTALLED_APPS = [
    ...
    "channels",
    "mojo.apps.realtime",
]
```

### 2. ASGI Configuration

```python
# asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from mojo.apps.realtime.routing import websocket_urlpatterns

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": URLRouter(websocket_urlpatterns),
})
```

### 3. Channel Layers (Redis)

```python
# settings.py
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [("localhost", 6379)],
        },
    },
}
```

## Authentication Flow

1. Client connects to `ws://host/ws/realtime/`
2. Server sends: `{"type": "auth_required", "timeout_seconds": 30}`
3. Client sends: `{"type": "authenticate", "token": "<jwt>", "prefix": "bearer"}`
4. On success: `{"type": "auth_success", "instance_kind": "user", "instance_id": 42, "available_topics": [...]}`
5. Client is auto-subscribed to `user:42`

The auth system reuses `AUTH_BEARER_HANDLERS` from HTTP middleware — no separate config.

## Message Handlers

Register custom message types in settings:

```python
# settings.py
REALTIME_MESSAGE_HANDLERS = {
    "refresh_dashboard": "myapp.realtime.refresh_dashboard_handler",
    "send_team_message": "myapp.realtime.send_team_message_handler",
}
```

Handler signature:

```python
def refresh_dashboard_handler(consumer, instance, instance_kind, data):
    # instance = the authenticated User (or other model)
    # data = the message payload dict
    # Return a dict to send back to the client (optional)
    return {"type": "dashboard_refreshed", "data": get_dashboard(instance)}
```

## Topics

- External topic names: `user:123`, `group:7`, `general_announcements`
- Internally normalized to Channels-safe names (`user_123`)
- Authorization: each consumer's `get_available_topics()` determines allowed subscriptions
- Default available topics: `general_announcements`, `admin_alerts` (staff only), own `instance_kind:id`

## Settings

| Setting | Description |
|---|---|
| `REALTIME_MESSAGE_HANDLERS` | Dict mapping message_type → handler path |
| `CHANNEL_LAYERS` | Django Channels Redis config |
