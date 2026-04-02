# Redis — Django Developer Reference

Redis helpers live in `mojo/helpers/redis/`. They provide connection management, a high-level adapter for common operations, and resource pooling.

## Quick Start

```python
from mojo.helpers.redis import get_connection

r = get_connection()
r.set("mykey", "hello", ex=3600)
value = r.get("mykey")  # "hello"
```

`get_connection()` returns a thread-safe `redis.Redis` (or `redis.cluster.RedisCluster`) client backed by a connection pool. Call it from anywhere — it returns the same singleton per process.

`get_client()` is an alias for `get_connection()`.

---

## Settings

All settings are optional. Configure in `settings.py` or via environment variables through the settings helper.

| Setting | Default | Description |
|---------|---------|-------------|
| `REDIS_URL` | — | Full URL (e.g. `redis://localhost:6379/0`). If set, all other connection settings are ignored |
| `REDIS_SERVER` | `"localhost"` | Hostname |
| `REDIS_PORT` | `6379` | Port |
| `REDIS_DB_INDEX` | `0` | Database index |
| `REDIS_USERNAME` | — | ACL username (Serverless Valkey / Redis 6+) |
| `REDIS_PASSWORD` | — | ACL password |
| `REDIS_SCHEME` | `"rediss"` | `"redis"` or `"rediss"` (TLS). Auto-set to `"redis"` when host contains `localhost` |
| `REDIS_MAX_CONN` | `500` | Connection pool size per process |
| `REDIS_CONNECT_TIMEOUT` | `2` | Connection timeout in seconds |
| `REDIS_SOCKET_TIMEOUT` | `60` | Socket timeout in seconds |
| `REDIS_CLUSTER` | `False` | Set `True` to use `RedisCluster` client (skip auto-detection) |
| `REDIS_READ_FROM_REPLICAS` | `"1"` | Read from replica nodes (cluster mode only) |

### Local Development

```python
REDIS_URL = "redis://localhost:6379/0"
```

### Production (AWS Serverless Valkey / ElastiCache)

```python
REDIS_SERVER = "xxx.serverless.use1.cache.amazonaws.com"
REDIS_PORT = 6379
REDIS_USERNAME = "default"
REDIS_PASSWORD = "your-acl-token"
REDIS_SCHEME = "rediss"
REDIS_CLUSTER = True
```

---

## RedisAdapter

The adapter wraps common Redis data structures with automatic JSON serialization and connection management. Get the framework singleton:

```python
from mojo.helpers.redis import get_adapter

adapter = get_adapter()
```

### Key / Value

```python
# Store a string (expires in 1 hour)
adapter.set("session:abc", "user_42", ex=3600)

# Store a dict (auto-serialized to JSON)
adapter.set("config:feature_flags", {"dark_mode": True, "beta": False}, ex=600)

# Retrieve
value = adapter.get("session:abc")       # "user_42"
flags = adapter.get("config:feature_flags")  # '{"dark_mode": true, "beta": false}'

# Conditional set — only if key doesn't exist (useful for locks)
acquired = adapter.set("lock:report", "worker-1", ex=30, nx=True)

# Check existence and TTL
adapter.exists("session:abc")   # 1
adapter.ttl("session:abc")      # seconds remaining (-1 = no expiry, -2 = doesn't exist)

# Delete
adapter.delete("session:abc")

# Update expiration
adapter.expire("config:feature_flags", 1800)
adapter.pexpire("config:feature_flags", 1800000)  # milliseconds
```

### Hashes

Store structured fields under a single key. Good for objects with many attributes.

```python
# Set fields (dicts/lists auto-serialized, bools become "1"/"0")
adapter.hset("user:42", {
    "name": "Alice",
    "email": "alice@example.com",
    "prefs": {"theme": "dark"},
    "active": True,
})

# Get one field
adapter.hget("user:42", "name")  # "Alice"

# Get all fields
adapter.hgetall("user:42")  # {"name": "Alice", "email": "alice@example.com", ...}

# Delete fields
adapter.hdel("user:42", "prefs")
```

### Sorted Sets

Ordered by score. Useful for leaderboards, scheduled jobs, rate limiting windows.

```python
# Add members with scores
adapter.zadd("leaderboard", {"alice": 100.0, "bob": 85.0, "carol": 92.0})

# Get score
adapter.zscore("leaderboard", "alice")  # 100.0

# Get members by score range
adapter.zrangebyscore("leaderboard", 80.0, 95.0)  # ["bob", "carol"]
adapter.zrangebyscore("leaderboard", 80.0, 95.0, limit=1)  # ["bob"]

# Pop lowest score
adapter.zpopmin("leaderboard")  # [("bob", 85.0)]

# Count and remove
adapter.zcard("leaderboard")         # 2
adapter.zrem("leaderboard", "carol") # 1
```

### Lists

Simple FIFO/LIFO queues.

```python
# Push to list
adapter.rpush("queue:emails", "msg1", "msg2", "msg3")

# Blocking pop (waits up to 5 seconds)
result = adapter.brpop(["queue:emails"], timeout=5)  # ("queue:emails", "msg3")

# Check length
adapter.llen("queue:emails")  # 2
```

### Pub/Sub

Publish messages to channels for real-time fan-out.

```python
# Publish (dicts auto-serialized to JSON)
adapter.publish("notifications", {"type": "alert", "msg": "Deploy complete"})

# Subscribe (returns a PubSub object from redis-py)
ps = adapter.pubsub()
ps.subscribe("notifications")
for message in ps.listen():
    if message["type"] == "message":
        print(message["data"])
```

### Streams

Redis Streams for durable, ordered message processing with consumer groups.

```python
# Add to stream (dicts/lists in fields auto-serialized)
msg_id = adapter.xadd("events", {"action": "login", "user_id": "42"}, maxlen=10000)

# Create consumer group
adapter.xgroup_create("events", "my-group", id="0")

# Read new messages as a consumer
messages = adapter.xreadgroup(
    "my-group", "worker-1",
    {"events": ">"},
    count=10,
    block=5000,
)

# Acknowledge processed messages
adapter.xack("events", "my-group", msg_id)

# Claim stale messages from other consumers
adapter.xclaim("events", "my-group", "worker-2", min_idle=60000, msg_id)

# Inspect pending messages
adapter.xpending("events", "my-group")  # summary
adapter.xpending("events", "my-group", start="-", end="+", count=10)  # detailed

# Stream info
adapter.xinfo_stream("events")
```

### Pipelines

Batch multiple commands in a single round-trip.

```python
with adapter.pipeline(transaction=False) as pipe:
    pipe.set("counter:a", 1)
    pipe.set("counter:b", 2)
    pipe.incr("counter:a")
    # All commands execute on context exit
```

Use `transaction=True` (default) for atomic MULTI/EXEC. Use `transaction=False` for non-atomic batching (better for metrics, logging, or cross-slot operations in cluster mode).

### Health Check

```python
adapter.ping()  # True if Redis is reachable
```

---

## Resource Pools

`mojo.helpers.redis.pool` provides checkout/checkin resource pooling backed by Redis. Two classes are available:

### RedisBasePool — String ID Pool

Manage a pool of arbitrary string identifiers.

```python
from mojo.helpers.redis.pool import RedisBasePool

pool = RedisBasePool("my_pool", default_timeout=30)

# Add items
pool.add("worker-1")
pool.add("worker-2")
pool.add("worker-3")

# Checkout / checkin (context manager — auto-returns on exit)
with pool.checkout_item(timeout=10) as item:
    print(f"Got: {item}")  # "worker-3" (or whichever is available)
    # Item is automatically returned when the block exits

# Checkout a specific item
with pool.checkout_specific_item("worker-1", timeout=5) as item:
    print(f"Got: {item}")

# Manual checkout / checkin
pool.checkout("worker-2")
# ... do work ...
pool.checkin("worker-2")

# Blocking wait for next available
item = pool.get_next_available(timeout=10)  # blocks until one is free

# Inspect pool state
pool.list_all()          # all items (set)
pool.list_available()    # available items (list)
pool.list_checked_out()  # checked-out items (set difference)

# Remove items
pool.remove("worker-1")              # only if available
pool.remove("worker-1", force=True)  # even if checked out

# Maintenance
pool.remove_duplicates()  # deduplicate the available list
pool.clear()              # remove all items
pool.destroy_pool()       # delete Redis keys entirely
```

### RedisModelPool — Django Model Pool

Manages a pool of Django model instances by primary key. Useful for distributing work across a set of model records (e.g. API keys, worker configs, external accounts).

```python
from mojo.helpers.redis.pool import RedisModelPool

# Pool of active ApiKey instances
pool = RedisModelPool(
    model_cls=ApiKey,
    query_dict={"status": "active"},
    pool_key="apikey_pool",
    default_timeout=30,
)

# Initialize from database (clears and rebuilds)
pool.init_pool()

# Checkout an instance (context manager)
with pool.checkout_instance(timeout=10) as key:
    print(f"Using API key: {key.pk}")
    # Instance is returned to pool on exit

# Checkout a specific instance
with pool.checkout_specific_instance(my_key) as key:
    print(f"Using: {key.pk}")

# Manual checkout / return
instance = pool.get_next_instance(timeout=10)
# ... do work ...
pool.return_instance(instance)

# Add / remove instances dynamically
pool.add_to_pool(new_key)                    # auto-inits pool if needed
pool.remove_from_pool(old_key)               # only if available
pool.remove_from_pool(old_key, force=True)   # even if checked out

# Inspect
pool.list_checked_out_instances()  # returns queryset of checked-out models
```

The model pool validates instances on checkout — if a record was deleted or no longer matches `query_dict`, it is silently removed and the next available item is returned.

---

## Django Cache Backend

For Django's cache framework (`django.core.cache`), use the Mojo Redis-backed backend. See [Django Cache Backend](../core/cache.md) for configuration.

---

## Direct Client Access

For operations not covered by the adapter (e.g. `SCAN`, `GETSET`, Lua scripting), use the raw client:

```python
from mojo.helpers.redis import get_connection

r = get_connection()
r.incr("counter")
r.srandmember("myset")
```

The client is a standard `redis.Redis` (or `redis.cluster.RedisCluster`) instance from redis-py. All redis-py methods are available.
