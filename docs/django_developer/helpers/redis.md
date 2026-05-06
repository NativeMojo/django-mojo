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

#### Conditional checkout — `skip_predicate`

`RedisBasePool` accepts an optional `skip_predicate` callable that is consulted on every checkout. Use it to mark a pool member as temporarily ineligible (cooldowns, maintenance flags) without removing it from the pool.

```python
from mojo.helpers.redis import get_connection
from mojo.helpers.redis.pool import RedisBasePool

r = get_connection()

def in_cooldown(str_id):
    return bool(r.exists(f"cooldown:{str_id}"))

pool = RedisBasePool("my_pool", skip_predicate=in_cooldown)
pool.add("worker-1")
pool.add("worker-2")

# After checkin, set a cooldown TTL so worker-1 is skipped for 30s
r.setex("cooldown:worker-1", 30, "1")

with pool.checkout_item(timeout=10) as item:
    # Yields worker-2 first; worker-1 is skipped while its cooldown key is alive
    ...
```

Signature: `(str_id) -> bool | int | float`. Three return shapes are recognised:

| Return value | Meaning |
|---|---|
| `False` / `0` | Eligible — return this candidate now. |
| `True` / `None` / unknown truthy / negative numeric | Skip — try another candidate, no retry-after info. |
| `int` / `float` (> 0) | Temporarily ineligible — pool may retry this candidate after N seconds. |

**Wallclock-budget contract (numeric returns):** when the predicate returns a positive number, `timeout` is treated as the maximum wallclock time the call may take. The pool holds the deferred candidate out of the available list and `time.sleep`s until the soonest retry-after matures (capped at 1 second per sleep so peer checkins from other workers are observed quickly). All deferred items are republished to the available list when the call exits — peers are never starved. If the budget elapses before any candidate becomes eligible, `get_next_available` returns `None`.

**Bool-only carve-out (no retry signal):** when the predicate returns only `True` / `False`, the pool sweeps the available list once (bounded by the current pool size) and returns `None` if every candidate said skip — even if `timeout` is large. `True` carries no information about *when* the candidate might become eligible, so the pool cannot meaningfully wait. Return a numeric retry-after (seconds) to opt into the wallclock-budget behaviour.

```python
def cooldown(str_id):
    ttl = r.ttl(f"cooldown:{str_id}")
    if ttl is None or ttl <= 0:
        return False  # eligible
    return float(ttl)  # retry after this many seconds

pool = RedisBasePool("my_pool", skip_predicate=cooldown)
# get_next_available(timeout=30) will sleep through cooldown windows up to 30s
```

Bool-skip candidates are `lpush`ed back to the head of the list so other items (at the tail) are tried first by `brpop`. Predicate exceptions are caught, logged via `logit.exception`, and treated as skip (conservative default — a buggy predicate cannot return a poisoned id).

`checkout_specific_item` bypasses the predicate by design — explicit checkouts of a known id always succeed.

> **Ordering note:** skipped items move to the head of the list, so they are tried last after every other available member. This is a small shift away from strict FIFO — relevant only if your consumer relies on insertion order.

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

# Initialize from database (idempotent — no-op if already initialized)
pool.init_pool()

# Force rebuild from queryset (wipes any items added via add_to_pool that
# fall outside query_dict; use sparingly — typically only when the underlying
# DB rows have changed and you want the pool refreshed from scratch)
pool.init_pool(force=True)

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
pool.add_to_pool(new_key)                    # lazy-inits pool if uninitialized
pool.remove_from_pool(old_key)               # only if available; no auto-init
pool.remove_from_pool(old_key, force=True)   # even if checked out

# Inspect
pool.list_checked_out_instances()  # returns queryset of checked-out models
```

The model pool validates instances on checkout — if a record was deleted or no longer matches `query_dict`, it is silently removed and the next available item is returned.

#### Initialization contract

- **`is_ready()`** — returns `True` once the pool has been initialized. The check is on the membership set only, so the pool is still considered ready when every member is currently checked out (the available list goes empty during normal operation; the set persists for the pool's lifetime).
- **`init_pool(force=False)`** — idempotent. When `is_ready()` is already `True`, the call is a no-op and does not destroy the existing pool. When the pool is uninitialized (cold start or after `destroy_pool()`), the method acquires a Redis lock at `{pool_key}:init_lock`, double-checks readiness, then runs `destroy_pool()` + per-item rebuild from `query_dict`. Concurrent first-time inits across processes are safe — only one rebuild runs; the others poll briefly for it to finish.
- **`init_pool(force=True)`** — always rebuilds from the queryset. Use this when the underlying DB rows have changed and you want a fresh pool. Items added via `add_to_pool()` that fall outside `query_dict` are wiped on a forced rebuild; preserve them across non-forced calls.
- **Lazy init** — `add_to_pool()` and `get_next_instance()` call `init_pool()` (idempotent) before their work. A cold or destroyed pool is populated automatically on first use. `remove_from_pool()` does NOT lazy-init — it returns `False` when the pool is uninitialized, since rebuilding the pool from the DB queryset just to remove an item would silently undo a `destroy_pool()`.
- **Thread-safety** — all mutation paths go through atomic Redis operations and (for the destructive rebuild) a Redis lock with a 10s TTL safety net. The pool tolerates lock-holder crashes: the next caller after the TTL expires acquires the lock and reinitialises.

#### Conditional checkout — `skip_predicate`

`RedisModelPool` accepts an optional `skip_predicate` callable applied to the loaded instance after the `query_dict` recheck. Use it to mark instances as temporarily ineligible without removing them from the pool.

```python
from mojo.helpers.redis import get_connection
from mojo.helpers.redis.pool import RedisModelPool

r = get_connection()

pool = RedisModelPool(
    model_cls=ApiKey,
    query_dict={"status": "active"},
    pool_key="apikey_pool",
    skip_predicate=lambda key: bool(r.exists(f"cooldown:apikey:{key.pk}")),
)

with pool.checkout_instance(timeout=10) as key:
    # Skipped while its per-pk cooldown TTL key is alive
    ...
    # On checkin, mark a 30s cooldown so other workers prefer different keys
    r.setex(f"cooldown:apikey:{key.pk}", 30, "1")
```

Signature: `(instance) -> bool | int | float`. Same three return shapes as `RedisBasePool.skip_predicate`:

| Return value | Meaning |
|---|---|
| `False` / `0` | Eligible — return this instance now. |
| `True` / `None` / unknown truthy / negative numeric | Skip — try another candidate, no retry-after info. |
| `int` / `float` (> 0) | Temporarily ineligible — pool may retry this instance after N seconds. |

**Wallclock-budget contract:** for numeric returns, `timeout` is the maximum wallclock time the call may take. The pool holds the deferred instance out of the available list and sleeps until the soonest retry-after matures (capped at 1s per sleep). Deferred instances are always republished on exit. The deadline also bounds the existing stale-row retry path (`DoesNotExist` / `query_dict` mismatch) — slow stale-row recovery cannot blow the budget.

**Bool-only carve-out:** with bool-only returns, the pool sweeps once (bounded by the current pool size) and returns `None` if every member is `True`. Use a numeric retry-after to opt into waiting.

```python
def cooldown(instance):
    ttl = r.ttl(f"cooldown:apikey:{instance.pk}")
    if ttl is None or ttl <= 0:
        return False
    return float(ttl)

pool = RedisModelPool(
    model_cls=ApiKey,
    query_dict={"status": "active"},
    pool_key="apikey_pool",
    skip_predicate=cooldown,
)
# get_next_instance(timeout=30) sleeps through per-key cooldowns up to 30s
```

`get_specific_instance` and `checkout_specific_instance` bypass the predicate by design — admin or non-customer paths can force access to a specific instance regardless of its eligibility state.

> **Ordering note:** skipped items move to the head of the list, so they are tried last after every other available member. This is a small shift away from strict FIFO — relevant only if your consumer relies on insertion order.

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
