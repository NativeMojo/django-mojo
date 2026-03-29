# Structured Firewall Event Tracking

**Status**: Done
**Priority**: Medium
**Created**: 2026-03-27

## Problem

Firewall actions (block, unblock, whitelist, unwhitelist) on `GeoLocatedIP` are logged via `self.log()` with free-text strings. An admin trying to answer basic questions has no clean path:

- "Show me all IPs blocked in the last 24 hours"
- "How many times has 1.2.3.4 been blocked and by whom?"
- "List all active blocks that expire in the next hour"
- "Which blocks were auto-escalated vs manual?"

The `GeoLocatedIP` model tracks current state (`is_blocked`, `blocked_at`, `blocked_reason`, `block_count`) but not history. Each block overwrites the previous one. And the logit entries are unstructured text — you can't filter or aggregate them without string parsing.

## Recommendation: Logit with Structured Payloads

Logit is the right home. It already has:
- `kind` field (indexed) — filter by `firewall:block`, `firewall:unblock`, etc.
- `payload` field — can hold JSON with structured details
- `ip` field — the source IP from the request context
- `model_name` + `model_id` — links back to the GeoLocatedIP record
- `uid` + `username` — who did it
- `created` — when it happened
- Composite index on `(created, kind)` for fast time-range + kind queries

No new models needed. No incident events (those represent detections, not responses).

## What to Change

### 1. Standardize `kind` values

Use consistent, filterable kind strings:

| Action | `kind` value |
|--------|-------------|
| Block IP | `firewall:block` |
| Unblock IP | `firewall:unblock` |
| Whitelist IP | `firewall:whitelist` |
| Remove whitelist | `firewall:unwhitelist` |
| Auto-block from threat escalation | `firewall:auto_block` |
| Broadcast block to fleet | `firewall:broadcast_block` |
| Broadcast unblock to fleet | `firewall:broadcast_unblock` |
| ipset load (bulk) | `firewall:ipset_load` |
| ipset remove (bulk) | `firewall:ipset_remove` |

### 2. Pass structured payload on every log call

Currently in `on_action_block`:
```python
self.log(f"IP Blocked: {self.ip_address} - {reason}", "firewall:block")
```

Should become:
```python
self.log(
    f"IP Blocked: {self.ip_address} - {reason}",
    "firewall:block",
    payload=ujson.dumps({
        "ip": self.ip_address,
        "reason": reason,
        "ttl": ttl,
        "blocked_until": str(self.blocked_until) if self.blocked_until else None,
        "block_count": self.block_count,
        "trigger": "manual",  # or "auto:threat_escalation", "auto:incident_rule"
    }),
)
```

Apply the same pattern to `on_action_unblock`, `on_action_whitelist`, `on_action_unwhitelist`.

### 3. Add structured logging to model methods too

The `block()`, `unblock()`, `whitelist()` methods are called both from REST actions and from code (e.g. `update_threat_from_incident`). They should also log with structured payloads so every path is tracked.

`update_threat_from_incident` should log with `kind="firewall:auto_block"` and include the incident priority that triggered it.

### 4. Log broadcast results

When `block()` calls `jobs.broadcast_execute()`, log the result:

```python
self.log(
    f"Broadcast block: {self.ip_address}",
    "firewall:broadcast_block",
    payload=ujson.dumps({
        "ip": self.ip_address,
        "runners_reached": len(results),
    }),
)
```

### 5. Admin queries become trivial

```python
from mojo.apps.logit.models import Log

# All blocks in the last 24 hours
Log.objects.filter(kind="firewall:block", created__gte=since)

# All firewall activity for a specific IP
Log.objects.filter(kind__startswith="firewall:", model_id=geo_ip.id)

# Auto-blocks vs manual blocks
# Parse payload JSON for "trigger" field

# All firewall activity (any type)
Log.objects.filter(kind__startswith="firewall:")
```

Via REST API:
```
GET /api/logit/log?kind=firewall:block&dr_start=2026-03-26
GET /api/logit/log?kind__startswith=firewall:&model_id=42
```

### 6. Record metrics for firewall events

No firewall metrics exist today. Add `metrics.record()` calls for **blocking events only**. Unblocks and whitelist changes are low-volume administrative actions that don't need time-series tracking — logit entries are sufficient for those.

```python
from mojo.apps import metrics

# In block()
metrics.record("firewall:blocks", category="firewall")
metrics.record(f"firewall:blocks:country:{self.country_code}", category="firewall")

# In update_threat_from_incident() when auto-blocking
metrics.record("firewall:auto_blocks", category="firewall")

# In broadcast_execute success
metrics.record("firewall:broadcasts", category="firewall")
```

This gives the web developer time-series data via the metrics REST API:

```
GET /api/metrics/metric?slug=firewall:blocks&granularity=hours&dr_start=2026-03-26
GET /api/metrics/metric?category=firewall&granularity=days
```

Use category `"firewall"` so all firewall metrics can be fetched in one batch.

## What NOT to Do

- **Don't create a new FirewallEvent model** — logit already does this job with the right fields and indexes
- **Don't use Incident Events** — those represent security detections (OSSEC alerts, brute force, etc.), not administrative responses. Mixing them muddies the incident feed.
- **Don't add a JSONField to GeoLocatedIP for history** — that's what logit is for

## Acceptance Criteria

- [x] Every `block()`, `unblock()`, `whitelist()`, `unwhitelist()` call writes a logit entry with structured `payload`
- [x] `update_threat_from_incident()` auto-blocks log with `kind="firewall:auto_block"` and include incident context
- [x] `kind` values follow the `firewall:*` convention consistently
- [x] Every payload includes: `ip`, `reason`, `trigger` (manual/auto), and action-specific fields (ttl, blocked_until, etc.)
- [x] Broadcast results are logged
- [x] Existing `on_action_*` handlers updated to use structured payloads (duplicate log calls removed)
- [x] REST API can filter logit by `kind=firewall:*` to show firewall activity dashboard
- [x] `metrics.record()` called for block, auto_block, and broadcast events (not unblock/whitelist)
- [x] All firewall metrics use `category="firewall"` for batch fetching
- [x] Country-level block metrics recorded (`firewall:blocks:country:{CC}`)
- [x] Web developer docs: `docs/web_developer/account/firewall.md` — security dashboard guide (already existed, permissions updated)
- [x] GeoIP web dev docs updated with block/unblock/whitelist/unwhitelist actions (covered in firewall.md)
