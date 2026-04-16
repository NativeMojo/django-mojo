# Mojo Realtime WebSocket Module

A simple, robust WebSocket solution for Django applications using Redis + ASGI. 

## Overview

This module provides a stateless, scalable WebSocket system that:
- Uses Redis for all state management and pub/sub messaging
- Integrates with existing mojo authentication system
- Provides a clean function-based API for Django apps
- Scales horizontally across multiple workers
- Requires minimal dependencies (no Django Channels)

## Core Components

- **WebSocketHandler** - Handles individual WebSocket connections
- **Manager Functions** - Django-side API for sending messages and checking status
- **ASGI Application** - Routes WebSocket connections 
- **Redis Integration** - Uses existing mojo Redis client for state and messaging

## Quick Usage

### In Your Django Project

```python
# Import the manager
from mojo.apps import realtime

# Send message to specific user (wrapped in {"type": "message", "data": ...})
realtime.send_to_user("user", user_id, {
    "title": "New Message",
    "body": "You have a notification"
})

# Send event directly to user (payload sent as-is, no wrapping)
realtime.send_event_to_user("user", user_id, {
    "type": "status_update",
    "status": "active"
})

# Broadcast to all connected users
realtime.broadcast({
    "title": "System Alert",
    "body": "Maintenance starting soon"
})

# Check if user is online
if realtime.is_online("user", user_id):
    realtime.send_event_to_user("user", user_id, data)
else:
    send_email_notification(user_id, data)

# Publish to topic subscribers
realtime.publish_topic("chat:room1", {
    "type": "message",
    "user": "john",
    "text": "Hello everyone!"
})

# Request-response: send a message and wait for client reply
response = realtime.request("user", user_id, {
    "type": "confirm_action",
    "action": "delete_account"
}, timeout=30)

# Wait for a specific event from a user
event = realtime.wait_for_event("user", user_id, 
    match={"type": "device_state", "state": "ready"},
    timeout=60
)

# Get statistics
online_users = realtime.get_auth_count("user")
```

### ASGI Integration

**Simple setup with ProtocolTypeRouter:**
```python
# asgi.py - Import routing utilities directly (no Django setup needed)
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

**Even simpler with convenience function:**
```python
# asgi.py - Cleanest option, no Django dependencies
from mojo.apps.realtime.routing import create_application

# Uses default realtime route at ws/realtime/
application = create_application()
```

**Custom routes:**
```python
# asgi.py - Import routing utilities directly
from mojo.apps.realtime.routing import create_application, path
from mojo.apps.realtime.asgi import get_asgi_application as get_realtime_asgi

websocket_routes = [
    path("ws/realtime/", get_realtime_asgi()),
    path("ws/admin/", get_realtime_asgi()),  # Additional endpoints
]

application = create_application(websocket_routes=websocket_routes)
```

## Import Patterns

**ASGI Setup (works before Django is fully configured):**
```python
# asgi.py - No Django dependencies
from mojo.apps.realtime.routing import create_application, ProtocolTypeRouter
```

**Django Usage (requires Django setup):**
```python 
# views.py, models.py, tasks.py - After Django is configured
from mojo.apps import realtime
```

## Model Integration

### Connection Hook

The primary hook for connection lifecycle. Receives connection metadata and can return an initial response and/or request topic subscriptions:

```python
class Device(MojoModel):
    def on_realtime_connection(self, connection_data):
        """
        Called after successful WebSocket authentication.
        
        connection_data contains:
            - connection_id: unique connection UUID
            - remote_ip: client IP address
            - user_agent: client User-Agent string
        
        Return dict with optional keys:
            - "response": sent directly to client over WebSocket
            - "subscriptions": list of topics to subscribe to
        """
        self.touch()
        
        return {
            "response": {
                "type": "connected",
                "status": self.status,
                "config": self.get_config(),
            },
            "subscriptions": [
                "general_announcements",
            ],
        }
    
    def on_realtime_disconnected(self):
        """Called when the WebSocket connection closes."""
        pass
    
    def on_realtime_message(self, data):
        """
        Handle custom messages from client.
        Return dict with "response" key to reply, or None.
        """
        if data.get("type") == "heartbeat":
            return {"response": {"type": "ack"}}
        return None
    
    def on_realtime_can_subscribe(self, topic):
        """Gate topic subscriptions. Return True to allow, False to deny."""
        return topic == "general_announcements"
```

### Legacy Connection Hook

If `on_realtime_connection` is not defined, the framework falls back to:

```python
def on_realtime_connected(self):
    """Called after auth — no connection_data, same response contract."""
    pass
```

### Hook Response Contract

All hooks share the same response processing:

| Return value | Behavior |
|---|---|
| `{"response": {...}}` | Dict sent directly to client via WebSocket |
| `{"subscriptions": ["topic1", ...]}` | Subscribe client to topics |
| `{"response": {...}, "subscriptions": [...]}` | Both |
| Plain dict (no `response` key) | Sent directly to client (backward compat) |
| `None` | No action |

The `response` is delivered directly over the WebSocket connection — not through Redis pub/sub. This makes it reliable for initial state delivery on connect.

## Client-Side JavaScript

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/realtime/');

ws.onopen = () => {
    // Authenticate
    ws.send(JSON.stringify({
        type: 'authenticate',
        token: localStorage.getItem('access_token')
    }));
};

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    switch(data.type) {
        case 'auth_success':
            // Authenticated — hook response (if any) arrives before this
            console.log('Authenticated as', data.user_type, data.user_id);
            break;
            
        case 'connected':
            // Hook response from on_realtime_connection
            console.log('Initial state:', data);
            break;
            
        case 'message':
            // Wrapped message from send_to_user()
            console.log('Message:', data.data);
            break;
            
        default:
            // Direct events from send_event_to_user() or hook responses
            console.log('Event:', data.type, data);
            break;
    }
};
```

## Manager API Reference

### Sending Messages

- `send_to_user(user_type, user_id, message_data)` - Send to user, wrapped in `{"type": "message", "data": ...}`
- `send_event_to_user(user_type, user_id, event_data)` - Send to user directly, no wrapping (payload as-is)
- `send_to_connection(connection_id, message_data)` - Send to connection (wrapped)
- `send_event_to_connection(connection_id, event_data)` - Send to connection (direct, no wrapping)
- `broadcast(message_data)` - Send to all connected clients
- `publish_topic(topic, message_data)` - Send to topic subscribers

### Request-Response

- `request(user_type, user_id, data, timeout=30)` - Send message and block until client responds
- `wait_for_event(user_type, user_id, match, timeout=30)` - Block until client sends a message matching all key:value pairs in `match`

### Status Functions

- `is_online(user_type, user_id)` - Check if user is online
- `get_auth_count(user_type=None)` - Get count of authenticated connections
- `get_user_connections(user_type, user_id)` - Get connection IDs for user
- `get_online_users(user_type=None)` - Get list of online users
- `disconnect_user(user_type, user_id)` - Force disconnect user

### Info Functions

- `get_redis_info(connection_id)` - Get connection details
- `get_topic_subscribers(topic)` - Get subscribers for topic

## Redis Data Structure

The system uses these Redis key patterns:

- `realtime:connections:{connection_id}` - Connection metadata (STRING, JSON)
- `realtime:online:{user_type}:{user_id}` - User's active connection IDs (SET)
- `realtime:topic:{topic_name}` - Topic subscriber connection IDs (SET)
- `realtime:messages:{connection_id}` - Direct message channel (PUB/SUB)
- `realtime:broadcast` - Global broadcast channel (PUB/SUB)
- `realtime:response:{request_id}` - Request-response results (LIST, blocking pop)
- `realtime:waiters:{user_type}:{user_id}` - Active event waiters (SET)
- `realtime:waiter:{waiter_id}` - Waiter match criteria (STRING, JSON)

All keys have automatic TTL for cleanup.

## Message Protocol

### Client -> Server
```json
{"type": "authenticate", "token": "...", "prefix": "bearer"}
{"type": "subscribe", "topic": "user:123"}
{"type": "unsubscribe", "topic": "user:123"}  
{"type": "ping"}
{"type": "response", "request_id": "...", "data": {...}}
```

Any other `type` is routed to `REALTIME_MESSAGE_HANDLERS` or the instance's `on_realtime_message` hook.

### Server -> Client
```json
{"type": "auth_required", "timeout": 30}
{"type": "auth_success", "user_type": "user", "user_id": 123}
{"type": "subscribed", "topic": "user:123"}
{"type": "unsubscribed", "topic": "user:123"}
{"type": "message", "data": {...}, "topic": "user:123"}
{"type": "pong", "user_type": "user", "user_id": 123}
{"type": "error", "message": "..."}
```

Hook responses and `send_event_to_user` payloads arrive with their own `type` field (e.g., `{"type": "connected", ...}`).

## Requirements

- Redis server (uses existing mojo Redis client configuration)
- ASGI-compatible server (uvicorn, daphne, gunicorn+uvicorn)
- Python 3.8+ (for asyncio features)

## Deployment

1. Configure Redis connection in Django settings
2. Set up ASGI application with WebSocket routing
3. Run with ASGI server: `uvicorn project.asgi:application`
4. The system scales horizontally - add more worker processes as needed

## Features

- Stateless workers (all state in Redis)
- Horizontal scaling via Redis pub/sub
- Authentication via existing mojo auth system
- Topic-based subscriptions with authorization gates
- Direct user messaging (wrapped and unwrapped)
- Request-response pattern (blocking server-side)
- Event waiting (match incoming client messages)
- Broadcast messaging
- Online status tracking
- Connection statistics
- Automatic cleanup (TTL-based)
- Model hooks for connection lifecycle and message handling
- Thread-safe manager API
- Minimal dependencies
