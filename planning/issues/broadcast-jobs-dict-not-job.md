# IP blocks recorded in DB but not applied to iptables + repeated blocking

**Type**: bug
**Status**: open
**Date**: 2026-03-30
**Severity**: critical

## Description

Production shows `firewall:block` log entries with block_count climbing rapidly (132→141 in ~1 second) for the same IP, yet `iptables -L` shows zero rules. Three related problems:

1. **iptables rules never applied** — `geo.block()` updates the DB and logs successfully, then calls `jobs.broadcast_execute("...asyncjobs.block_ip", data)`. The broadcast function `block_ip(job)` calls `job.payload` but receives a plain dict from the broadcast system, causing `AttributeError` on the runner. The error is logged to the runner's logger (job_engine.py:414) but not to the web/DB logs, so it's invisible from the admin side.

2. **Repeated blocking of already-blocked IPs** — `geo.block()` has no idempotency check. It doesn't check `self.is_blocked` before re-blocking, so every event from the same IP triggers a new block call, incrementing `block_count`, writing duplicate logs, and firing redundant broadcasts.

3. **Block reason has no incident/event reference** — `BlockHandler` passes `reason="auto:ruleset"` with no incident or event ID, making it hard to trace which incident triggered the block.

4. **Block handler doesn't resolve the incident** — when a `block://` handler fires and successfully blocks an IP, the incident remains in whatever state it was in. It should be resolved (or at least have a history entry recording the action).

## Context

The DB side of blocking works — `GeoLocatedIP.is_blocked=True`, `blocked_until` is set, `block_count` increments, logit entries are written. But the fleet-wide iptables enforcement never happens because the broadcast function crashes. This means the security pipeline detects threats and records them but fails to actually enforce the block at the network level.

The repeated blocking is a secondary issue — without the catch-all bundling or dedup, rapid-fire events from the same IP each trigger the handler independently.

## Acceptance Criteria

### 1. Fix broadcast functions to accept dict (not Job)
- `block_ip`, `unblock_ip`, `sync_ipset`, `remove_ipset` in `asyncjobs.py` receive a dict from `broadcast_execute`, not a Job instance
- They should use `data.get(...)` directly, not `data.payload`

### 2. Add idempotency to `geo.block()`
- If `self.is_blocked` is already True and the block hasn't expired, skip the re-block
- Don't increment `block_count`, don't write duplicate logs, don't re-broadcast
- Return True (already blocked) without side effects

### 3. Include incident/event reference in block reason
- `BlockHandler.run()` should include event ID and/or incident ID in the reason string
- e.g. `"auto:ruleset:event:123:incident:456"`

### 4. Block handler should record action on incident
- When `BlockHandler.run()` succeeds, add incident history noting the block
- Consider auto-resolving the incident (or making this configurable via handler params)

## Investigation

**Likely root cause (iptables empty)**: `broadcast_execute` at `manager.py:396` calls `func(message.get('data', {}))` passing a plain dict. But `block_ip` at `asyncjobs.py:29` calls `job.payload` which fails because dicts have no `.payload` attribute. The exception is caught at `job_engine.py:413-414` and logged to the runner logger only.

**Likely root cause (repeated blocking)**: `geo.block()` at `geolocated_ip.py:326` unconditionally sets `is_blocked=True` and increments `block_count` without checking if already blocked.

**Confidence**: high — code analysis confirms both paths

**Code path (iptables)**:
1. `event_handlers.py:354` — `geo.block(reason=reason, ttl=ttl)`
2. `geolocated_ip.py:357-362` — `jobs.broadcast_execute("mojo.apps.incident.asyncjobs.block_ip", {"ips": [...]})`
3. `manager.py:486-487` — fire-and-forget Redis pub/sub
4. `job_engine.py:396` — `func(message.get('data', {}))` — passes dict
5. `asyncjobs.py:29` — `job.payload` crashes — dict has no `.payload`
6. `job_engine.py:413-414` — exception logged to runner logger only

**Code path (repeated blocking)**:
1. Multiple OSSEC events arrive for same IP within seconds
2. Each triggers rule match → `BlockHandler.run()` → `geo.block()`
3. `geo.block()` has no `if self.is_blocked: return True` guard
4. block_count increments 132→141 in ~1 second

**Regression test**: not feasible — requires mocking broadcast system and firewall

**Related files**:
- `mojo/apps/incident/asyncjobs.py:18-48` — fix `block_ip`, `unblock_ip`, `sync_ipset`, `remove_ipset` to accept dict
- `mojo/apps/account/models/geolocated_ip.py:311-365` — add idempotency guard to `block()`
- `mojo/apps/incident/handlers/event_handlers.py:341-357` — include event/incident in reason, record on incident
- `mojo/apps/jobs/job_engine.py:396` — confirms broadcast calling convention

## Plan

### Step 1: Fix broadcast functions to accept dict
Rename param to `data`, use `data.get(...)` directly for all four broadcast functions.

### Step 2: Add idempotency to geo.block()
Early return if `self.is_blocked and not self.is_expired` — skip re-block, don't increment count, don't re-broadcast.

### Step 3: Include incident/event in block reason
Pass event/incident info through to `geo.block()` reason string.

### Step 4: Record block action on incident
Add `incident.add_history()` call when block succeeds. Consider auto-resolve param.
