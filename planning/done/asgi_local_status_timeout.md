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

`check_database()` and `check_redis()` accepted a `timeout` parameter but never used it.
The DB cursor had no statement timeout, and the Redis client had no socket timeout.

## Fix

- **Database**: `signal.SIGALRM` alarm wraps the cursor call — kills it after `timeout` seconds
- **Redis**: `socket_timeout` and `socket_connect_timeout` set on the connection pool before ping
- **Default timeout**: reduced from 5s to 3s

## Files Changed

- `mojo/apps/account/management/commands/status.py`
