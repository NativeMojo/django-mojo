# sync_firewall runs 30 minutes due to per-CIDR subprocess calls

**Type**: bug
**Status**: planned
**Date**: 2026-04-05
**Severity**: high

## Description

`sync_firewall` (hourly cron) takes ~30 minutes because `firewall.ipset_load()` spawns a separate `sudo ipset add` subprocess for every single CIDR. With country-level IPSets (2,000–3,000 CIDRs each) plus permanent blocks, this means thousands of fork+sudo+subprocess calls per run.

## Context

The firewall system should be lightweight. A 30-minute sync on an hourly schedule means the job is running half the time, consuming system resources and delaying other jobs. If multiple country IPSets are enabled, the job could exceed its 1-hour window.

Affected users: all production instances running the incident firewall cron.

## Acceptance Criteria

- `sync_firewall` completes in under 30 seconds for typical workloads (5,000+ total CIDRs across all IPSets)
- `ipset_load()` uses `ipset restore` (batch stdin) instead of per-CIDR subprocess calls
- Broadcast handlers (`broadcast_block_ip`, `broadcast_ipset_add_blocked`) remain single-IP for real-time use — only bulk operations need the batch path
- Existing validation (`_validate_ip`, `_validate_ipset_name`) must still apply to every entry
- No change to the iptables rule attachment logic

## Investigation

**Likely root cause**: `firewall.ipset_load()` adds CIDRs one at a time in a loop, each via `subprocess.run([SUDO, ipset, add, ...])`. Each call pays fork + sudo + kernel overhead (~50–100ms). For 4,500 CIDRs this is 7.5–30 minutes depending on system load.

**Confidence**: confirmed (code analysis — the loop-of-subprocess pattern is unambiguous)

**Code path**:
- `mojo/apps/incident/asyncjobs.py:130-158` — `sync_firewall` entry point
- `mojo/apps/incident/firewall.py:194-235` — `ipset_load()` with the per-CIDR loop at lines 221-227
- `mojo/apps/incident/firewall.py:62-77` — `_run()` spawns `sudo` subprocess per call

**Regression test**: not feasible — requires ipset/iptables kernel modules and sudo access

**Related files**:
- `mojo/apps/incident/firewall.py` — primary fix target: replace loop with `ipset restore` stdin batch
- `mojo/apps/incident/asyncjobs.py` — no changes needed, calls `ipset_load` correctly
- `mojo/apps/incident/models/ipset.py` — `cidrs` property and `sync()` method (also calls `ipset_load` via broadcast)

## Fix Direction

Replace the per-CIDR loop in `ipset_load()` with a single `ipset restore` call that pipes all entries via stdin:

```
create <name> hash:net -exist
flush <name>
add <name> 10.0.0.0/8
add <name> 192.168.0.0/16
...
```

This is the standard Linux approach for bulk ipset operations — one subprocess call regardless of CIDR count.

## Plan

**Status**: planned
**Planned**: 2026-04-05

### Objective
Make firewall sync lightweight by using `ipset restore` for bulk loads and eliminating unnecessary hourly rebuilds of unchanged ipsets.

### Steps
1. `mojo/apps/incident/firewall.py` — Replace per-CIDR subprocess loop in `ipset_load()` with single `ipset restore` via stdin
   - Build restore script in Python (create, flush, add lines), validate every CIDR with `_validate_ip()` first
   - Pipe to `sudo ipset restore` as one subprocess call
   - Use atomic swap: load into `<name>_tmp`, swap with live set, destroy tmp — zero-downtime
   - Add `_run_stdin()` helper alongside `_run()` for piping stdin to subprocess
   - Keep `ipset_add()` / `ipset_del()` as-is for single-IP real-time operations

2. `mojo/apps/incident/asyncjobs.py` — Rework `sync_firewall` from "hourly full rebuild" to "startup restore + skip unchanged"
   - For each IPSet: compare `ipset.modified` vs `ipset.last_synced` — skip if unchanged
   - For `mojo_blocked` permanent IPs: track last sync time in a Redis key, query `GeoLocatedIP.objects.filter(modified__gt=last_sync)` to detect changes
   - Log what was skipped vs synced for observability
   - First run (nothing synced yet) loads everything — same as current behavior but fast

3. No other files change — broadcast handlers, `IPSet.sync()`, `sweep_expired_blocks`, real-time path all stay as-is

### Design Decisions
- **Atomic swap over flush+reload**: Prevents a window where the ipset is empty during sync. Minimal complexity, meaningful for a security tool.
- **Keep `sync_firewall` as a cron, not startup-only**: Periodic safety net is worth having, but it should be fast and skip unchanged sets. Operators can reduce frequency via settings.
- **No diffing of individual CIDRs**: When an ipset needs reload, full replace via restore is simpler and faster than computing a diff. Restore takes <1s regardless.
- **Redis key for permanent block tracking**: Simpler than adding a `last_synced` field to GeoLocatedIP. Store a timestamp after syncing the mojo_blocked set.

### Edge Cases
- **Empty CIDR list**: `ipset restore` with just create+flush is valid — produces an empty set
- **Invalid CIDRs in batch**: Validate before building script. One bad entry in `ipset restore` aborts the whole batch, so filter first.
- **Atomic swap with iptables rule**: The iptables rule references the set name, not the tmp name. Swap is transparent to iptables.
- **First run after deploy**: `last_synced` is null → everything loads

### Testing
- Restore script generation logic (valid, invalid, empty CIDR lists) → `tests/test_incident/test_firewall_restore.py`
- Integration test not feasible — requires ipset kernel module and sudo

### Docs
- `docs/django_developer/incident/firewall.md` — update to reflect `ipset restore` and sync behavior change
- `CHANGELOG.md` — note performance fix
