# Redis Helper Documentation

The Django-MOJO Redis helper provides a unified, connection-pooled Redis interface with typed operations and resource management capabilities.

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Connection Management](#connection-management)
4. [Redis Adapter](#redis-adapter)
5. [Redis Pools](#redis-pools)
6. [Configuration](#configuration)
7. [Best Practices](#best-practices)
8. [Examples](#examples)

## Overview

The Redis helper consists of three main components:

- **`client.py`**: Shared connection pooling with automatic response decoding
- **`adapter.py`**: High-level typed operations for streams, sets, hashes, and more
- **`pool.py`**: Resource pooling for managing application-level resources

All components use a single, shared Redis connection pool for optimal performance and resource usage.

## Quick Start

```python
from mojo.helpers.redis import get_connection, get_adapter

# Simple Redis operations
redis_conn = get_connection()
redis_conn.set('key', 'value')
value = redis_conn.get('key')

# Using the adapter for advanced operations
adapter = get_adapter()
adapter.set('user:123', {'name': 'John', 'email': 'john@example.com'})
```

## Connection Management

### Basic Connection

```python
from mojo.helpers.redis import get_connection

# Get a connection from the shared pool
redis_conn = get_connection()

# All responses are automatically decoded to strings
redis_conn.set('hello', 'world')
value = redis_conn.get('hello')  # Returns 'world' (string, not bytes)
```

### Connection Features

- **Shared Pool**: All Redis operations use the same connection pool
- **Auto Decode**: `decode_responses=True` is enabled by default
- **Thread Safe**: Connection pool handles concurrent access
- **Configuration**: Uses settings from `mojo.helpers.settings`

## Redis Adapter

The `RedisAdapter` provides typed, high-level operations with automatic serialization.

### Getting the Adapter

```python
from mojo.helpers.redis import get_adapter

adapter = get_adapter()
```

### Stream Operations

Perfect for message queues and event processing:

```python
# Add message to stream
message_id = adapter.xadd('events', {
    'type': 'user_login',
    'user_id': 123,
    'timestamp': '2024-01-01T12:00:00Z',
    'metadata': {'ip': '192.168.1.1'}  # Automatically JSON serialized
})

# Create consumer group
adapter.xgroup_create('events', 'processors', id='0', mkstream=True)

# Read messages as consumer
messages = adapter.xreadgroup(
    'processors', 'worker-1', 
    {'events': '>'}, 
    count=10, 
    block=1000
)

# Process and acknowledge
for stream, msgs in messages:
    for msg_id, fields in msgs:
        # Process message...
        adapter.xack(stream, 'processors', msg_id)
```

### Hash Operations

For structured data storage:

```python
# Store user data
adapter.hset('user:123', {
    'name': 'John Doe',
    'email': 'john@example.com',
    'active': True,
    'settings': {'theme': 'dark', 'lang': 'en'}  # Auto JSON serialization
})

# Get single field
name = adapter.hget('user:123', 'name')

# Get all fields
user_data = adapter.hgetall('user:123')
```

### Sorted Set Operations

For rankings, scheduling, and prioritized queues:

```python
# Add items with scores
adapter.zadd('leaderboard', {
    'player1': 1000,
    'player2': 1500,
    'player3': 750
})

# Get top players
top_players = adapter.zpopmin('leaderboard', count=3)

# Get players in score range
mid_players = adapter.zrangebyscore('leaderboard', 500, 1200)
```

### Key-Value Operations

```python
# Simple key-value with expiration
adapter.set('session:abc123', 'user_data', ex=3600)  # Expires in 1 hour

# Complex data (auto JSON serialized)
adapter.set('config:app', {
    'version': '1.0.0',
    'features': ['auth', 'redis', 'jobs']
})

config = adapter.get('config:app')  # Returns JSON-deserialized dict
```

### List Operations

For simple queues and stacks:

```python
# Push to queue
adapter.rpush('tasks', 'process_image', 'send_email', 'cleanup')

# Blocking pop (waits up to 30 seconds)
task = adapter.brpop(['tasks'], timeout=30)
if task:
    queue_name, task_name = task
    # Process task...
```

### Pub/Sub Operations

For real-time messaging:

```python
# Publish message
subscribers = adapter.publish('notifications', {
    'type': 'new_message',
    'user_id': 123,
    'message': 'Hello world!'
})

# Subscribe (in another process/thread)
pubsub = adapter.pubsub()
pubsub.subscribe('notifications')
for message in pubsub.listen():
    if message['type'] == 'message':
        data = json.loads(message['data'])
        # Handle notification...
```

### Pipeline Operations

For batch operations:

```python
with adapter.pipeline() as pipe:
    pipe.hset('user:1', {'name': 'Alice'})
    pipe.hset('user:2', {'name': 'Bob'})
    pipe.zadd('online_users', {'user:1': time.time()})
    # All operations executed atomically
```

## Redis Pools

For managing application-level resources using Redis as coordination layer.

### Basic Pool

```python
from mojo.helpers.redis.pool import RedisBasePool

# Create pool for managing worker resources
pool = RedisBasePool('worker_pool', default_timeout=30)

# Add items to pool
pool.add('worker_1')
pool.add('worker_2') 
pool.add('worker_3')

# Get next available worker
worker_id = pool.get_next_available(timeout=10)
if worker_id:
    # Do work with worker...
    # Return to pool when done
    pool.checkin(worker_id)
```

### Django Model Pool

```python
from mojo.helpers.redis.pool import RedisModelPool
from myapp.models import Device

# Pool for managing active devices
device_pool = RedisModelPool(
    model_cls=Device,
    query_dict={'status': 'active', 'available': True},
    pool_key='available_devices'
)

# Initialize with current active devices
device_pool.init_pool()

# Get next available device
device = device_pool.get_next_instance(timeout=30)
if device:
    # Use device for task...
    # Return to pool
    device_pool.return_instance(device)
```

## Configuration

Redis settings are managed through the existing MOJO settings helper system. You can configure Redis using individual settings or a complete REDIS_DB dictionary:

### Individual Settings (Recommended)

```python
# In your Django settings or config files
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
REDIS_DATABASE = 0
REDIS_PASSWORD = 'your_password'  # Optional
```

### Complete Configuration Dictionary

```python
# Alternative: Use REDIS_DB dictionary for full control
REDIS_DB = {
    'host': 'localhost',
    'port': 6379,
    'db': 0,
    'password': None,
    'socket_timeout': 30,
    'socket_connect_timeout': 30,
    'socket_keepalive': True,
    'socket_keepalive_options': {},
    'retry_on_timeout': True,
    'encoding': 'utf-8',
    'encoding_errors': 'strict',
    # decode_responses is automatically set to True - do not override!
}
```

### Environment Variables

```bash
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DATABASE=0
REDIS_PASSWORD=secret
```

### Important Notes

- `decode_responses=True` is **always enforced** to ensure all Redis responses are strings, not bytes
- Individual settings take precedence and are easier to manage
- The MOJO settings helper automatically handles defaults and environment variables

## Best Practices

### 1. Use the Adapter for Complex Operations

```python
# Good: Use adapter for structured operations
adapter = get_adapter()
adapter.hset('user:123', {'name': 'John', 'email': 'john@example.com'})

# Avoid: Raw Redis commands for structured data
redis_conn = get_connection()
redis_conn.hset('user:123', 'name', 'John')
redis_conn.hset('user:123', 'email', 'john@example.com')
```

### 2. Always Set Expiration for Temporary Data

```python
# Good: Set expiration
adapter.set('cache:expensive_query', result, ex=3600)

# Avoid: No expiration on temporary data
adapter.set('cache:expensive_query', result)
```

### 3. Use Pipelines for Batch Operations

```python
# Good: Batch operations
with adapter.pipeline() as pipe:
    for user_id in user_ids:
        pipe.hset(f'user:{user_id}', {'last_seen': time.time()})

# Avoid: Individual operations in loop
for user_id in user_ids:
    adapter.hset(f'user:{user_id}', {'last_seen': time.time()})
```

### 4. Handle Connection Failures

```python
try:
    result = adapter.get('important_key')
    if result is None:
        # Key doesn't exist, handle appropriately
        result = compute_fallback_value()
except Exception as e:
    logit.error(f"Redis operation failed: {e}")
    # Fallback to database or default value
    result = get_from_database()
```

### 5. Use Meaningful Key Names

```python
# Good: Descriptive, hierarchical keys
adapter.set('session:user:123:token', token)
adapter.zadd('leaderboard:game:456', {'player:789': score})

# Avoid: Vague or flat keys
adapter.set('token123', token)
adapter.zadd('scores', {'user789': score})
```

## Examples

### Event Processing with Streams

```python
from mojo.helpers.redis import get_adapter
from mojo.helpers import logit

class EventProcessor:
    def __init__(self, stream_name='events', group_name='processors'):
        self.adapter = get_adapter()
        self.stream = stream_name
        self.group = group_name
        self.consumer = f'worker-{os.getpid()}'
        
        # Ensure consumer group exists
        self.adapter.xgroup_create(self.stream, self.group, mkstream=True)
    
    def publish_event(self, event_type, data):
        """Publish an event to the stream."""
        return self.adapter.xadd(self.stream, {
            'type': event_type,
            'data': data,
            'timestamp': time.time()
        })
    
    def process_events(self):
        """Process events from the stream."""
        while True:
            try:
                messages = self.adapter.xreadgroup(
                    self.group, self.consumer,
                    {self.stream: '>'},
                    count=10, block=1000
                )
                
                for stream, msgs in messages:
                    for msg_id, fields in msgs:
                        try:
                            self.handle_event(msg_id, fields)
                            self.adapter.xack(stream, self.group, msg_id)
                        except Exception as e:
                            logit.error(f"Failed to process {msg_id}: {e}")
                            
            except KeyboardInterrupt:
                break
            except Exception as e:
                logit.error(f"Event processing error: {e}")
                time.sleep(1)
    
    def handle_event(self, msg_id, fields):
        """Handle a specific event."""
        event_type = fields.get('type')
        data = json.loads(fields.get('data', '{}'))
        
        if event_type == 'user_signup':
            self.handle_user_signup(data)
        elif event_type == 'order_placed':
            self.handle_order_placed(data)
        # ... other event types
```

### Caching with Automatic Refresh

```python
from mojo.helpers.redis import get_adapter
import time
import json

class SmartCache:
    def __init__(self, default_ttl=3600):
        self.adapter = get_adapter()
        self.default_ttl = default_ttl
    
    def get_or_set(self, key, factory_func, ttl=None):
        """Get value from cache or compute and cache it."""
        value = self.adapter.get(key)
        
        if value is not None:
            return json.loads(value)
        
        # Compute value
        computed = factory_func()
        
        # Cache with TTL
        self.adapter.set(
            key, 
            json.dumps(computed), 
            ex=ttl or self.default_ttl
        )
        
        return computed
    
    def invalidate(self, pattern):
        """Invalidate cache keys matching pattern."""
        # Note: Use scan for production to avoid blocking
        keys = self.adapter.get_client().keys(pattern)
        if keys:
            self.adapter.delete(*keys)

# Usage
cache = SmartCache()

def get_user_profile(user_id):
    return cache.get_or_set(
        f'profile:user:{user_id}',
        lambda: expensive_profile_query(user_id),
        ttl=1800  # 30 minutes
    )
```

### Rate Limiting

```python
from mojo.helpers.redis import get_adapter
import time

class RateLimiter:
    def __init__(self):
        self.adapter = get_adapter()
    
    def is_allowed(self, key, limit, window_seconds):
        """Check if action is allowed under rate limit."""
        now = time.time()
        window_start = now - window_seconds
        
        # Remove old entries and count current
        pipe = self.adapter.get_client().pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, window_seconds)
        
        results = pipe.execute()
        current_count = results[1]
        
        return current_count < limit

# Usage
limiter = RateLimiter()

def api_endpoint(request):
    user_id = request.user.id
    
    if not limiter.is_allowed(f'rate:api:{user_id}', limit=100, window_seconds=3600):
        return JsonResponse({'error': 'Rate limit exceeded'}, status=429)
    
    # Process request...
```

## Testing

The Django-MOJO framework includes comprehensive tests for Redis pools using the `testit` framework.

### Running Redis Pool Tests

```bash
# Run all Redis pool tests
python -m testit.runner -m test_helpers -t redis_pools

# Run specific test
python -m testit.runner -m test_helpers -t redis_pools.test_redis_base_pool_initialization

# Run with verbose output
python -m testit.runner -v -m test_helpers -t redis_pools
```

### Test Coverage

The Redis pool tests cover:

- **Basic Operations**: Add, remove, checkout, checkin items
- **Concurrent Access**: Thread-safe operations under load
- **Timeout Handling**: Blocking operations with proper timeouts
- **Edge Cases**: Empty pools, non-existent items, error conditions
- **Performance**: Operations with large numbers of items
- **Django Integration**: Model pool functionality with mocked models
- **Shared Connection**: Verification that pools use shared Redis connection

### Prerequisites for Testing

1. **Redis Server**: Must be running and accessible
2. **Django Setup**: Tests use Django models and require Django to be configured
3. **Clean State**: Tests automatically clean up Redis keys with test prefixes

### Test Structure

```python
from testit import helpers as th

@th.django_unit_setup()
def setup_redis_pools(opts):
    """Ensures Redis is available and cleans test data"""
    # Setup code here

@th.django_unit_test()
def test_redis_base_pool_operations(opts):
    """Test basic pool operations"""
    from mojo.helpers.redis.pool import RedisBasePool
    
    pool = RedisBasePool('test_pool')
    pool.add('item1')
    assert pool.checkout('item1') == True
    pool.checkin('item1')
    pool.clear()
```

### Debug Messaging in Tests

All Redis pool tests include comprehensive debug messaging in assertions to make test failures immediately actionable:

#### Poor vs. Good Assertion Messages

**❌ Poor (what to avoid):**
```python
assert len(items) == 3
assert 'item1' in items  
assert pool.checkout('worker1') == True
```

**✅ Good (what we use):**
```python
assert len(items) == 3, f"Expected 3 items, got {len(items)}: {items}"
assert 'item1' in items, f"'item1' not found in items: {items}"
assert pool.checkout('worker1') == True, "Failed to checkout 'worker1'"
```

#### Debug Message Benefits

- **🔍 Faster Debugging**: See actual vs expected values immediately
- **📊 Better Context**: Understand what the test was checking
- **🎯 Precise Errors**: Know exactly which assertion failed and why
- **👥 Team Productivity**: Anyone can understand test failures quickly
- **📝 Living Documentation**: Assert messages explain business logic

#### Common Debug Patterns

```python
# Count assertions
assert len(available) == 2, f"Expected 2 available items, got {len(available)}: {available}"

# Membership assertions  
assert 'item1' in checked_out, f"'item1' should be in checked_out: {checked_out}"

# State assertions
assert result == True, f"Operation should succeed, got: {result}"

# Type assertions
assert isinstance(value, str), f"Expected str, got {type(value)}: {value}"

# Comparison assertions
assert pool.pool_key == 'test_pool', f"Expected pool_key 'test_pool', got '{pool.pool_key}'"
```

#### Example Test Failure Output

With good debug messages, test failures are immediately actionable:

```
AssertionError: Expected 3 available items after checkin, got 2: ['worker2', 'worker3']
```

Instead of just:
```
AssertionError
```

### Mock Testing for Django Models

For Django model pools, tests use mocks to avoid database dependencies:

```python
@th.django_unit_test()
def test_model_pool_operations(opts):
    """Test model pool with mocked Django models"""
    from unittest.mock import Mock, patch
    
    mock_instance = Mock()
    mock_instance.pk = 1
    mock_instance.status = 'active'
    
    with patch.object(MyModel, 'objects') as mock_objects:
        mock_objects.filter.return_value = [mock_instance]
        # Test model pool operations
```

### Best Practices for Testing

1. **Always Clean Up**: Use `pool.clear()` after each test
2. **Use Test Prefixes**: Name pools with 'test_' prefix
3. **Mock External Dependencies**: Use mocks for Django models and external services
4. **Test Concurrency**: Include threading tests for production scenarios
5. **Verify Connections**: Ensure pools use shared Redis configuration

---

For more examples and patterns, see the jobs system documentation which extensively uses Redis streams for job queuing and coordination.