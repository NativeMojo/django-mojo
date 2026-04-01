# Incident System — Django Developer Reference

## Purpose and Intent

The incident system is the framework's central intelligence layer for security and system health. Its job is not simply to log things — it is to **aggregate raw signals, suppress noise, identify patterns, and surface only what matters**.

Every part of the framework should treat the incident system as its primary channel for reporting anything security-relevant or operationally significant. This includes authentication failures, permission denials, suspicious IPs, payment errors, rate limit hits, data integrity anomalies — anything that, at sufficient volume or severity, indicates a real problem.

The core insight is that **individual events are rarely meaningful on their own**. A single failed login is noise. Fifty failed logins from the same IP in five minutes is an attack. The incident system exists to bridge that gap automatically, without requiring developers to think about thresholds, deduplication, or alerting in their application code.

---

## Architecture Overview

```
Event (raw signal)
  → RuleSet.check_by_category()   (rule matching by scope, then category)
    → Rule.check_rule()            (field-level conditions on event.metadata)
  → threshold/bundling logic       (pending → new transition)
  → Incident (correlated group)
    → handler chain               (job, email, notify, ticket, block)
    → Ticket (actionable work)
```

Events are the input. Incidents are the output. Rules, RuleSets, and handlers are the processing pipeline in between.

---

## Fleet-Wide IP Blocking

The incident system is the **sole authority** for IP blocking decisions. OSSEC and other detection tools report events — they never block directly.

### Design principles

1. **OSSEC detects, the incident engine decides.** OSSEC active response (local blocking) is disabled. OSSEC only reports events via webhook.
2. **GeoLocatedIP is the source of truth.** All block state lives in the `GeoLocatedIP` model — `is_blocked`, `blocked_until`, `blocked_reason`, `is_whitelisted`.
3. **Broadcast, not polling.** When a block decision is made, it broadcasts instantly to all instances via `jobs.broadcast_execute()`. No 60-second polling window.
4. **Whitelist overrides everything.** A whitelisted IP is never blocked, even by auto-escalation rules.
5. **Admin controls via CRUD + POST_SAVE_ACTIONS.** No dedicated REST endpoints for blocking — use the standard `GeoLocatedIP` REST interface with actions (`block`, `unblock`, `whitelist`, `unwhitelist`).

### How a block flows

```
OSSEC detects suspicious activity
  → reports via POST /api/incident/ossec/alert (event only, no blocking)
  → Event created → RuleSet evaluates
  → RuleSet with block:// handler fires
  → GeoLocatedIP.block(reason, ttl) called
    → DB updated (is_blocked=True, blocked_until, blocked_reason)
    → jobs.broadcast_execute("mojo.apps.incident.asyncjobs.broadcast_block_ip", {ips, ttl})
    → Every instance's job runner picks up the broadcast
    → firewall.block(ip) applies iptables DROP rule via sudo
```

### How an unblock flows

```
Cron (every minute): sweep_expired_blocks
  → Finds GeoLocatedIP where is_blocked=True AND blocked_until <= now
  → Bulk DB update: is_blocked=False
  → jobs.broadcast_execute("mojo.apps.incident.asyncjobs.broadcast_unblock_ip", {ips})
  → Every instance removes the iptables rule
```

Admins can also unblock immediately via the `unblock` action on `GeoLocatedIP`.

### firewall.py — iptables enforcement

`mojo.apps.incident.firewall` is the low-level iptables interface. It is only ever called by the job agent (running as `ec2-user` with passwordless sudo). It refuses to run as any other user.

| Function | Description |
|---|---|
| `block(ip)` | Idempotent — adds iptables DROP rule for INPUT (and FORWARD if forwarding is enabled) |
| `unblock(ip)` | Idempotent — removes DROP rules |
| `is_blocked(ip)` | Checks `iptables-save` output |
| `ipset_load(name, cidrs)` | Creates/replaces a kernel ipset with the given CIDRs and adds an iptables DROP rule for it |
| `ipset_remove(name)` | Removes a kernel ipset and its associated iptables rule |

All IPs are validated against a strict regex before touching iptables. Commands run via `sudo /sbin/iptables`.

### Async jobs

| Job | Type | Description |
|---|---|---|
| `broadcast_block_ip` | Broadcast | Applies iptables blocks on the local instance. Receives plain dict: `{"ips": [...], "ttl": 600}` |
| `broadcast_unblock_ip` | Broadcast | Removes iptables blocks on the local instance. Receives plain dict: `{"ips": [...]}` |
| `sweep_expired_blocks` | Cron (every minute) | Finds expired blocks in DB, updates DB, broadcasts fleet-wide unblock |
| `prune_events` | Cron (daily 9:45) | Deletes events older than `INCIDENT_EVENT_PRUNE_DAYS` with level < 6 |

### Why no public blocking endpoints?

Previously there were `ossec/firewall` and `ossec/firewall/block` endpoints. These were removed because:

- **Security risk**: Public endpoints that can block arbitrary IPs are an attack surface. Anyone who discovers them could denial-of-service legitimate users.
- **Single authority**: Block decisions must flow through the incident engine's rule evaluation, not bypass it via direct API calls.
- **Admin actions use CRUD**: Admins block/unblock via `GeoLocatedIP` POST_SAVE_ACTIONS, which are permission-gated (`manage_users`).

---

## Bulk Blocking via IPSet

The `IPSet` model manages ipset-based bulk IP blocking for entire countries, datacenter ranges, and abuse lists. Unlike per-IP blocking via `GeoLocatedIP`, IPSet operates on large CIDR sets (thousands to hundreds of thousands of entries) using the Linux `ipset` kernel module for O(1) lookups.

### Model Fields

| Field | Description |
|---|---|
| `name` | Unique ipset name (e.g., `country_cn`, `abuse_abuseipdb`) |
| `kind` | Type of set: `country`, `datacenter`, `abuse`, `custom` |
| `source` | Data source: `ipdeny`, `abuseipdb`, `manual` |
| `source_url` | URL to fetch CIDR data from (auto-populated for known sources) |
| `source_key` | API key or identifier for the source (e.g., country code, API key) |
| `data` | TextField containing the CIDR list (one per line) |
| `is_enabled` | Whether this ipset is active in iptables |
| `cidr_count` | Number of CIDRs currently loaded |
| `last_synced` | Timestamp of last successful sync to fleet |
| `sync_error` | Last error message if sync failed |

### POST_SAVE_ACTIONS

| Action | Description |
|---|---|
| `sync` | Broadcast the ipset data to all instances (loads into ipset + iptables) |
| `enable` | Enable the ipset and sync fleet-wide |
| `disable` | Disable the ipset and remove from fleet-wide iptables |
| `refresh_source` | Re-fetch data from the source URL, update `data` field, and sync |

### How it works

1. CIDR data is stored directly in the database as a TextField (one CIDR per line).
2. When synced, the data is broadcast to all instances via `jobs.broadcast_execute()`.
3. Each instance creates a kernel ipset (`ipset create <name> hash:net`), loads the CIDRs, and adds an iptables DROP rule referencing the set.
4. Lookups are O(1) regardless of set size, making it practical to block entire countries or large abuse lists.

### REST Endpoint

| Endpoint | Auth | Description |
|---|---|---|
| `/api/incident/ipset` | `view_security` / `security` (read), `manage_security` / `security` (write) | Standard CRUD + POST_SAVE_ACTIONS for IPSet management |

### Setup Examples

Use the helper classmethods to create common configurations:

```python
from mojo.apps.incident.models import IPSet

# Block traffic from specific countries
IPSet.create_country("cn")
IPSet.create_country("ru")
IPSet.create_country("ir")

# Block known abusive IPs via AbuseIPDB
IPSet.create_abuse_list("your-api-key-here")
```

### Cron: Weekly Refresh

A weekly cron job (Sunday 3:00 AM) calls `refresh_ipsets`, which iterates all enabled IPSet records, re-fetches data from their configured sources, updates the `data` field, and syncs the updated sets fleet-wide.

### Async Jobs

| Job | Type | Description |
|---|---|---|
| `broadcast_sync_ipset` | Broadcast | Loads ipset data on the local instance (creates ipset, loads CIDRs, adds iptables rule). Receives plain dict: `{"name": ..., "cidrs": [...]}` |
| `broadcast_remove_ipset` | Broadcast | Removes an ipset and its iptables rule from the local instance. Receives plain dict: `{"name": ...}` |
| `refresh_ipsets` | Cron (weekly, Sunday 3:00 AM) | Re-fetches source data for all enabled IPSets and syncs fleet-wide |

---

## Core Models

| Model | Purpose |
|---|---|
| `Event` | A single raw signal — one occurrence of something noteworthy |
| `RuleSet` | A named policy: which events match, how to bundle them, what to do |
| `Rule` | A single field-level condition within a RuleSet |
| `Incident` | A correlated group of related events requiring attention |
| `IncidentHistory` | Audit trail of incident state changes |
| `Ticket` | An actionable work item linked to an incident |
| `TicketNote` | A note or status change attached to a ticket |
| `IPSet` | A bulk IP blocking set (country, datacenter, abuse list) managed via ipset |

---

## Reporting Events

### From any Python code

```python
from mojo.apps import incident

incident.report_event(
    "User exceeded rate limit",
    title="Rate Limit Hit",
    category="rate_limit",
    scope="api",
    level=4,
    request=request,
    uid=request.user.id,
)
```

`report_event` is the primary API. It:
1. Creates an `Event` record with all fields and metadata populated
2. Calls `event.sync_metadata()` to enrich with geo-IP data
3. Calls `event.publish()` to run rule matching and incident creation

### From a MojoModel instance

```python
# Report an event tied to a specific model instance (auto-fills model_name, model_id)
self.report_incident(
    "Suspicious edit attempt",
    event_type="security_alert",
    level=5
)

# Report at the class level with request context
MyModel.class_report_incident(
    "Unauthorized list attempt",
    event_type="permission_denied",
    request=request
)
```

### Automatically — permission denials

Every permission denial issued by `MojoModel`'s permission system is automatically reported as an event. Categories include:

- `unauthenticated`
- `view_permission_denied`
- `edit_permission_denied`
- `group_member_permission_denied`
- `user_permission_denied`

No extra code required.

---

## Event Fields

| Field | Description |
|---|---|
| `level` | Severity 0–15. 0–3 informational, 4–7 warning, 8–15 critical |
| `scope` | Logical domain: `"global"`, `"account"`, `"payment"`, etc. |
| `category` | Dot-namespaced event type: `"auth:failed"`, `"permission_denied"` |
| `source_ip` | IP address of the originating request |
| `hostname` | Server hostname (auto-populated from `socket.gethostname()`) |
| `uid` | User ID associated with this event |
| `country_code` | ISO 3166-1 alpha-2 country code (auto-populated via GeoIP) |
| `title` | Short human-readable summary |
| `details` | Full description, stack trace, or structured message |
| `model_name` | Related model (e.g., `"account.User"`) |
| `model_id` | Related model instance PK |
| `metadata` | JSON bag of arbitrary context — also used by Rules for field matching |

### Custom metadata — pass anything as keyword arguments

Any extra keyword argument passed to `report_event` is automatically stored in `metadata` and becomes available for rule matching. There is no need to build a dict manually:

```python
incident.report_event(
    "Failed login attempt",
    category="auth:failed",
    level=5,
    request=request,
    username=attempted_username,   # custom
    attempt_count=7,               # custom
    auth_method="password",        # custom
)
```

All three custom fields end up in `event.metadata` alongside the standard ones. A Rule with `field_name="attempt_count"` and `comparator=">="` and `value="5"` would match this event.

When a `request` is passed, metadata is also automatically enriched with:

```
request_ip, http_path, http_protocol, http_method,
http_query_string, http_user_agent, http_host,
user_name, user_email (if authenticated)
```

---

## Level Guide

| Range | Meaning | Typical Use |
|---|---|---|
| 0–3 | Informational | Routine activity worth recording |
| 4–6 | Warning | Anomaly, policy violation, soft error |
| 7–9 | Elevated | Attack pattern, repeated failures, suspicious behavior |
| 10–14 | High severity | Confirmed attack, data integrity issue |
| 15 | Critical | System compromise, emergency |

Events with `level >= INCIDENT_LEVEL_THRESHOLD` (default: `7`) automatically create or escalate an incident even without a matching RuleSet.

---

## Rule Engine

The rule engine evaluates each event against configured RuleSets. It is the mechanism that separates signal from noise.

### RuleSet

A RuleSet defines:

- **`category`** — Which event category it applies to (matched by `scope` first, then `category`)
- **`priority`** — Evaluation order (lower = higher priority). First matching RuleSet wins.
- **`match_by`** — `ALL` (all rules must match) or `ANY` (any rule can match)
- **`bundle_by`** — How to group events into one incident (see bundling below)
- **`bundle_minutes`** — Time window for bundling. `0` = disabled, `None` = unlimited, `>0` = window in minutes
- **`handler`** — What to do when a RuleSet triggers (see handlers below)
- **`metadata`** — Optional threshold configuration (`min_count`, `window_minutes`, `pending_status`)

### Rule

Each Rule checks one field in `event.metadata` against a target value:

| Field | Description |
|---|---|
| `field_name` | The metadata key to inspect |
| `comparator` | `==`, `>`, `>=`, `<`, `<=`, `contains`, `regex` |
| `value` | The target value |
| `value_type` | `str`, `int`, `float`, `bool` |
| `index` | Evaluation order within the RuleSet |

Rules operate on `event.metadata`. Since `report_event` syncs all standard fields (level, category, source_ip, etc.) into metadata automatically, they are all available for rule matching alongside any custom fields you pass in.

### Bundling

Bundling controls how related events are collapsed into a single incident rather than creating a new one for each event.

| `bundle_by` value | Groups events by |
|---|---|
| `NONE` | Each event creates its own incident |
| `HOSTNAME` | Same server |
| `MODEL_NAME` | Same model type |
| `MODEL_NAME_AND_ID` | Same model instance |
| `SOURCE_IP` | Same source IP |
| `SOURCE_IP_AND_HOSTNAME` | Same IP + server |
| `SOURCE_IP_AND_MODEL_NAME` | Same IP + model type |
| `SOURCE_IP_AND_MODEL_NAME_AND_ID` | Same IP + model instance |
| `HOSTNAME_AND_MODEL_NAME` | Same server + model type |
| `HOSTNAME_AND_MODEL_NAME_AND_ID` | Same server + model instance |

### Thresholds (pending → new)

A RuleSet can hold incidents in `pending` status until a minimum event count is reached within a time window. This eliminates alerting on isolated events:

```python
RuleSet.objects.create(
    category="auth:failed",
    name="Brute Force Detection",
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=10,
    handler="block://?ttl=3600,ticket://?status=new&priority=8&category=security",
    metadata={
        "min_count": 10,           # Wait for 10 matching events
        "window_minutes": 10,      # Within 10 minutes
        "pending_status": "pending"
    }
)
```

Until 10 events accumulate, the incident sits at `pending`. Once the threshold is crossed, it transitions to `new` and the handler fires — blocking the IP fleet-wide and creating a ticket.

---

## Handlers

Handlers execute when a RuleSet triggers. Multiple handlers can be chained with commas.

### Syntax

```
job://app.module.function
email://admin@example.com
notify://user_id_or_channel
ticket://?status=open&priority=8&category=security&title=Investigate
block://?ttl=3600
```

Chained example:

```
block://?ttl=3600,ticket://?status=new&priority=9&category=security,email://security@example.com
```

### Handler Types

| Handler | Action |
|---|---|
| `job://` | Queues an async job (function path in netloc) |
| `email://` | Sends a notification email to the recipient |
| `notify://` | Sends a push/in-app notification to a user or channel |
| `ticket://` | Creates a Ticket linked to the incident |
| `block://` | Blocks the event's `source_ip` fleet-wide via `GeoLocatedIP.block()` |

### Block Handler Parameters

| Param | Default | Description |
|---|---|---|
| `ttl` | `600` | Seconds until auto-unblock (0 or omit = permanent) |
| `reason` | `auto:ruleset` | Base reason string recorded in `GeoLocatedIP.blocked_reason` |

The block handler extracts `source_ip` from the event, calls `GeoLocatedIP.geolocate()` to get or create the record, then calls `geo.block()` which handles both the DB update and the fleet-wide broadcast.

The final `blocked_reason` value is constructed by appending the incident and event IDs to the base reason for traceability:

```
auto:ruleset:incident:42:event:87
```

After a successful block, the handler also:
- Records a `handler:block` entry in `IncidentHistory` noting the IP and TTL
- Automatically resolves the incident (sets `status = "resolved"`) unless it is already `resolved` or `ignored`

### Ticket Handler Parameters

| Param | Description |
|---|---|
| `title` | Ticket title (defaults to event title) |
| `description` | Ticket body (defaults to event details) |
| `status` | Initial status (`open`, `new`, etc.) |
| `priority` | Integer priority (defaults to `event.level`) |
| `category` | Ticket category (default: `"incident"`) |
| `assignee` | User ID to assign the ticket to |

---

## Incident Lifecycle

```
pending  →  new  →  open  →  investigating  →  resolved  →  closed
```

| Status | Meaning |
|---|---|
| `pending` | Below threshold — waiting for more events |
| `new` | Threshold met or level-based trigger — needs triage |
| `open` | Acknowledged, active |
| `investigating` | Actively being worked |
| `resolved` | Root cause addressed |
| `closed` | No further action needed |

### Incident Actions

| Action | Description |
|---|---|
| `merge` | Merge other incidents into this one. Moves all events from listed incidents into the primary and deletes the originals. |

```python
primary_incident.on_action_merge([incident_id_1, incident_id_2])
```

---

## OSSEC Integration

OSSEC runs on every EC2 instance as a detection-only agent. Local active response (blocking) is disabled in `ossec.conf`. OSSEC detects and reports — the incident engine decides and enforces.

### Event flow

```
OSSEC agent (on EC2 instance)
  → detects log pattern (SSH brute force, web attack, etc.)
  → ossec-webhook.sh batches alerts
  → POST /api/incident/ossec/alert/batch
  → ossec parser normalizes the alert
  → reporter.report_event() creates Event
  → Event.publish() triggers rule evaluation
  → RuleSet handler fires (block, ticket, email, etc.)
```

### REST Endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `POST /api/incident/ossec/alert` | Public | Receive a single OSSEC alert |
| `POST /api/incident/ossec/alert/batch` | Public | Receive a batch of OSSEC alerts |

These endpoints only create events — they have no blocking authority.

### OSSEC alert fields (after parsing)

| Field | Maps to |
|---|---|
| `rule_id` | `model_id` (bundled as `ossec_rule`) |
| `level` | `level` |
| `description` | `title` |
| `full_log` | `details` |
| `source_ip` | `source_ip` |
| `hostname` | `hostname` |

---

## Integration with GeoLocatedIP

The incident system and `GeoLocatedIP` form a feedback loop:

1. **Events enrich GeoLocatedIP**: When events arrive with a `source_ip`, `sync_metadata()` calls `GeoLocatedIP.geolocate()` to attach geo and threat data.
2. **Incidents escalate threat levels**: `GeoLocatedIP.update_threat_from_incident(priority)` is called when incidents are created. This escalates `threat_level` (never downgrades) and auto-blocks IPs that reach `high` or `critical`.
3. **GeoLocatedIP data feeds rules**: Rules can match on `risk_score`, `is_tor`, `is_vpn`, `threat_level`, `country_code` — any GeoIP field that ends up in event metadata.
4. **Block/unblock flows through GeoLocatedIP**: The `block://` handler and admin actions both go through `GeoLocatedIP.block()`, ensuring a single code path for DB updates and fleet broadcasts.

See [GeoIP](../account/geoip.md) for the full model reference.

---

## Integration Patterns

### Every component should report events

The incident system only works if data flows into it. When writing a new service, model, or REST handler, ask:

- Could this action fail in a way that indicates abuse or misconfiguration?
- Could repeated failures from one source indicate an attack?
- Is this an action with security or compliance significance?

If yes, report an event.

### Pattern: rate limiting

```python
incident.report_event(
    f"User {user.id} exceeded API rate limit",
    category="rate_limit:api",
    scope="api",
    level=4,
    request=request,
    uid=user.id,
    endpoint=request.path,
)
```

A RuleSet bundling by `SOURCE_IP` with `min_count=20` and `window_minutes=1` fires only when a real abuse pattern emerges — not on a single slow request.

### Pattern: authentication failures

```python
incident.report_event(
    f"Failed login for {username}",
    category="auth:failed",
    scope="account",
    level=5,
    request=request,
    username=username,
)
```

### Pattern: known threat IP

```python
from mojo.apps.account.models import GeoLocatedIP
from mojo.apps import incident

geo = GeoLocatedIP.geolocate(request.ip)

if geo.is_threat:
    incident.report_event(
        f"Request from known threat IP {request.ip}",
        category="ip:known_threat",
        scope="account",
        level=10,
        request=request,
        source_ip=request.ip,
        is_tor=geo.is_tor,
        is_vpn=geo.is_vpn,
        threat_level=geo.threat_level,
        risk_score=geo.risk_score,
    )
```

### Pattern: payment anomalies

```python
incident.report_event(
    "Multiple card declines for user",
    category="payment:declined",
    scope="billing",
    level=5,
    uid=user.id,
    model_name="billing.Order",
    model_id=order.id,
)
```

### Pattern: auto-block via RuleSet

```python
# This RuleSet blocks the IP after 10 failed SSH logins in 5 minutes,
# creates a ticket, and emails the security team — all from one config.
RuleSet.objects.create(
    category="ossec",
    name="SSH Brute Force",
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=5,
    handler="block://?ttl=3600&reason=ssh_brute_force,ticket://?status=new&priority=9&category=security,email://security@example.com",
    metadata={
        "min_count": 10,
        "window_minutes": 5,
        "pending_status": "pending"
    }
)
```

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `INCIDENT_LEVEL_THRESHOLD` | `7` | Minimum level to auto-create an incident without a matching RuleSet |
| `INCIDENT_EVENT_PRUNE_DAYS` | `30` | Days to retain events with level < 6 |
| `INCIDENT_EVENT_METRICS` | — | Enable metrics recording for events and incidents |
| `INCIDENT_METRICS_MIN_GRANULARITY` | `"hours"` | Granularity for incident metrics |

---

## Why Consistency Matters

The incident system gets more valuable as more components use it. A RuleSet configured to detect brute force across `auth:failed` events only works if every authentication path reports `auth:failed` consistently.

Establish naming conventions per domain and stick to them:

```
auth:failed
auth:locked
auth:mfa_bypass_attempt
ip:suspicious
ip:known_threat
permission:denied
payment:declined
data:unexpected_delete
rate_limit:api
ossec
firewall:block
firewall:unblock
firewall:whitelist
firewall:unwhitelist
```

The rule engine matches on these strings — consistent category naming means rule coverage automatically extends to every code path that reports under the same category, without touching the RuleSet.
