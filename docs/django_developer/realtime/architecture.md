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

1. Client connects to `ws/realtime/` — gated first by a pre-accept per-IP
   connect-rate check (`WS_CONNECT_RATE_LIMIT`, default 30/min); over budget
   closes with code `4429` before `websocket.accept` is even sent (DM-042).
2. Server sends: `{"type": "auth_required", "timeout": <WS_UNAUTH_TIMEOUT>}`
3. Client sends: `{"type": "authenticate", "token": "<jwt>", "prefix": "bearer"}`
4. Server validates token via `AUTH_BEARER_HANDLERS`
5. Per-identity concurrency cap (`WS_MAX_CONNECTIONS`, default 10) is checked
   against `realtime:online:{user_type}:{user_id}`; over the cap sends an
   error and closes the connection.
6. Registers connection and user online status in Redis
7. The dedicated Redis pub/sub connection (`start_redis_messages`) is created
   here, **after** successful authentication — an unauthenticated socket
   never holds a pub/sub connection or its delivery task (DM-042; see
   [Abuse Hardening](../security/abuse_hardening.md#4-websocket-connection-limits)).
   Pub/sub runs on the event loop through `get_async_connection()` (#5750):
   waiting for a message holds no executor thread, so logins, hooks and
   permission checks never queue behind other sockets' polls. Keyed Redis
   commands (connection records, topic sets, presence) stay on the sync client
   in the executor.
8. Auto-subscribes to `<user_type>:<id>` topic
9. Sends `realtime_connection_changed(connected=True)` to any receivers
   (see [Instance Hooks](hooks.md#connection-signal)) — no database write
10. Calls `on_realtime_connection(connection_data)` hook (if defined)
11. Processes hook response (sends response, subscribes to topics)
12. Sends: `{"type": "auth_success", "user_type": "user", "user_id": 42}`
13. Starts the server ping (`WS_SERVER_PING_SECONDS`) — see
    [Activity Timeout and Keepalive](#activity-timeout-and-keepalive)

If no `authenticate` message arrives within `WS_UNAUTH_TIMEOUT` seconds
(default **10**, shortened from 30 in DM-042), the connection is closed.
Once authenticated, the idle timeout (`WS_IDLE_TIMEOUT`, default 90 s without
a client frame) applies instead.

### Database connections

Bearer validation and every hook or permission check that can reach the
database (`on_realtime_connection`, `on_realtime_connected`,
`on_realtime_message`, `on_realtime_can_subscribe`,
`on_realtime_disconnected`, `realtime_connection_changed` receivers,
group-topic and chat checks, incident reports) run
inside [`database_connection_boundary`](../helpers/async_db.md). Each call
drops a dead or expired connection before it runs and closes or returns its
connection afterwards, the way an HTTP request does. A database restart,
failover or killed backend therefore fails at most the call in flight, not
every later login on that process (#5736).

With `CONN_MAX_AGE = 0` (the default) each such call that queries the database
opens a connection. Configure `DATABASE_POOL_OPTIONS` to reuse pooled
connections instead. In pool mode, recovery relies on `CONN_HEALTH_CHECKS`,
which is on by default (`DATABASE_CONN_HEALTH_CHECKS`), to drop a dead pooled
connection before it is used.

### WebSocket Limits (DM-042)

| Setting | Default | Meaning |
|---|---|---|
| `WS_CONNECT_RATE_LIMIT` | `30` | Connects per minute per IP, checked before accept. `<= 0` disables. |
| `WS_MAX_CONNECTIONS` | `10` | Concurrent sockets per authenticated identity. `<= 0` disables. |
| `WS_UNAUTH_TIMEOUT` | `10` | Seconds an unauthenticated socket may live. |
| `WS_IDLE_TIMEOUT` | `90` | Seconds an authenticated socket may go without a client frame. |
| `WS_SERVER_PING_SECONDS` | `20` | Server ping interval for authenticated sockets. `<= 0` disables. |

Every `WS_*` setting is read **once**, from Django settings, when
`realtime/handler.py` is first imported (the first socket of the process) —
never per connection, and never from a DB-backed `Setting` row (#6562). Change
one in the settings file and restart the ASGI process.

A rejected pre-accept connection closes with code **4429** — clients should
treat this as a deliberate rejection and back off, not a network error. See
[Authenticated-Abuse Hardening](../security/abuse_hardening.md#4-websocket-connection-limits)
for the full rationale and deployment notes, and
[Rate Limits & Client Backoff](../../web_developer/security/rate_limits.md#websocket-rules)
for the client-side contract.

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

### Group-topic permissions (opt-in)

`REALTIME_GROUP_TOPIC_PERMISSIONS` is a file-static Django setting. Its default is `None`; leaving it unset or setting it to `None` preserves existing group subscriptions and delivery. It is not a database-backed Setting or per-group override.

```python
# MojoVerify deployment settings only; other deployments remain unset.
REALTIME_GROUP_TOPIC_PERMISSIONS = [
    "admin_compliance", "admin_verify", "view_verify", "manage_verification",
]
```

Configure a nonempty list or tuple of nonblank permission names. Permissions use **OR** semantics through `Group.user_has_permission`: any configured permission may grant access, including normal global/superuser and inherited membership permissions. Membership resolution uses the first active membership in the group/ancestor chain, matching the shared permission helper; a direct membership does not merge its permissions with a parent membership. An empty collection, wrong type, or invalid entry fails closed for group topics; it does not disable the policy.

While enabled:

- Group topics must be canonical positive decimal IDs, such as `group:7`. Leading zeros, signs, whitespace, extra segments, and other malformed `group:` names are denied.
- The authenticated identity must be an actual active account `User`. Other bearer identity models cannot access protected group topics, even if their IDs match a User.
- The group and all its ancestors must be active (`Group.get_active`). Generic membership or `view_groups` / `manage_groups` alone does not bypass the configured permission check.
- The policy applies to client subscriptions, automatic subscriptions, and subscriptions returned by hooks. A custom subscription hook may further deny access, but cannot grant access past this policy.
- Each protected subscribe and topic-message delivery reads a fresh active User and current group permissions from the primary database. When access is denied or cannot be checked, delivery drops that message and unsubscribes the connection from the topic. It does not close the socket or change other topics or the message envelope.

Revocation is checked on the next protected delivery; it does not wait for a Redis TTL or use a periodic permission cache. Already-sent messages cannot be recalled. Restoring permission does not replay dropped messages or automatically resubscribe the connection; the client must subscribe again. Redis pub/sub provides no replay guarantee.

## Activity Timeout and Keepalive

An authenticated socket is closed when no frame **from the client** has
arrived for `WS_IDLE_TIMEOUT` seconds (default 90). The check runs every 5 s.
Frames the server sends never count as activity.

The server keeps its own clients alive (#6562): from `auth_success` on, every
`WS_SERVER_PING_SECONDS` (default 20, `<= 0` disables) it sends

```json
{"type": "ping", "ts": 1712345678}
```

and a client that answers

```json
{"type": "pong"}
```

resets its idle clock — so a socket that only listens to server pushes stays up
for as long as it answers. A `pong` gets no reply, refreshes presence
(throttled) and never reaches `on_realtime_message`. Clients may also keep
sending their own `{"type": "ping"}`, which the server answers with
`{"type": "pong", ...}` as before.

These are application frames on purpose: uvicorn answers protocol-level
WebSocket pings itself (`bin/asgi_prod` configures no `--ws-ping-*`), so those
never reach the handler.

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

There is also a Pub/Sub channel per topic, `realtime:topic:{name}` — same
string as the membership SET above, different Redis namespace.

### Channel naming and `REDIS_PUBSUB_PREFIX`

The three Pub/Sub channels (broadcast, per-topic, per-connection messages)
are built by `mojo/apps/realtime/channels.py`. When the file-static setting
`REDIS_PUBSUB_PREFIX` is nonempty, every channel becomes
`{REDIS_PUBSUB_PREFIX}:{name}` on publish, subscribe, and unsubscribe.
Storage keys are unaffected (they are isolated by the Redis database
index), and nothing changes for clients — topic names, payloads, auth, and
topic authorization are identical; the prefix exists only on the Redis
wire. Default is `""` (channel names byte-identical to the table above).
It exists for test-checkout isolation — Redis Pub/Sub ignores database
numbers, so two test environments sharing one Redis would otherwise
receive each other's messages. `bin/create_testproject` derives a
per-checkout value; leave it unset in production. See
`testit/Isolation.md` — "Messaging isolation".

## Client IP Resolution

The WS handler derives the client IP using the same trust order as the HTTP path (DM-009 / DM-010):

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
- Each connection subscribes to its own Redis channel plus topic channels,
  on its own async pub/sub connection — one Redis connection per logged-in
  socket. `REDIS_PUBSUB_MAX_CONN` (default `REDIS_MAX_CONN`, 500) caps them
  per event loop, which is one per ASGI worker process on uvicorn, daphne and
  gunicorn with uvicorn workers. Idle sockets hold no executor thread.
- If a socket's pub/sub connection drops, that socket stops receiving and the
  error is logged; other sockets are unaffected. The WebSocket itself stays
  open (unchanged by #5750), so delivery resumes only when the client
  reconnects.
- Online status uses Redis SETs supporting multiple connections per user
