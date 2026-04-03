# Use ipset for permanent IP blocks

**Type**: request
**Status**: resolved
**Date**: 2026-03-31
**Priority**: high

## Description

Permanent IP blocks (TTL=None) should use a dedicated ipset (`mojo_blocked`) instead of individual iptables rules. This ipset is synced at runner startup and reconciled by the sweep cron, ensuring new servers and servers that missed broadcasts always have the full block list. TTL-based blocks remain as individual iptables rules via broadcast — they expire soon and aren't worth the complexity.

## Context

The current blocking system broadcasts individual `firewall.block(ip)` calls which add per-IP iptables rules. This has three problems:

1. **New servers start with empty iptables** — no mechanism to sync existing blocks at startup
2. **Failed broadcasts are lost** — fire-and-forget pub/sub means a Redis hiccup drops blocks silently
3. **Individual iptables rules don't scale** — hundreds of blocked IPs means hundreds of rules, each checked sequentially per packet. ipset uses a hash table and is O(1) per lookup regardless of set size.

The existing `IPSet` model and `firewall.ipset_load()` already provide the infrastructure for ipset-based blocking (used for country/datacenter blocks). This request extends that pattern to incident-driven permanent blocks.

## Acceptance Criteria

- Permanent blocks (`geo.block(ttl=None)`) add the IP to a `mojo_blocked` ipset instead of individual iptables rules
- TTL blocks (`geo.block(ttl=600)`) continue using individual iptables rules as today
- When a permanent block is unblocked, it is removed from the `mojo_blocked` ipset
- Runner startup syncs the `mojo_blocked` ipset from DB (all permanently blocked IPs)
- The sweep cron reconciles the `mojo_blocked` ipset from DB truth every cycle
- A `firewall.ipset_add(name, ip)` and `firewall.ipset_del(name, ip)` function exists for single-IP ipset operations (avoids full flush+reload on every block)

## Investigation

**What exists**:
- `firewall.ipset_load(name, cidrs)` — bulk create/flush/reload an ipset + attach iptables rule. Used by `IPSet.sync()` and `broadcast_sync_ipset()`.
- `firewall.ipset_remove(name)` — remove an ipset and its iptables rule
- `IPSet` model — manages named ipsets (country, datacenter, abuse). Has `sync()` method that broadcasts to fleet.
- `broadcast_sync_ipset(data)` — broadcast handler that calls `firewall.ipset_load()` on each runner
- `sweep_expired_blocks(job)` — cron that finds expired blocks and broadcasts unblock. Currently only handles expiry, not reconciliation.
- `job_engine.initialize()` — engine startup hook. Currently starts heartbeat, control listener, signal handlers. No configurable startup callbacks.
- `GeoLocatedIP.block()` — currently broadcasts `broadcast_block_ip` with individual iptables for all blocks regardless of TTL

**What changes**:

| File | Change |
|---|---|
| `mojo/apps/incident/firewall.py` | Add `ipset_add(name, ip)` and `ipset_del(name, ip)` for single-IP operations. Add `ipset_sync_blocked(ips)` convenience wrapper that loads `mojo_blocked` ipset. |
| `mojo/apps/incident/asyncjobs.py` | Add `broadcast_sync_blocked_ips(data)` handler. Modify `broadcast_block_ip` to route permanent blocks to ipset. Add `sync_blocked_ips(job)` startup/cron function. |
| `mojo/apps/account/models/geolocated_ip.py` | `block()`: if ttl=None, broadcast `broadcast_ipset_add_blocked` instead of `broadcast_block_ip`. `unblock()`: if was permanent, broadcast `broadcast_ipset_del_blocked`. |
| `mojo/apps/incident/asyncjobs.py` | `sweep_expired_blocks`: add reconciliation step that rebuilds `mojo_blocked` ipset from DB truth (only permanently blocked IPs) |
| `mojo/apps/jobs/job_engine.py` | Add `STARTUP_JOBS` setting — list of dotted function paths to run once at `initialize()`. Default includes `mojo.apps.incident.asyncjobs.sync_blocked_ips`. |

**Constraints**:
- `ipset add` with `-exist` flag is idempotent — safe to call multiple times
- ipset `hash:ip` type is better than `hash:net` for individual IPs (more memory efficient)
- The reconciliation must not disrupt existing TTL blocks in iptables
- `mojo_blocked` ipset must be created with `hash:ip` type (not `hash:net`) since these are individual IPs not CIDRs
- Startup sync should not block the engine from starting to process jobs — run in a thread or as first queued job
- The `mojo_blocked` ipset name should be configurable via `FIREWALL_BLOCKED_IPSET_NAME` setting

**Related files**:
- `mojo/apps/incident/firewall.py` — iptables/ipset management
- `mojo/apps/incident/asyncjobs.py` — broadcast handlers and cron jobs
- `mojo/apps/account/models/geolocated_ip.py` — `block()` and `unblock()` methods
- `mojo/apps/incident/models/ipset.py` — existing IPSet model (reference pattern)
- `mojo/apps/jobs/job_engine.py` — engine startup (`initialize()`)

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `FIREWALL_BLOCKED_IPSET_NAME` | `"mojo_blocked"` | Name of the ipset for permanent blocks |

## Tests Required

- `broadcast_block_ip` routes permanent blocks (ttl=0) to ipset add, TTL blocks to iptables
- `broadcast_unblock_ip` removes from ipset for permanent blocks
- `sync_blocked_ips` queries correct IPs (permanent only) and calls `ipset_load`
- `sweep_expired_blocks` reconciles ipset from DB truth
- `firewall.ipset_add` and `firewall.ipset_del` work for single IPs
- Idempotency: adding same IP twice to ipset doesn't error
- New server startup: `sync_blocked_ips` called during `initialize()`

## Out of Scope

- Changing TTL block mechanism (stays as individual iptables rules)
- Migrating existing `IPSet` model records to use this pattern (they already work fine with `hash:net`)
- REST API changes (this is all internal plumbing)
- Persistent iptables rules across reboots (startup sync handles this)

## Plan

**Status**: planned
**Planned**: 2026-04-03

### Objective

Route permanent IP blocks through a `mojo_blocked` ipset (O(1) lookup), add hourly reconciliation that doubles as startup recovery, and reduce sweep frequency to every 5 minutes.

### Steps

1. **`mojo/apps/incident/firewall.py`** — Add two single-IP ipset functions (lines ~145+):
   - `ipset_add(name, ip)` — `ipset add <name> <ip> -exist`. Creates the set (`hash:net -exist`) if it doesn't exist. Returns True/False.
   - `ipset_del(name, ip)` — `ipset del <name> <ip> -exist`. Returns True/False.
   - Both validate name and IP using existing `_validate_ip` / `_validate_ipset_name`.
   - Also ensure the iptables DROP rule exists for the set (same pattern as `ipset_load` line 183-185).

2. **`mojo/apps/incident/asyncjobs.py`** — Add broadcast handlers and sync job:
   - `broadcast_ipset_add_blocked(data)` — Receives `{"ip": "1.2.3.4"}`, calls `firewall.ipset_add("mojo_blocked", ip)`. Broadcast handler (plain dict, not Job).
   - `broadcast_ipset_del_blocked(data)` — Receives `{"ip": "1.2.3.4"}`, calls `firewall.ipset_del("mojo_blocked", ip)`. Broadcast handler.
   - `sync_firewall(job)` — Hourly reconciliation job:
     1. Query `GeoLocatedIP.objects.filter(is_blocked=True, blocked_until__isnull=True)` for all permanent blocks → list of IPs
     2. Call `firewall.ipset_load("mojo_blocked", ips)` to flush+reload the ipset from DB truth
     3. Query all enabled `IPSet` records → for each, call `firewall.ipset_load(ipset.name, ipset.cidrs)`
     4. Log counts via `job.add_log()`
   - Read `FIREWALL_BLOCKED_IPSET_NAME` from settings (default `"mojo_blocked"`) at module level.

3. **`mojo/apps/account/models/geolocated_ip.py`** — Modify `block()` and `unblock()`:
   - **`block()` (line 374-382)**: When `ttl` is 0/None (permanent), broadcast `broadcast_ipset_add_blocked` with `{"ip": self.ip_address}` instead of `broadcast_block_ip`. When `ttl` > 0, keep existing `broadcast_block_ip` behavior.
   - **`unblock()` (line 407-414)**: Check if the block was permanent (`self.blocked_until is None` before the save clears it — capture this before the update). If permanent, broadcast `broadcast_ipset_del_blocked` with `{"ip": self.ip_address}`. If TTL, keep existing `broadcast_unblock_ip` behavior.

4. **`mojo/apps/incident/cronjobs.py`** — Two changes:
   - Change `sweep_expired_blocks` from `@schedule(minutes="*")` to `@schedule(minutes="*/5")` (every 5 minutes).
   - Add `sync_firewall` cron: `@schedule(minutes="0")` (every hour at minute 0). Publishes `mojo.apps.incident.asyncjobs.sync_firewall` job.

### Design Decisions

- **`hash:net` for everything**: Handles both CIDRs and individual IPs (as /32). No need for `hash:ip`. Consistent with existing `ipset_load()` which already creates `hash:net` sets.
- **No job engine changes**: No `STARTUP_JOBS`, no `initialize()` hooks. The hourly cron is the startup recovery — first run after restart rebuilds all ipsets. Up to 1 hour of exposure is acceptable for suspected-IP blocks.
- **Incremental add/del for realtime, full rebuild for reconciliation**: `block()`/`unblock()` broadcast single-IP `ipset_add`/`ipset_del` for immediate effect. The hourly `sync_firewall` does a full flush+reload from DB truth to catch anything missed (failed broadcasts, new servers, drift).
- **Sweep at 5 minutes, not 1**: Expired blocks sitting an extra 4 minutes is irrelevant. 12x fewer queries per hour on an API node.
- **TTL blocks stay as individual iptables rules**: They expire soon and aren't worth ipset complexity. Lost on restart — acceptable since they'd expire anyway.
- **`ipset_add` creates the set if missing**: Handles the edge case where a block broadcast arrives before the first `sync_firewall` run. The set is created lazily with `-exist` flag.

### Edge Cases

- **First block before first sync**: `ipset_add` creates `mojo_blocked` set if it doesn't exist, so the broadcast works even on a fresh server before `sync_firewall` has run.
- **Unblock race with sync**: If `unblock()` removes an IP via `ipset_del`, then `sync_firewall` runs and the IP is already removed from DB, the full rebuild won't re-add it. Clean.
- **Concurrent block/unblock**: `ipset add -exist` and `ipset del -exist` are idempotent. No race conditions.
- **`ipset_load` flush gap**: During `sync_firewall`, the flush+reload creates a brief window where the ipset is empty. For thousands of IPs this is milliseconds. Acceptable for this threat level.
- **Capture permanent status before unblock save**: `unblock()` must check `self.blocked_until is None` BEFORE saving (which clears `blocked_until`), to know whether to broadcast ipset_del or iptables unblock.

### Testing

- `firewall.ipset_add` and `ipset_del` validate inputs and return correctly → `tests/test_incident/test_ipset_blocks.py`
- `block(ttl=None)` broadcasts `ipset_add` instead of `iptables` → same file (mock `jobs.broadcast_execute`, check function path and payload)
- `block(ttl=600)` still broadcasts `iptables` → same file
- `unblock()` of permanent block broadcasts `ipset_del` → same file
- `unblock()` of TTL block broadcasts `iptables unblock` → same file
- `sync_firewall` queries correct IPs and IPSets, calls `ipset_load` → `tests/test_incident/test_sync_firewall.py`
- Cron schedule changes (sweep at */5, sync_firewall at 0) → verify in cronjobs.py

### Docs

- `docs/django_developer/logging/incidents.md` — Add section on ipset-based permanent blocking, `sync_firewall` reconciliation, `FIREWALL_BLOCKED_IPSET_NAME` setting
- `docs/django_developer/logging/incidents.md` — Update settings table with new setting and sweep frequency change
- `CHANGELOG.md` — Document the change

## Resolution

**Status**: resolved
**Date**: 2026-04-03

### What Was Built
Permanent IP blocks routed through `mojo_blocked` ipset for O(1) kernel lookup. Hourly `sync_firewall` cron rebuilds all ipsets from DB truth (startup recovery + drift reconciliation). Sweep frequency reduced to every 5 minutes.

### Files Changed
- `mojo/apps/incident/firewall.py` — Added `ipset_add()` and `ipset_del()` for single-IP operations
- `mojo/apps/incident/asyncjobs.py` — Added `broadcast_ipset_add_blocked`, `broadcast_ipset_del_blocked`, `sync_firewall` job, `FIREWALL_BLOCKED_IPSET_NAME` setting
- `mojo/apps/account/models/geolocated_ip.py` — `block()` routes permanent blocks through ipset; `unblock()` routes based on DB-read `was_permanent`
- `mojo/apps/incident/cronjobs.py` — Sweep at `*/5`, added `sync_firewall` hourly cron
- `tests/test_helpers/cron.py` — Updated existing test to expect `*/5` sweep schedule

### Tests
- `tests/test_incident/test_ipset_blocks.py` — 5 tests: block/unblock routing for permanent vs TTL
- `tests/test_incident/test_sync_firewall.py` — 4 tests: sync queries, IPSet loading, cron registration
- Run: `bin/run_tests -t test_incident.test_ipset_blocks -t test_incident.test_sync_firewall`

### Docs Updated
- `docs/django_developer/logging/incidents.md` — ipset block flows, new functions, settings, cron changes
- `CHANGELOG.md` — v1.1.10 entry

### Security Review
- Command injection mitigations solid (validated inputs, no shell=True)
- Fixed `was_permanent` stale-read race (now reads from DB)
- Flush+reload window acceptable for this threat level
- Redis trust boundary is pre-existing architectural concern

### Follow-up
- Consider `ipset swap` for atomic replacement in `sync_firewall` (large deployments)
- Consider startup validation of `FIREWALL_BLOCKED_IPSET_NAME` setting
- Register `sync_firewall` cron in deployment config
