# IP blocks recorded in DB but not applied to iptables + repeated blocking

**Type**: bug
**Status**: resolved
**Date**: 2026-03-30
**Severity**: critical

## Description

Production shows `firewall:block` log entries with block_count climbing rapidly (132→141 in ~1 second) for the same IP, yet `iptables -L` shows zero rules. The execute_handler job completes successfully (status=completed, 35ms, no errors), confirming the DB side works end-to-end. The problem is somewhere between the `broadcast_execute()` call and iptables application.

### Confirmed problems

1. **Repeated blocking of already-blocked IPs** — `geo.block()` has no idempotency check. It doesn't check `self.is_blocked` before re-blocking, so every event from the same IP triggers a new block call, incrementing `block_count`, writing duplicate logs, and firing redundant broadcasts.

2. **Block reason has no incident/event reference** — `BlockHandler` passes `reason="auto:ruleset"` with no incident or event ID, making it hard to trace which incident triggered the block.

3. **Block handler doesn't resolve the incident** — when a `block://` handler fires and successfully blocks an IP, the incident remains in whatever state it was in. It should be resolved (or at least have a history entry recording the action).

4. **iptables rules never applied (CONFIRMED)** — `geo.block()` calls `jobs.broadcast_execute("...asyncjobs.block_ip", data)` which is fire-and-forget via Redis pub/sub. Runner logs confirm: `AttributeError: 'dict' object has no attribute 'payload'` — the broadcast functions receive a plain dict but try to access `job.payload`.

5. **Broadcast functions need clear naming convention** — broadcast functions receive a plain dict (not a Job), but nothing in their name or signature makes this obvious. Rename with `broadcast_` prefix and add docstrings to prevent future mistakes.

## Context

The DB side of blocking works — `GeoLocatedIP.is_blocked=True`, `blocked_until` is set, `block_count` increments, logit entries are written. The execute_handler job completes successfully. But the fleet-wide iptables enforcement never happens.

The `broadcast_execute` is fire-and-forget: it publishes to Redis pub/sub with no persistence, no acknowledgement, and no retry. Any failure on the runner side is invisible from the web/DB layer. The root cause could be in the broadcast receive path, the runner subscription, or the firewall execution — **runner-side logs are needed to narrow this down**.

The repeated blocking is a confirmed secondary issue — without idempotency, rapid-fire events from the same IP each trigger the handler independently.

## Acceptance Criteria

### 1. Fix and rename broadcast functions
- Rename `block_ip` → `broadcast_block_ip`, `unblock_ip` → `broadcast_unblock_ip`, `sync_ipset` → `broadcast_sync_ipset`, `remove_ipset` → `broadcast_remove_ipset`
- Change param from `job` to `data`, use `data.get(...)` directly
- Add docstring: `"""Broadcast handler — receives plain dict from pub/sub, not a Job."""`
- Replace any `job.add_log()` calls with `logit`
- Update all `broadcast_execute()` call sites to reference new function names

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

**Confirmed (repeated blocking)**: `geo.block()` at `geolocated_ip.py:326` unconditionally sets `is_blocked=True` and increments `block_count` without checking if already blocked. Production data confirms this (block_count 132→141 in ~1 second).

**Confirmed (iptables empty)**: Runner logs show `AttributeError: 'dict' object has no attribute 'payload'` in `asyncjobs.block_ip`. The broadcast system passes a plain dict but the function tries to access `job.payload`. All four broadcast functions in `asyncjobs.py` have this same bug.

**Confidence**: high for all confirmed problems

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

### Step 1: Fix and rename broadcast functions
Rename all four broadcast functions with `broadcast_` prefix, change param to `data`, use `data.get(...)`. Replace `job.add_log()` with `logit`. Update all `broadcast_execute()` call sites.

### Step 2: Add idempotency to geo.block()
Early return if `self.is_blocked and not self.is_expired` — skip re-block, don't increment count, don't re-broadcast.

### Step 3: Include incident/event in block reason
Pass event/incident info through to `geo.block()` reason string.

### Step 4: Record block action on incident
Add `incident.add_history()` call when block succeeds. Consider auto-resolve param.

## Resolution

**Status**: resolved
**Date**: 2026-03-31

### What Was Built
Fixed broadcast functions that crashed with `AttributeError: 'dict' object has no attribute 'payload'` because they expected a Job instance but broadcast_execute passes a plain dict. Added block idempotency, incident/event traceability in block reasons, and auto-incident-resolution on successful block.

### Files Changed
- `mojo/apps/incident/asyncjobs.py` — Renamed 4 broadcast functions with `broadcast_` prefix, changed param to `data`, replaced `job.add_log()` with `logit`
- `mojo/apps/account/models/geolocated_ip.py` — Added idempotency guard to `block()`, updated 3 broadcast_execute call sites
- `mojo/apps/incident/handlers/event_handlers.py` — BlockHandler builds reason with incident/event IDs, auto-resolves incident after block
- `mojo/apps/incident/models/ipset.py` — Updated 2 broadcast_execute call sites
- `docs/django_developer/logging/incidents.md` — Updated function references and documented new behavior
- `docs/django_developer/security/README.md` — Updated blocking flow and broadcast job table
- `docs/django_developer/account/geoip.md` — Documented idempotency in block flow
- `CHANGELOG.md` — Added v1.1.4 entries

### Tests
- `tests/test_incident/broadcast_and_block.py` — 11 tests covering broadcast dict acceptance, block idempotency, expiry re-block, whitelist refusal, reason traceability, incident auto-resolve, and skip-resolve-if-already-resolved
- Run: `bin/run_tests -t test_incident.broadcast_and_block`

### Full Suite
- 1073 total, 1034 passed, 39 skipped, 0 failed

### Security Review
- Race condition in idempotency check under concurrent workers (pre-existing pattern, follow-up)
- Broadcast errors silently swallowed with `except Exception: pass` (follow-up: add logit.exception)
- No critical findings

### Follow-up
- Add `select_for_update()` or atomic conditional update to `geo.block()` for race-safe idempotency
- Replace `except Exception: pass` in broadcast calls with `logit.exception(...)` for observability
- Consider structured separator for reason string to avoid ambiguity with colons
