---
type: bug
status: resolved
date: 2026-03-22
resolved: 2026-03-22
---

# manage.py status hangs when DB or Redis is unreachable

## Problem

`./bin/manage.py status` hangs indefinitely in production when the database or Redis
is misconfigured or unreachable. The `--timeout` flag existed but was never applied
to the actual connections — both `cursor.execute("SELECT 1")` and `redis_client.ping()`
blocked forever with no timeout.

## Root Cause

Two issues:

1. **Circular dependency in Redis client**: `get_connection()` called `settings.get()`
   (DB-backed) to read Redis config. `settings.get()` calls `Setting.resolve()` which
   tries to read from Redis cache — which requires the connection we're trying to build.
   This caused timeouts and retries on every Redis setting lookup during first connection.

2. **No timeout on status checks**: `check_database()` and `check_redis()` accepted a
   `timeout` parameter but never applied it to the actual connections.

## Fix

- **Redis client circular dependency**: Changed `_build_url()` and `get_connection()`
  to use `settings.get_static()` (file-only, no DB/Redis) instead of `settings.get()`
  for all Redis connection config. Redis config can't come from a Redis-backed store.

- **Database timeout**: `signal.SIGALRM` alarm wraps the cursor call — kills it after
  `timeout` seconds

- **Redis timeout**: `socket_timeout` and `socket_connect_timeout` set on the connection
  pool before ping

- **Default timeout**: reduced from 5s to 3s

## Files Changed

- `mojo/helpers/redis/client.py` — `settings.get()` → `settings.get_static()` for all Redis config
- `mojo/apps/account/management/commands/status.py` — actual timeout enforcement
