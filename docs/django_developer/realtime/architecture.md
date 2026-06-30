# Realtime Architecture & Setup — Django Developer Reference

## Overview

The realtime system provides a generic WebSocket endpoint (`ws/realtime/`) using raw ASGI + Redis. It shares the same bearer authentication as HTTP middleware.

**Key concepts:**
- One endpoint for all apps
- Message-based authentication (same JWT/bearer tokens as HTTP)
- Topic-based pub/sub (`user:123`, `general_announcements`)
- Auto-subscription to own topic after auth
- Pluggable message handlers via settings or model hooks
- All state stored in Redis (stateless workers)

## Requirements

- Redis server (uses existing mojo Redis client)
- ASGI-compatible server (uvicorn, daphne, gunicorn+uvicorn)
- Python 3.8+

No additional dependencies required — no Django Channels, no channels_redis.

## Setup

### 1. ASGI Configuration

**Simplest setup:**
```python
# asgi.py
from mojo.apps.realtime.routing import create_application

application = create_application()
```

**With explicit routing:**
```python
# asgi.py
from django.core.asgi import get_asgi_application
from mojo.apps.realtime.routing import ProtocolTypeRouter, WebSocketRouter, path
from mojo.apps.realtime.asgi import get_asgi_application as get_realtime_asgi

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": WebSocketRouter([
        path("ws/realtime/", get_realtime_asgi()),
    ]),
})
```

### 2. Bearer Authentication

The system reuses `AUTH_BEARER_HANDLERS` from HTTP middleware — no separate config:

```python
# settings/middleware.py
AUTH_BEARER_HANDLERS = {
    "bearer": "myapp.models.User.validate_auth_token",
    "vendterm": "devices.models.Device.validate_auth_token",
}

# Maps bearer prefix to user_type for realtime
AUTH_BEARER_NAME_MAP = {
    "bearer": "user",
    "vendterm": "terminal",
}
```

### 3. Run with ASGI Server

```bash
uvicorn project.asgi:application --host 0.0.0.0 --port 8000
```

## Authentication Flow

1. Client connects to `ws://host/ws/realtime/`
2. Server sends: `{"type": "auth_required", "timeout": 30}`
3. Client sends: `{"type": "authenticate", "token": "<jwt>", "prefix": "bearer"}`
4. Server validates token via `AUTH_BEARER_HANDLERS`
5. Registers connection and user online status in Redis
6. Auto-subscribes to `<user_type>:<id>` topic
7. Calls `on_realtime_connection(connection_data)` hook (if defined)
8. Processes hook response (sends response, subscribes to topics)
9. Sends: `{"type": "auth_success", "user_type": "user", "user_id": 42}`

If no `authenticate` message arrives within 30 seconds, the connection is closed.

## Message Handlers

Register custom message types in settings:

```python
# settings.py
REALTIME_MESSAGE_HANDLERS = {
    "refresh_dashboard": "myapp.realtime.refresh_dashboard_handler",
    "send_team_message": "myapp.realtime.send_team_message_handler",
}
```

Messages not matched by a handler are routed to the instance's `on_realtime_message(data)` hook.

## Topics

- Topic names: `user:123`, `group:7`, `general_announcements`
- Auto-subscription: every authenticated connection subscribes to `<user_type>:<id>`
- Authorization: if the model defines `on_realtime_can_subscribe(topic)`, it is called on each subscribe request
- Topic membership is stored in Redis SETs with automatic TTL

## Activity Timeout

Connections are monitored for activity. If no client message (including `ping`) arrives within 30 seconds, the connection is closed. Clients should send periodic pings to stay alive:

```json
{"type": "ping"}
```

## Redis Architecture

All connection state lives in Redis, making workers stateless and horizontally scalable:

| Key Pattern | Type | Purpose |
|---|---|---|
| `realtime:connections:{id}` | STRING (JSON) | Connection metadata |
| `realtime:online:{user_type}:{user_id}` | SET | Active connection IDs for a user |
| `realtime:topic:{name}` | SET | Connection IDs subscribed to topic |
| `realtime:messages:{id}` | PUB/SUB | Direct messages to a connection |
| `realtime:broadcast` | PUB/SUB | Global broadcast channel |
| `realtime:response:{request_id}` | LIST | Request-response results |
| `realtime:waiters:{user_type}:{user_id}` | SET | Active event waiter IDs |

All keys have automatic TTL (default 300 seconds, refreshed on activity).

## Client IP Resolution

The WS handler derives the client IP using the same trust order as the HTTP path (ITEM-009 / ITEM-010):

1. **`X-Real-IP`** (proxy-authoritative) — checked first in both the ASGI `scope` headers and the wrapper `request_headers`. This is the canonical source.
2. **Transport peer** (`scope["client"]` / `peername`) — last-resort fallback only, used when `X-Real-IP` is absent (e.g. a direct-connect dev setup).

`X-Forwarded-For` and the RFC 7239 `Forwarded` header are **not consulted** — both are client-controllable and spoofable. The resolved IP is passed through the shared `normalize_ip` helper (strips port suffix, normalises IPv4-mapped IPv6, etc.).

**Deployment requirement:** the reverse proxy must set `X-Real-IP $remote_addr;` and overwrite any client-supplied value. The shipped `asgi.inc` already does this. Without it, the WS handler falls back to the transport peer address, which may be the proxy IP in a load-balancer setup.

The resolved IP is stored in:
- Redis connection records (`realtime:connections:{id}`)
- Security/incident `Event.source_ip` generated during the WS session

## Scaling

- Workers are stateless — add more processes behind a load balancer
- Redis pub/sub ensures messages reach the correct worker
- Each connection subscribes to its own Redis channel plus topic channels
- Online status uses Redis SETs supporting multiple connections per user
