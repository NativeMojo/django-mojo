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

# Send message to specific user
realtime.send_to_user("user", user_id, {
    "title": "New Message",
    "body": "You have a notification"
})

# Broadcast to all connected users
realtime.broadcast({
    "title": "System Alert",
    "body": "Maintenance starting soon"
})

# Check if user is online
if realtime.is_online("user", user_id):
    # Send realtime notification
    realtime.send_to_user("user", user_id, data)
else:
    # Send email instead
    send_email_notification(user_id, data)

# Publish to topic subscribers
realtime.publish_topic("chat:room1", {
    "type": "message",
    "user": "john",
    "text": "Hello everyone!"
})

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

## User Model Integration

Add these optional methods to your User model:

```python
class User(AbstractUser):
    def on_realtime_connected(self):
        """Called when user connects via WebSocket"""
        # Update last seen, set online flag, etc.
        pass
        
    def on_realtime_disconnected(self):
        """Called when user disconnects"""
        # Update last seen, clear online flag, etc.
        pass
        
    def on_realtime_message(self, data):
        """Handle custom messages from client"""
        message_type = data.get('message_type')
        
        if message_type == 'echo':
            return {
                "type": "echo",
                "user_id": self.id,
                "payload": data.get('payload')
            }
        
        # Return dict to send response to client, or None
        return None
```

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
            // Subscribe to topics
            ws.send(JSON.stringify({
                type: 'subscribe',
                topic: `user:${data.user_id}`
            }));
            break;
            
        case 'message':
            // Handle incoming message
            console.log('Message:', data.data);
            break;
    }
};
```

## Manager API Reference

### Core Functions

- `broadcast(message_data)` - Send to all connected clients
- `publish_topic(topic, message_data)` - Send to topic subscribers  
- `send_to_user(user_type, user_id, message_data)` - Send to specific user
- `send_to_connection(connection_id, message_data)` - Send to specific connection

### Status Functions

- `is_online(user_type, user_id)` - Check if user is online
- `get_auth_count(user_type=None)` - Get count of authenticated connections
- `get_user_connections(user_type, user_id)` - Get connection IDs for user
- `get_online_users(user_type=None)` - Get list of online users
- `disconnect_user(user_type, user_id)` - Force disconnect user

### Info Functions

- `get_connection_info(connection_id)` - Get connection details
- `get_topic_subscribers(topic)` - Get subscribers for topic

## Redis Data Structure

The system uses these Redis key patterns:

- `realtime:connections:{connection_id}` - Connection metadata
- `realtime:online:{user_type}:{user_id}` - User online status
- `realtime:topic:{topic_name}` - Topic subscribers (SET)
- `realtime:messages:{connection_id}` - Direct message channel
- `realtime:broadcast` - Global broadcast channel
- `realtime:topic:{topic_name}` - Topic message channel

All keys have automatic TTL for cleanup.

## Message Protocol

### Client → Server
```json
{"type": "authenticate", "token": "...", "prefix": "bearer"}
{"type": "subscribe", "topic": "user:123"}
{"type": "unsubscribe", "topic": "user:123"}  
{"type": "ping"}
{"type": "custom", "message_type": "echo", "payload": {...}}
```

### Server → Client
```json
{"type": "auth_required", "timeout": 30}
{"type": "auth_success", "user_type": "user", "user_id": 123}
{"type": "message", "data": {...}, "topic": "user:123"}
{"type": "error", "message": "..."}
{"type": "pong", "user_type": "user", "user_id": 123}
```

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

- ✅ Stateless workers (all state in Redis)
- ✅ Horizontal scaling via Redis pub/sub
- ✅ Authentication via existing mojo auth system
- ✅ Topic-based subscriptions
- ✅ Direct user messaging
- ✅ Broadcast messaging
- ✅ Online status tracking
- ✅ Connection statistics
- ✅ Automatic cleanup (TTL-based)
- ✅ User model hooks for custom behavior
- ✅ Thread-safe manager API
- ✅ Minimal dependencies

This implementation is designed to be simple, reliable, and scalable for production use.