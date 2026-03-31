# Use ipset for permanent IP blocks

**Type**: request
**Status**: open
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
