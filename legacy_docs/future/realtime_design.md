# Realtime WebSocket Design - Redis + ASGI Solution

## Overview

This design replaces Django Channels with a simple, robust WebSocket solution using ASGI + Redis. The architecture prioritizes simplicity, scalability, and minimal third-party dependencies while maintaining safety between Django ORM and realtime components.

## Core Principles

1. **KISS Design** - Keep components simple and focused
2. **Minimal Dependencies** - Use only Python's built-in `websockets` library + Redis
3. **Thread Safety** - Safe interaction between Django ORM and realtime system
4. **Redis-Centric** - Redis handles all connection state, pub/sub, and coordination
5. **Stateless Workers** - WebSocket workers can be added/removed without state loss
6. **Manager Interface** - Clean API for Django HTTP side to interact with realtime system

## Architecture Components

### 1. WebSocket Handler (`RealtimeWebSocketHandler`)
- Handles individual WebSocket connections
- Manages authentication flow
- Routes messages between client and Redis
- Lightweight and stateless (connection state in Redis)

### 2. Realtime Manager (`RealtimeManager`)
- Django-side interface for realtime operations
- Provides methods like `broadcast()`, `publish_topic()`, `is_online()`, `get_auth_count()`
- Thread-safe singleton accessible from Django views/models
- Handles Django ORM → Redis communication

### 3. Redis Data Layer
- Connection registry (active connections)
- Authentication mapping (user → connection)
- Topic subscriptions (topic → subscribers)
- Message pub/sub channels
- Online status tracking

### 4. ASGI Application
- Routes WebSocket connections to handler
- Integrates with Django's ASGI app
- Simple protocol router

## Redis Data Structures

### Connection Registry
```
realtime:connections:{connection_id} = {
    "user_id": 123,
    "user_type": "user", 
    "authenticated": true,
    "connected_at": timestamp,
    "last_ping": timestamp,
    "topics": ["user:123", "general"]
}
TTL: 3600 seconds (auto-cleanup dead connections)
```

### User Online Status
```
realtime:online:{user_type}:{user_id} = {
    "connection_ids": ["conn1", "conn2"],
    "last_seen": timestamp
}
TTL: 3600 seconds
```

### Topic Subscriptions
```
realtime:topic:{topic_name} = SET of connection_ids
TTL: 3600 seconds
```

### Pub/Sub Channels
- `realtime:messages:{connection_id}` - Direct messages to specific connection
- `realtime:broadcast` - Global broadcast messages
- `realtime:topic:{topic_name}` - Topic-specific messages

## Core Components Design

### WebSocket Handler

```python
class WebSocketHandler:
    def __init__(self, websocket, path):
        self.websocket = websocket
        self.connection_id = generate_unique_id()
        self.redis_client = get_connection()
        self.authenticated = False
        self.user = None
        self.user_type = None
        
    async def handle_connection(self):
        """Main connection handler"""
        try:
            await self.register_connection()
            await self.send_auth_required()
            
            # Start background tasks
            auth_task = asyncio.create_task(self.auth_timeout())
            message_task = asyncio.create_task(self.handle_messages())
            redis_task = asyncio.create_task(self.handle_redis_messages())
            
            # Wait for any task to complete
            done, pending = await asyncio.wait(
                [auth_task, message_task, redis_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                
        finally:
            await self.cleanup_connection()
    
    async def authenticate(self, token, prefix="bearer"):
        """Handle authentication using existing auth logic"""
        # Reuse existing mojo.apps.realtime.auth logic
        user, error, key_name = await async_validate_bearer_token(prefix, token)
        
        if error or not user:
            await self.send_error("Authentication failed")
            return False
            
        self.user = user
        self.user_type = key_name
        self.authenticated = True
        
        # Update Redis connection record
        await self.update_connection_auth()
        await self.register_user_online()
        await self.send_auth_success()
        
        # Auto-subscribe to user's own topic
        await self.subscribe_topic(f"{self.user_type}:{self.user.id}")
        
        return True
```

### Manager Functions

```python
def broadcast(message_data):
    """Broadcast message to all connected clients"""
    redis_client = get_connection()
    message = {
        "type": "broadcast",
        "data": message_data,
        "timestamp": time.time()
    }
    redis_client.publish("realtime:broadcast", json.dumps(message))

def publish_topic(topic, message_data):
    """Publish message to specific topic subscribers"""
    redis_client = get_connection()
    message = {
        "type": "topic_message", 
        "topic": topic,
        "data": message_data,
        "timestamp": time.time()
    }
    redis_client.publish(f"realtime:topic:{topic}", json.dumps(message))

def send_to_user(user_type, user_id, message_data):
    """Send direct message to specific user (all their connections)"""
    connections = get_user_connections(user_type, user_id)
    for conn_id in connections:
        send_to_connection(conn_id, message_data)

def send_to_connection(connection_id, message_data):
    """Send message to specific connection"""
    redis_client = get_connection()
    message = {
        "type": "direct_message",
        "data": message_data,
        "timestamp": time.time()
    }
    redis_client.publish(f"realtime:messages:{connection_id}", json.dumps(message))

def is_online(user_type, user_id):
    """Check if user is online"""
    redis_client = get_connection()
    key = f"realtime:online:{user_type}:{user_id}"
    return redis_client.exists(key) > 0

def get_auth_count(user_type=None):
    """Get count of authenticated connections"""
    redis_client = get_connection()
    if user_type:
        pattern = f"realtime:online:{user_type}:*"
    else:
        pattern = "realtime:online:*"
    return len(redis_client.keys(pattern))

def get_user_connections(user_type, user_id):
    """Get all connection IDs for a user"""
    redis_client = get_connection()
    key = f"realtime:online:{user_type}:{user_id}"
    data = redis_client.get(key)
    if data:
        user_data = json.loads(data)
        return user_data.get("connection_ids", [])
    return []

def get_topic_subscribers(topic):
    """Get connection IDs subscribed to topic"""
    redis_client = get_connection()
    return list(redis_client.smembers(f"realtime:topic:{topic}"))
```

## Message Protocol

### Client → Server
```json
// Authentication
{"type": "authenticate", "token": "...", "prefix": "bearer"}

// Subscribe to topic
{"type": "subscribe", "topic": "user:123"}

// Unsubscribe from topic  
{"type": "unsubscribe", "topic": "user:123"}

// Ping
{"type": "ping"}

// Custom message
{"type": "custom", "action": "...", "data": {...}}
```

### Server → Client
```json
// Authentication required
{"type": "auth_required", "timeout": 30}

// Authentication success
{"type": "auth_success", "user_type": "user", "user_id": 123}

// Topic message
{"type": "message", "topic": "user:123", "data": {...}}

// Direct message
{"type": "direct", "data": {...}}

// Broadcast message
{"type": "broadcast", "data": {...}}

// Error
{"type": "error", "message": "..."}

// Pong
{"type": "pong"}
```

## Safety Considerations

### Django ORM Safety
1. **Manager Pattern** - All Django ORM interactions go through RealtimeManager
2. **Connection Pooling** - Django and WebSocket workers use separate Redis connection pools
3. **Async Wrappers** - Use `database_sync_to_async` for ORM calls in async context
4. **Transaction Safety** - No shared transactions between HTTP and WebSocket

### Connection Safety
1. **Heartbeat** - Automatic ping/pong to detect dead connections
2. **TTL Cleanup** - Redis keys expire automatically for cleanup
3. **Graceful Shutdown** - Proper cleanup on connection close
4. **Error Isolation** - Connection errors don't affect other connections

### Memory Safety
1. **Stateless Workers** - No in-memory state (everything in Redis)
2. **Connection Limits** - Redis connection pool limits prevent memory exhaustion
3. **Message Queuing** - Use Redis pub/sub for message queuing

## Scalability Design

### Horizontal Scaling
- **Stateless Workers** - Add WebSocket worker processes easily
- **Redis Pub/Sub** - Handles inter-worker communication
- **Load Balancer** - Standard WebSocket load balancing (sticky sessions not required)

### Redis Scaling
- **Cluster Support** - Existing Redis client supports clustering
- **Connection Pooling** - Efficient Redis connection usage
- **Key Partitioning** - User-based keys naturally partition

### Performance Optimizations
- **Connection Batching** - Batch Redis operations where possible
- **Message Compression** - Optional JSON compression for large messages
- **Connection Pooling** - Reuse Redis connections across WebSocket connections

## ASGI Integration

```python
# asgi.py
from django.core.asgi import get_asgi_application
from mojo.apps.realtime.asgi import ASGIApplication

django_asgi_app = get_asgi_application()

async def application(scope, receive, send):
    if scope["type"] == "http":
        await django_asgi_app(scope, receive, send)
    elif scope["type"] == "websocket" and scope["path"] == "/ws/realtime/":
        await ASGIApplication()(scope, receive, send)
    else:
        # Reject other WebSocket paths
        await send({"type": "websocket.close", "code": 404})
```

## Django Integration Examples

```python
# In Django views/models
from mojo.apps import realtime

# Send notification to user
realtime.send_to_user("user", user_id, {
    "title": "New Message",
    "body": "You have a new message"
})

# Broadcast announcement
realtime.broadcast({
    "title": "System Maintenance", 
    "body": "System will be down for maintenance"
})

# Check if user is online
if realtime.is_online("user", user_id):
    # User is online, send realtime notification
    pass
else:
    # User offline, send email/SMS
    pass

# Get online user count
online_count = realtime.get_auth_count("user")
```

## Error Handling & Monitoring

### Connection Monitoring
- Track connection counts by user type
- Monitor authentication success/failure rates  
- Alert on unusual disconnection patterns

### Redis Health
- Monitor Redis connection pool health
- Track pub/sub message delivery
- Alert on Redis connectivity issues

### Performance Metrics
- WebSocket connection duration
- Message delivery latency
- Authentication timing

## Implementation Plan

### Phase 1: Core Infrastructure
1. `WebSocketHandler` - Basic WebSocket handling
2. Redis data structures and key patterns
3. Authentication integration with existing auth system
4. Basic ASGI application setup

### Phase 2: Manager Interface  
1. Manager module functions with core methods
2. Django integration points
3. Connection tracking and online status
4. Topic subscription system

### Phase 3: Advanced Features
1. Message routing and custom handlers
2. Performance optimizations
3. Comprehensive error handling
4. Monitoring and metrics

### Phase 4: Testing & Deployment
1. Update existing tests to new system
2. Load testing and performance validation
3. Production deployment strategy
4. Documentation and examples

## Migration Strategy

1. **Parallel Deployment** - Run new system alongside Channels initially
2. **Gradual Migration** - Move endpoints one by one
3. **Feature Parity** - Ensure all existing functionality works
4. **Performance Validation** - Confirm scalability improvements
5. **Rollback Plan** - Quick rollback to Channels if needed

## Dependencies

**Required:**
- `websockets` - Pure Python WebSocket library
- `redis` - Already in project for Redis client
- `asyncio` - Built-in Python async support

**No additional third-party libraries needed** - This keeps the solution simple and avoids dependency issues that plagued the Channels implementation.

## Security Considerations

1. **Authentication** - Reuse existing bearer token validation
2. **Authorization** - Topic-level access control
3. **Rate Limiting** - Connection and message rate limits
4. **Input Validation** - Sanitize all client messages
5. **Connection Limits** - Per-user connection limits

This design provides a robust, scalable WebSocket solution that's much simpler than Channels while maintaining all required functionality. The Redis-centric approach ensures state consistency across multiple workers and the manager functions provide a clean Django integration point.

## Manager Pattern - Class vs Functions

**Pros of Functions (Recommended)**:
- Simpler - no class instantiation or singleton management
- Stateless - each function gets fresh Redis connection from pool
- Thread-safe by default - no shared state between calls
- Cleaner imports - `from mojo.apps import realtime; realtime.send_to_user()`
- Less code - no `__init__` or class overhead
- Redis connection pool handles concurrency automatically

**Cons of Functions**:
- Slight overhead getting Redis connection per call (mitigated by connection pooling)
- No instance-level caching (not needed for stateless design)

**Implementation**:
```python
# mojo/apps/realtime/__init__.py  
from .manager import *

# mojo/apps/realtime/manager.py
# All the functions above

# Usage in Django
from mojo.apps import realtime
realtime.broadcast(data)
realtime.is_online("user", 123)
```

The function-based approach is cleaner and fits the stateless Redis-backed design perfectly.