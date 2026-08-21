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

## MojoSec cases

`MojoSecReceipt` remains the immutable forensic/idempotency ledger.
`MojoSecCase` summarizes web and FIM receipts into deterministic bounded
cases; `MojoSecCaseTransition` is an append-only snapshot for each receipt
contribution plus receipt-less system rows (`settled`, `reopened`,
`projection`, `receipt_id_snapshot=0`).

An installation's enrollment row carries a `mode`:

- **`shadow`** (default) — dual-write only. The receipt → `Event` →
  RuleSet/handler path stays authoritative and cases never notify, project,
  or own an acknowledgement.
- **`authoritative`** — digest-tier evidence (trusted expected-change FIM and
  policy-bound web observations, which normalize to info/warning) is
  **case-routed**: the receipt is created eventless with a sticky
  `case_routed` flag, the case contribution owns the ingestion ack (`retry`
  until it lands durably), and **no per-receipt Event is projected**.
  Unannotated protected changes, `fim.overflow` and
  `fim.expected_change_error` keep today's immediate, individual per-receipt
  Events at their existing severity. Routing is decided once, at first
  delivery: a later policy bump or un-enrollment cannot dead-letter or reject
  accepted evidence — an unresolvable binding terminal-converts the receipt
  into its ordinary per-receipt Event instead (visible noise, never silent
  loss), and a `*/5` sweep re-drives stranded contributions from replay
  evidence.

Cutting an installation to authoritative consciously silences exact-category
RuleSets on `mojosec.web.probe`, `mojosec.web.denied`, `mojosec.web.error`
and trusted `mojosec.fim.change` for that installation — the canary command's
`silenced_rule_sets` preflight lists exactly what stops firing. The only
deliberate case-level projection is promotion: when an authoritative-mode
case ratchets into high/critical, exactly one `mojosec.case.promoted` Event
per upward urgency step is projected (level 8/12, `source_ip=None`, bounded
metadata, no raw paths), published with exact-category lookup so deployments
attach notification RuleSets to it. Projection is crash-safe (the
`projected_urgency` ratchet is healed by the sweep) and handler dispatch uses
the strict idempotent form. `block://` on the promoted category is inert by
design — a case aggregates many sources and carries no `source_ip`;
enforcement wiring belongs to the recommendation lifecycle in a later slice.

Case counters deliberately mean different things:

| Counter | Meaning |
|---|---|
| `occurrence_count` | Sum of sensor-reported occurrences; preserves volume |
| `receipt_count` | Unique contributing receipt rows |
| `projected_event_count` | Event-shaped contributions; HTTP-scheme 301 contributions are discounted without trying to pair a redirect twin |
| `distinct_count` | Distinct normalized family/network/resource samples |
| `sample_count` | Stored bounded samples (maximum 8) |
| `overflow_count` | Distinct samples beyond the storage bound |

The receipt row lock plus its one case contribution timestamp makes replay and
concurrent duplicate delivery idempotent. The case window has a database unique
identity and is updated under a row lock. Web cases use one-hour windows and
normalize registered probe families plus an explicit `other_probe` bucket and
bounded IP networks. Untrusted FIM uses 15-minute windows, protected tiers and
a dedicated overflow family. **Trusted expected-change FIM coalesces into one
deployment case per sensor + deployment identity per UTC day** (evaluator v2):
`family="deployment"`, the `deployment_id` column carries the identity, and a
bounded `breakdown` records per-operation and per-tier change counts (≤16
operations / 8 tiers, spill in `"_other"`). A deployment case **settles** after
the quiet window (`MOJOSEC_DEPLOY_QUIET_SECONDS`, default 60, bounds 10–900)
with no last-seen movement — a system transition, never an Event or a
notification; a genuinely new late receipt reopens it once and it re-settles.
Real deploys therefore project nothing anywhere: the settled case row in the
case list is the one visible deployment summary. Raw paths, query strings,
bodies, cookies, authorization, arbitrary lines, and free-form sensor text do
not enter cases or case metrics.

Sensor time may lead receipt time by at most the file/static
`MOJOSEC_CASE_FUTURE_SKEW_SECONDS` setting (default 300 seconds, valid range
0–3600). A value outside that range fails closed to zero. Evidence beyond the
bound uses the immutable receipt creation time for case windows and ordering;
the raw receipt/replay evidence is not rewritten. Case metrics and the canary
command apply the matching upper bound of server time plus allowed skew.

### Read-only case API and metrics

The custom case endpoints require a human JWT with global `view_security` or
`security`. API keys and group/member grants are rejected. There are no create,
update, delete, approve, recommend, acknowledge, or execute endpoints.

| Method | Path | Input | Response |
|---|---|---|---|
| `GET` | `/api/incident/mojosec/case` | `page` (default 1, maximum 100), `page_size` (default 50, maximum 100), and exact `state` (`observing`/`elevated`/`settled`), `urgency`, `sensor_kind`, `resource_id`, `family`, or `deployment_id` filters | `{status, data, page, page_size, has_more}`; `data` is a bounded list of case summaries |
| `GET` | `/api/incident/mojosec/case/<id>` | Case id in the path | `{status, data}`; `data` is one case with at most 8 normalized samples and the latest 50 transition snapshots |
| `GET` | `/api/incident/mojosec/case-metrics` | `days` (default 1, range 1–90) and optional exact `resource_id` | `{status, data}` with bounded aggregate counters and no evidence arrays |

A list row contains `id`, `created`, `first_seen`, `last_seen`, `window_start`,
`window_end`, `sensor_kind`, `resource_id`, `family`, `deployment_id`, state
and urgency with their reasons, all six counters, and the policy/evaluator
versions; datetimes are ISO-8601 strings. Detail adds `sensor_id`, `network`,
`settled_at`, `projected_urgency`, `breakdown`, `samples`, and `transitions`.
Each transition exposes only its id/time, transition and reason, from/to state
and urgency, and the resulting occurrence/receipt counts; it never exposes
receipt replay JSON or integrity digests.

`page` and `page_size` must be positive integers. Values over 100 are rejected
with HTTP 400 rather than clamped, bounding the deepest possible offset to
9,900 rows. Clients use `has_more` and must not request an unbounded total.

The case-metrics `data` keys are `days`, `cases`, `occurrences`, `receipts`,
`projected_events`, `distinct`, `overflows`, `settled`, `suppressed_events`,
`compression_ratio`, and `by_urgency`. Compression is occurrences divided by
cases, not by receipts or projected Events; `suppressed_events` counts
case-routed receipts that would each have been one operator Event under the
legacy path. The contribution path also records operational metrics under
`mojosec:shadow:`: `receipts`, `occurrences`, `compressed_occurrences`,
`cases_opened`, `cases_updated`, `deploy_cases_opened`,
`deploy_cases_settled`, `events_suppressed`, `ack_retries`,
`route_conversions`, `case_events_projected`, `projection_failures`,
`urgency:<level>`, `promotions`, `overflow`, and `failures`. The `overflow`
metric counts updates to cases whose bounded sample overflow is nonzero; the
case's `overflow_count` is the durable distinct sample count.

### Rollout and one-VHost canary

Dual-write is off unless the file/static setting names an exact installation
and VHost. It is intentionally unavailable through the DB-backed settings
plane:

```python
MOJOSEC_CASE_SHADOW_TARGETS = [{
    "installation_key_id": 42,
    "vhost_ids": [17],
    "include_fim": False,
    "mode": "shadow",          # or "authoritative" after the canary is accepted
}]
```

`mode` is optional and defaults to `"shadow"`; any other value fails the whole
list closed, like every other malformed row.

`vhost_ids` is only the first web eligibility gate. Before contribution, the
server resolves an enabled VHost, requires `Vhost.domain.group_id` to equal the
installation API key's group, and revalidates its saved `mojosec_policy`.
Resource id and policy version must match exactly. The response class must be
the policy's registered class, or `impossible_path` for a normalized family
explicitly enabled by that policy. Nonexistent, cross-group, disabled, stale,
or mismatched evidence remains only in its raw receipt and contributes no
shadow case. `include_fim=True` enables all supported FIM evidence for that
installation key; FIM is installation-scoped and is not narrowed by
`vhost_ids`. A malformed target row fails closed by disabling the complete
target list. The target list and each row's `vhost_ids` list are capped at 32;
exceeding either bound also disables the complete list.

Start with one Mojoware VHost. Compare receipt fidelity, occurrence volume,
case cardinality, projected Events, overflow, urgency, and compression for at
least one normal traffic cycle. Use the case API for urgency and bounded case
inspection. The exact read-only bounds command is:

```bash
uv run python manage.py mojosec_shadow_compare --installation-key 42 --vhost 17 --hours 24 --max-cases 500 --min-compression 2
```

`--vhost` is optional; `--sensor <sensor_id>` narrows to one node, which is
the shape a FIM deployment canary wants. The command prints the redacted
`mojosec.shadow-comparison` v2 JSON contract — including `suppressed_events`
(per-receipt Events the cutover did not project), `deployment_cases`
(per-sensor/deployment cardinality, the "one quiet summary per deploy" proof)
and `silenced_rule_sets` (active exact-category RuleSets that stop firing for
an authoritative installation) — and exits non-zero when receipt fidelity,
cardinality, or compression bounds fail. It does not write cases or
production policy.

The cutover procedure is: enroll in `"shadow"`, run the comparison for at
least one normal traffic cycle **and one real deploy**, read
`silenced_rule_sets` to confirm nothing you rely on stops firing, then flip
that row's `mode` to `"authoritative"` and reconverge. Flip back (or set
`MOJOSEC_CASE_SHADOW_TARGETS = []`) to roll back new routing decisions;
already-routed pending receipts still resolve through their sticky flag —
contribution or terminal conversion — and are never lost. In shadow mode the
correlation write remains fail-open after receipt acknowledgement; in
authoritative mode the contribution owns the ack, and the `*/5`
`settle_mojosec_cases` sweep settles quiet deployment cases, heals crashed
projections, and re-drives stranded case-routed receipts.

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

Permanent blocks (`ttl=None`) and TTL blocks (`ttl > 0`) are enforced differently:

**Permanent block (ttl=None):**

```
GeoLocatedIP.block(reason, ttl=None)
  → DB updated (is_blocked=True, blocked_until=None, blocked_reason)
  → jobs.broadcast_execute("mojo.apps.incident.asyncjobs.broadcast_ipset_add_blocked", {ip})
  → Every instance adds the IP to the mojo_blocked ipset (O(1) kernel lookup)
  → firewall.ipset_add("mojo_blocked", ip) creates the set if absent and ensures iptables rule
```

**TTL block (ttl > 0):**

```
GeoLocatedIP.block(reason, ttl=600)
  → DB updated (is_blocked=True, blocked_until=<now+ttl>, blocked_reason)
  → jobs.broadcast_execute("mojo.apps.incident.asyncjobs.broadcast_block_ip", {ips, ttl})
  → Every instance adds an individual iptables DROP rule via firewall.block(ip)
```

### How an unblock flows

**Cron (every 5 minutes): sweep_expired_blocks**

```
sweep_expired_blocks
  → Finds GeoLocatedIP where is_blocked=True AND blocked_until <= now
  → Bulk DB update: is_blocked=False
  → jobs.broadcast_execute("mojo.apps.incident.asyncjobs.broadcast_unblock_ip", {ips})
  → Every instance removes the individual iptables rule
```

**Admin unblock (`unblock` action on GeoLocatedIP):**

- If the block was permanent (`blocked_until` was `None`): broadcasts `broadcast_ipset_del_blocked` to remove the IP from the `mojo_blocked` ipset.
- If the block had a TTL: broadcasts `broadcast_unblock_ip` to remove the individual iptables rule.

### Startup recovery and fleet reconciliation

A new hourly cron job, `sync_firewall`, rebuilds all ipsets from DB truth. This:

- Restores permanent blocks after a server restart (iptables rules are lost on reboot; ipset state is also non-persistent without this job).
- Catches any blocks missed by failed broadcasts.
- Catches drift between instances that joined after a block was issued.

```
sync_firewall (hourly, minute 0)
  → Query all GeoLocatedIP where is_blocked=True AND blocked_until IS NULL
  → firewall.ipset_load("mojo_blocked", permanent_ips)  — full flush+reload
  → Query all IPSet where is_enabled=True
  → For each: firewall.ipset_load(ipset.name, ipset.cidrs)
```

Up to one hour of exposure is acceptable for permanent blocks — they target sustained threats, not short-lived TTL blocks that expire on their own.

### firewall.py — iptables enforcement

`mojo.apps.incident.firewall` is the low-level iptables interface. It is only ever called by the job agent (running as `ec2-user` with passwordless sudo). It refuses to run as any other user.

| Function | Description |
|---|---|
| `block(ip)` | Idempotent — adds iptables DROP rule for INPUT (and FORWARD if forwarding is enabled) |
| `unblock(ip)` | Idempotent — removes DROP rules |
| `is_blocked(ip)` | Checks `iptables-save` output |
| `ipset_add(name, ip)` | Adds a single IP to a named ipset. Creates the set (`hash:net`) if absent. Ensures the iptables DROP rule for the set exists. Idempotent. Returns True/False. |
| `ipset_del(name, ip)` | Removes a single IP from a named ipset. Idempotent. Returns True/False. |
| `ipset_load(name, cidrs)` | Creates/replaces a kernel ipset with the given CIDRs and adds an iptables DROP rule for it |
| `ipset_remove(name)` | Removes a kernel ipset and its associated iptables rule |

All IPs are validated against a strict regex before touching iptables. Commands run via `sudo /sbin/iptables`.

### Async jobs

| Job | Type | Description |
|---|---|---|
| `broadcast_block_ip` | Broadcast | Applies individual iptables blocks on the local instance (TTL blocks). Receives plain dict: `{"ips": [...], "ttl": 600}` |
| `broadcast_unblock_ip` | Broadcast | Removes individual iptables blocks on the local instance. Receives plain dict: `{"ips": [...]}` |
| `broadcast_ipset_add_blocked` | Broadcast | Adds a single IP to the `mojo_blocked` ipset on the local instance (permanent blocks). Receives plain dict: `{"ip": "1.2.3.4"}` |
| `broadcast_ipset_del_blocked` | Broadcast | Removes a single IP from the `mojo_blocked` ipset on the local instance. Receives plain dict: `{"ip": "1.2.3.4"}` |
| `sweep_expired_blocks` | Cron (every 5 minutes) | Finds expired blocks in DB, updates DB, broadcasts fleet-wide unblock |
| `sync_firewall` | Cron (hourly, minute 0) | Rebuilds all ipsets from DB truth. Restores permanent blocks after restart and reconciles fleet drift. |
| `prune_events` | Cron (daily 9:45) | Deletes events older than `INCIDENT_EVENT_PRUNE_DAYS` with level < 6 |
| `prune_incidents` | Cron | Deletes resolved/closed/ignored incidents older than `INCIDENT_PRUNE_DAYS`. Skips incidents with `metadata.do_not_delete = True`. |
| `recheck_active_threats` | Cron (daily 4:20) | Re-scores up to `GEOLOCATION_RECHECK_THREATS_MAX` recently-active `GeoLocatedIP` rows so `threat_level` can decay. Skips `provider='mojo'` records and external blocklist lookups. |

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
| `source` | Data source: `ipdeny`, `abuseipdb`, `tor`, `blocklist_de`, `manual` |
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

### Cron: Threat List Cache Refresh (`tor_exits`, `blocklist_de`)

A separate 6-hourly cron, `refresh_threat_lists`, keeps two **cache-only**
IPSet rows warm — `tor_exits` (source `tor`) and `blocklist_de` (source
`blocklist_de`). They are created with `is_enabled=False` and refreshed via
`refresh_from_source()` only — never `sync()` — so they are excluded from
`refresh_ipsets`/`sync_firewall` and never reach the kernel firewall. They
exist purely so `mojo.helpers.geoip.detection.detect_tor()` and
`check_blocklist_de()` can read from the DB cache instead of downloading the
full list on every lookup. See
[account/geoip.md](../account/geoip.md#threat-list-caches-tor-exit-list-blocklistde)
for the reader side. **These rows cannot be enabled** — the REST `enable`
action rejects them and `sync()` no-ops for them even if the flag is
force-set, so the entire Tor exit list / blocklist.de list can never be
pushed into the fleet-wide firewall.

### Async Jobs

| Job | Type | Description |
|---|---|---|
| `broadcast_sync_ipset` | Broadcast | Loads ipset data on the local instance (creates ipset, loads CIDRs, adds iptables rule). Receives plain dict: `{"name": ..., "cidrs": [...]}` |
| `broadcast_remove_ipset` | Broadcast | Removes an ipset and its iptables rule from the local instance. Receives plain dict: `{"name": ...}` |
| `refresh_ipsets` | Cron (weekly, Sunday 3:00 AM) | Re-fetches source data for all enabled IPSets and syncs fleet-wide |
| `refresh_threat_lists` | Cron (every 6 hours) | `refresh_from_source()` only, for the cache-only `tor_exits`/`blocklist_de` IPSet rows — never syncs to the firewall |

---

## Group Context

Every `Event` and `Incident` carries an optional reference to the `account.Group` that originated the signal.

### Fields

Both `Event` and `Incident` have:

```
group  — nullable FK to account.Group (on_delete=SET_NULL, db_index=True)
```

The FK is `SET_NULL`, so deleting a group does not lose the event. Group identity is also snapshotted into `event.metadata` at creation time (see below), so audit records survive group renames and deletions.

### Auto-derivation in `report_event()`

`incident.report_event(...)` resolves the group via this precedence:

1. Caller-supplied `group=` kwarg — including explicit `group=None` which suppresses derivation
2. `request.group` — the group resolved from the current request context

`isinstance(Group, ...)` is enforced so plain IDs or strings are never set as the FK value.

Both `group_id` and `group_name` are mirrored into `event.metadata` at creation time. This snapshot survives group deletion (`SET_NULL` drops the FK, but metadata persists) and group renames (name is copied at event-creation time).

### Auto-stamping from `MojoModel`

`MojoModel.report_incident` and the class-level methods also auto-stamp group context:

| Method | Group source |
|---|---|
| `instance.report_incident(...)` | `self.group` when the instance has a `.group` attr that is a `Group` instance |
| `MyModel.class_report_incident(...)` | `request.group` |
| `MyModel.class_report_incident_for_user(...)` | `request.group` |

All three use `setdefault("group", ...)` so a caller-supplied `group=None` is never overwritten.

### Incident inheritance

When events are bundled into an incident via `Event.get_or_create_incident` and `Event.link_to_incident`:

- **New incident** — `Incident.group` is seeded from the first (seed) event's group.
- **Existing incident** — group is reconciled as events link in:
  - If the incident has no group yet and has not previously had a mismatch, it inherits the event's group.
  - If the incident's group differs from the event's group, the incident's group is set to `None` and `metadata.group_mismatch=True` is stamped on the incident.

`group_mismatch` is audit-stable — it is set once and never cleared, even if later events share the same group. It indicates that the incident aggregated activity from more than one group.

### Metadata snapshot

At event-creation time, `event.metadata` is populated with:

| Key | Value |
|---|---|
| `group_id` | `group.pk` |
| `group_name` | `group.name` (at time of event) |

These are written unconditionally when a group is resolved. Audit records remain readable even after the group is deleted.

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

### Record media attachments

`TicketNote.media` and `IncidentHistory.media` are singular, nullable references
to `fileman.File`. REST callers may supply one existing File id, `null`, or a
compatible inline File payload. Existing-id resolution and File VIEW permission
use the shared File relation path; the incident models add one domain validator
after that candidate has been resolved.

An attachment is accepted only when the File is active and completed, its
`FileManager` is active, and both the File and manager have exactly the same
`group_id` as the parent `Ticket` or `Incident`. This also defines the groupless
case: both attachment rows must be groupless, while normal File VIEW permission
still decides whether the caller may use the File. All lifecycle and scope
failures use the same 400 response and do not persist the record.

Ticket notes derive `group_id` from their parent before their first save, even
if the request supplies another group. This replaces the former post-save repair
without changing note text, action-response, Maestro, or LLM processing order.
Incident history keeps its supplied group as audit provenance; it is not rewritten
from the parent. Deleting an attached File sets `IncidentHistory.media` to null so
the history row and its provenance survive.

Both record types serialize `media` with File's exact `reference` graph:
`id`, `filename`, `content_type`, and `category`. Storage paths, tokens, URLs,
manager details, ownership, metadata, and renditions are never included.

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
2. Resolves `group` via caller `group=` kwarg → `request.group` and snapshots `group_id`/`group_name` into metadata
3. Calls `event.sync_metadata()` to enrich with geo-IP data
4. Calls `event.publish()` to run rule matching and incident creation

To suppress group derivation even when `request.group` is set, pass `group=None` explicitly.

### Rate-limited reporting — `report_event_suppressed`

`report_event` files unconditionally — one call, one row. That is wrong when the
diagnostic sits on an **attacker-amplifiable** path: a public endpoint, or a
value a low-trust party (a tenant) can write. There, an anonymous caller can
drive the rate at which you file events, turning your incident table into a flood
sink. `report_event_suppressed` is the reusable answer.

```python
from mojo.apps import incident

filed = incident.report_event_suppressed(
    "refused redirect_uri 'https://evil.example/x' — not on any allowlist",
    key=host,                       # the suppression unit (host, source, group id)
    title="Refused redirect_uri",
    category="auth:oauth_redirect_refused",
    level=3,
    request=request,
    window=3600,                    # seconds; one event per (category, key) per window
    budget=50,                      # optional: cap DISTINCT keys per window
    fail_open=False,                # drop on a Redis outage (see below)
    redirect_host=host,             # extra kwargs -> event metadata
)
```

Signature:

```python
report_event_suppressed(details, key, title=None, category="api_error", level=1,
                        request=None, scope="global", window=3600, budget=None,
                        fail_open=True, **kwargs) -> bool
```

- **Returns `bool`** — `True` when an event was filed, `False` when it was
  suppressed (already reported this window), dropped (over budget), or the report
  itself failed. It **never raises**: every failure mode is swallowed, so a
  broken incident plane can never turn a check on a public endpoint into a 500.
- **`key`** is the suppression unit. `(category, key)` is filed at most **once per
  `window`**. Choose the unit that is useful to an operator and bounded by
  something other than the attacker — a host, a config source name, a group id —
  never the raw attacker-controlled string.
- **`window`** (default 3600s) is the suppression period. It is a **rolling** TTL:
  the notice key is written with `ex=window` on the first report and refreshed
  only on the next window, so a pair reported once stays quiet for up to `window`.
- **`budget`** (default `None` = unlimited) caps the number of **distinct keys** a
  category may file in one fixed wall-clock bucket. Use it when the caller can mint
  unbounded distinct keys (many hosts, many groups) so per-key suppression alone
  would still let the total grow without bound. When the budget is first exceeded,
  **one** `level=4` "budget exhausted" marker event is filed (so the cap is
  visible) and further keys are dropped silently until the bucket rolls over.
- **`fail_open`** decides Redis-outage behavior. `True` (default) files
  **without** suppression and logs a single warning per process — right for a
  low-rate, trusted-provenance diagnostic that must not be lost. `False` **drops**
  the event when Redis is unreachable — right for a public, amplifiable category,
  where an outage must not become an open floodgate into the incident table.
- **`group=None`** in `kwargs` is honored exactly as in `report_event` (suppresses
  the request-group auto-stamp); any other extra kwarg lands in event metadata.

**Atomicity.** The notice key is claimed with `redis.set(key, "1", nx=True,
ex=window)` — a single atomic round-trip, so two concurrent workers can never both
pass the check for the same `(category, key)`. There is no read-then-write race.

**Test helpers.** `incident.notice_key(category, key)` and
`incident.budget_key(category, window)` return the exact Redis keys the helper
uses, so a test can clear precisely the keys it will exercise (the DB and Redis
are long-lived) instead of flushing Redis:

```python
from mojo.apps.incident import notice_key, budget_key
from mojo.helpers.redis import get_connection

redis = get_connection()
redis.delete(notice_key("auth:oauth_redirect_refused", "app.example.com"))
redis.delete(budget_key("auth:oauth_redirect_refused", 3600))
```

> The older `redirect_allowlist.report_unlisted_destination` hand-rolls the same
> Redis suppression and predates this helper; new code should use
> `report_event_suppressed`.

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

Every permission denial issued by `MojoModel`'s permission system is automatically reported as an event. The REST dispatcher in `mojo/decorators/http.py` is the single emission site — events are only recorded when the request actually responds 401 or 403, never on recovery paths that return 200.

| Category | HTTP | Trigger |
|---|---|---|
| `unauthenticated` | 401 | Unauth request hit a perm-gated endpoint |
| `user_permission_denied` | 403 | User lacks system-level perms |
| `view_permission_denied` | 403 | `instance.check_view_permission` rejected |
| `edit_permission_denied` | 403 | `instance.check_edit_permission` rejected |
| `group_member_permission_denied` | 403 | Group-scoped perm check failed |
| `feature_disabled` | 403 | `CAN_UPDATE/CAN_DELETE/CAN_CREATE/CAN_BATCH = False` on the model. Per-row batch drops (`CAN_UPDATE`/`CAN_CREATE` false on an individual batch row, branch `batch_can_update_false`/`batch_can_create_false`) are the n/a exception — no HTTP error, batch response still returns 200 |
| `fk_attach_denied` | n/a | FK field silently skipped during save — no HTTP error, parent save still returns 200 |
| `batch_row_denied` | n/a | Batch row dropped by the per-row instance permission check — no HTTP error, batch response still returns 200 |

**Recovery paths emit no events.** When a list endpoint returns HTTP 200 with a scoped or empty result (Group list fallback, owner/group-filtered list, `MOJO_REST_LIST_PERM_DENY=False` branch), no denial event is recorded.

**`fk_attach_denied` metadata fields:** `field_name`, `related_model`, `related_id`, `branch`. These can be matched by Rules to alert when users attempt to attach records they cannot view.

**`batch_row_denied` metadata fields:** `branch` (`batch_update`/`batch_create`), `index`, `instance_id`, `model_name`, `request_path`. Emitted per dropped row from `on_rest_handle_batch` — see [Batch Save Permissions](../rest/permissions.md#batch-save-permissions).

**`feature_disabled` batch metadata fields:** same shape as `batch_row_denied` — `branch` (`batch_can_update_false`/`batch_can_create_false`), `index`, `instance_id`, `model_name`, `request_path`. Emitted per row dropped from `on_rest_handle_batch` because `CAN_UPDATE`/`CAN_CREATE` is `False`, distinct from the whole-request `feature_disabled` raised for `CAN_BATCH=False` itself (which carries the standard `PermissionDeniedException` metadata instead).

No extra code required — the dispatcher handles this automatically for all framework 401/403 paths.

---

## Event Fields

| Field | Description |
|---|---|
| `level` | Severity 0–15. 0–3 informational, 4–7 warning, 8–15 critical |
| `scope` | Logical domain: `"global"`, `"account"`, `"payment"`, etc. |
| `category` | Dot-namespaced event type: `"auth:failed"`, `"permission_denied"` |
| `source_ip` | IP address of the originating request. `CharField(max_length=45)`, nullable — holds a full IPv6 address or `None` |
| `hostname` | Server hostname (auto-populated from `socket.gethostname()`) |
| `uid` | User ID associated with this event |
| `country_code` | ISO 3166-1 alpha-2 country code (auto-populated via GeoIP) |
| `title` | Short human-readable summary |
| `details` | Full description, stack trace, or structured message |
| `model_name` | Related model (e.g., `"account.User"`) |
| `model_id` | Related model instance PK |
| `group` | Nullable FK to `account.Group` — auto-derived from request or caller context (see [Group Context](#group-context)) |
| `metadata` | JSON bag of arbitrary context — also used by Rules for field matching. Always includes `group_id` and `group_name` snapshots when a group is resolved. |

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

Any kwarg value that is a dict is automatically sanitized before being stored — sensitive keys (`password`, `token`, `api_key`, `secret`, `authorization`, etc.) are replaced with `*****`. This means passing `request_data=request.DATA` is safe even when the request contains a password field.

When a `request` is passed, metadata is also automatically enriched with:

```
request_ip, http_path, http_protocol, http_method,
http_query_string, http_user_agent, http_host,
user_name, user_email (if authenticated)
bearer (if authenticated, masked via mask_token)
```

**Bearer masking**: when the request is authenticated and carries a bearer token, `event_metadata["bearer"]` stores only a masked tail — typically `"****<last4>"` — via `mojo.helpers.logit.mask_token`. This is enough for support to correlate a token to a user-reported incident without leaving a replayable credential in the audit log. Tokens of length ≤ 4 are fully masked (`"*****"`) with no reveal. Masking is forward-only: pre-existing event rows keep whatever was stored at the time.

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
- **`priority`** — Evaluation order (lower = higher priority). First matching RuleSet wins. Hand-crafted defaults use 1–50; the LLM agent defaults new rulesets to priority 50, leaving room below for rules that must match first.
- **`match_by`** — `ALL` (all rules must match) or `ANY` (any rule can match)
- **`bundle_by`** — How to group events into one incident (see bundling below)
- **`bundle_minutes`** — Time window for bundling. `0` = disabled, `None` = unlimited, `>0` = window in minutes
- **`handler`** — What to do when a RuleSet triggers (see handlers below)
- **`trigger_count`** — Fire the handler when the incident reaches this many events. `null` = fire immediately on the first event.
- **`trigger_window`** — Only count events within this many minutes when checking `trigger_count`. `null` = count all events on the incident regardless of age.
- **`retrigger_every`** — Re-fire the handler every N additional events after the initial trigger. `null` = fire once only.

### Rule

Each Rule checks one field in `event.metadata` against a target value:

| Field | Description |
|---|---|
| `field_name` | The metadata key to inspect. Use bare names like `http_url`, `level`, `risk_score` — do not prefix with `metadata.` |
| `comparator` | `==`, `>`, `>=`, `<`, `<=`, `contains`, `regex` |
| `value` | The target value |
| `value_type` | `str`, `int`, `float`, `bool` |
| `index` | Evaluation order within the RuleSet |

Rules operate on `event.metadata`. Since `report_event` syncs all standard fields (level, category, source_ip, etc.) into metadata automatically, they are all available for rule matching alongside any custom fields you pass in.

`Rule.check_rule()` looks up the field by first checking `event.metadata[field_name]`, then falling back to `getattr(event, field_name)`. If `field_name` was stored with a `metadata.` prefix (e.g., `"metadata.http_url"`), the prefix is stripped automatically before lookup — so rules created that way still match correctly.

Field names starting with `_` are always refused, so private attributes on
`Event` can never be addressed from a rule.

### Rate fields — matching on an IP's recent behaviour

Because `check_rule()` falls through to `getattr(event, field_name)`, **any
property on `Event` is a matchable rule field**. Three are provided for writing
rules about a source IP's recent behaviour rather than about the single event in
hand. All three are scoped to `GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS` (24h)
and share the allowlists described in
[account/geoip.md](../account/geoip.md#threat-intelligence).

| Field | Value |
|---|---|
| `ip_recent_attack_events` | Count of allowlisted attack-category events (confirmed + suspect) for this event's `source_ip` in the window. `0` when the IP is clean or unset. |
| `ip_recent_distinct_targets` | Distinct user accounts those suspect-category events named. One person fumbling their own password scores `1`; a stuffing run scores many. |
| `ip_recent_distinct_devices` | Distinct pre-existing `BouncerSignal.muid`s seen behind the IP — a NAT/office egress scores high. **`None`** when the deployment has no bouncer telemetry, and `check_rule()` treats `None` as no-match, so a rule on this field fails **closed**. |

```python
# Block only an address that has been at it a while AND is spraying accounts,
# and is not obviously a shared office egress.
rs = RuleSet.objects.create(
    category="invalid_password",
    name="Credential stuffing — sustained and broad",
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=60,
    match_by=MatchBy.ALL,
    handler="block://?ttl=3600",
    trigger_count=5, trigger_window=60,
)
Rule.objects.create(parent=rs, index=0, field_name="ip_recent_attack_events",
                    comparator=">=", value="25", value_type="int")
Rule.objects.create(parent=rs, index=1, field_name="ip_recent_distinct_targets",
                    comparator=">=", value="10", value_type="int")
```

**No default RuleSet ships using these fields, and that is deliberate.** Each is
a database aggregate evaluated on the event publish path; a shipped default
would put that cost on every event in every deployment. Opt in when your traffic
justifies it, and pair the fields with a `trigger_count` so the aggregate is not
the only gate.

The three values are computed together and memoised on the event instance
(`Event._ip_stats`) — `check_by_category()` walks every RuleSet in a category, so
one event can read a field many times, and without the memo each read would be a
fresh query.

### Bundling

Bundling controls how related events are collapsed into a single incident rather than creating a new one for each event.

| `bundle_by` value | ID | Groups events by |
|---|---|---|
| `NONE` | 0 | Each event creates its own incident |
| `HOSTNAME` | 1 | Same server |
| `MODEL_NAME` | 2 | Same model type |
| `MODEL_NAME_AND_ID` | 3 | Same model instance |
| `SOURCE_IP` | 4 | Same source IP |
| `HOSTNAME_AND_MODEL_NAME` | 5 | Same server + model type |
| `HOSTNAME_AND_MODEL_NAME_AND_ID` | 6 | Same server + model instance |
| `SOURCE_IP_AND_MODEL_NAME` | 7 | Same IP + model type |
| `SOURCE_IP_AND_MODEL_NAME_AND_ID` | 8 | Same IP + model instance |
| `SOURCE_IP_AND_HOSTNAME` | 9 | Same IP + server |
| `GROUP_ID` | 10 | Same group (tenant) — recommended for multi-tenant noise isolation |
| `GROUP_AND_MODEL_NAME` | 11 | Same group + model type |
| `GROUP_AND_MODEL_NAME_AND_ID` | 12 | Same group + model instance |
| `GROUP_AND_SOURCE_IP` | 13 | Same group + source IP — recommended default for multi-tenant attack-pattern detection |

### Thresholds (trigger_count)

A RuleSet can defer its handler until a minimum event count is reached. Until the threshold is crossed, the incident stays at `pending`. Once it is reached, the incident transitions to `new` and the handler fires.

Use `trigger_window` to restrict the count to events within a recent time window — useful when the same incident might accumulate events over hours or days but you only want to trigger on a burst.

```python
RuleSet.objects.create(
    category="auth:failed",
    name="Brute Force Detection",
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=10,
    handler="block://?ttl=3600,ticket://?status=new&priority=8&category=security",
    trigger_count=10,       # Wait for 10 events on this incident
    trigger_window=10,      # Only count events from the last 10 minutes
)
```

Until 10 events accumulate within the window, the incident sits at `pending`. Once the threshold is crossed, it transitions to `new` and the handler fires — blocking the IP fleet-wide and creating a ticket.

> **`bundle_minutes` must cover `trigger_window`.** The count is per-incident. With `bundle_minutes=0` (bundling disabled) every event creates its own incident, so the count never climbs past 1 and the `trigger_count` **disables the ruleset** instead of deferring it. Set `bundle_minutes` to at least `trigger_window`.

Any RuleSet with a `block://` handler and no `trigger_count` fires on the **first** matching event. That is correct only when no legitimate user can produce the match — see [Default auth and bouncer rulesets](#default-auth-and-bouncer-rulesets) for how the shipped defaults draw that line.

### Retriggering (retrigger_every)

Set `retrigger_every` to re-fire the handler after additional events beyond the initial trigger. This is useful for escalating alerts on incidents that keep growing:

```python
RuleSet.objects.create(
    category="auth:failed",
    name="Brute Force - Escalating",
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=60,
    handler="notify://perm@manage_security",
    trigger_count=10,
    retrigger_every=20,     # Re-notify every 20 additional events after the first trigger
)
```

With this config: handler fires at 10 events, then again at 30, 50, 70, and so on.

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
| `maestro` | Set `1` to report the Ticket to the configured Maestro default board |
| `board` | Remote Maestro board id; also opts the Ticket into Maestro reporting (see [Maestro Workspace Reporting](../security/maestro_board.md)) |

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

### Incident Deletion Lifecycle

Incidents can be automatically deleted at two points:

**1. Delete on resolution (immediate)**

If a RuleSet has `metadata.delete_on_resolution = True`, any incident it creates is automatically deleted the moment it transitions to `resolved` or `closed`. This keeps the database clean for high-volume noise patterns (bot scanners, brute-force probes, health check blips) that generate incidents only as bookkeeping artifacts — once handled, there is no value in retaining them.

This is triggered from all resolution paths: REST saves, the BlockHandler, and the LLM agent.

```python
RuleSet.objects.create(
    name="Bot Scanner Noise",
    category="ossec",
    handler="block://?ttl=3600",
    metadata={"delete_on_resolution": True},
)
```

**2. Periodic pruning (`prune_incidents` job)**

The `prune_incidents` async job deletes resolved, closed, and ignored incidents older than `INCIDENT_PRUNE_DAYS` (default: 90 days). It runs on a schedule and skips any incident protected by `do_not_delete`.

**Protecting serious incidents from deletion**

Set `metadata.do_not_delete = True` on an incident to prevent both auto-deletion on resolution and periodic pruning. Use this for confirmed serious threats — real intrusions, active data exfiltration, or incidents that must be retained for compliance or forensics.

```python
incident.metadata["do_not_delete"] = True
incident.save(update_fields=["metadata"])
```

`do_not_delete` overrides any RuleSet `delete_on_resolution` setting. When in doubt, leave it unset — protect only when there is a clear reason.

**`check_delete_on_resolution()` — the deletion method**

All resolution paths call `incident.check_delete_on_resolution()` after a status change. It returns `True` if the incident was deleted, `False` otherwise. It is a no-op when:
- The incident status is not `resolved` or `closed`
- `metadata.do_not_delete` is `True`
- The incident has no linked RuleSet
- The RuleSet does not have `metadata.delete_on_resolution = True`

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

### Default OSSEC rulesets — `ensure_ossec_rules()`

Call `RuleSet.ensure_ossec_rules()` during deployment or migrations to install the framework's built-in OSSEC rulesets. The method is idempotent — it creates missing rulesets and skips ones that already exist.

These rulesets exist to absorb the high-volume, low-value events that OSSEC generates constantly. Without them, each unmatched event either creates its own incident (when `level >= INCIDENT_LEVEL_THRESHOLD`) or falls through to the LLM triage handler — neither of which is useful for routine internet background noise.

**Priority order (lower = checked first, first match wins):**

| Priority | Name | Matches | Action |
|---|---|---|---|
| 1 | OSSEC - Bot/Scanner Patterns | `details` regex matches PHP, `.git`, `.asp`, `.env`, `cgi-bin`, `wp-content`, `wlwmanifest`, `locale.json`, `.jsp`, `.cfm`, `.cgi`, `dns-query`, `/vendor/phpunit`, `eval-stdin` | `block://?ttl=3600&fleet_wide=1`, no incident |
| 2 | OSSEC - Login Session Noise | `details` regex matches `Login session (opened\|closed).` | `ignore`, no incident |
| 3 | OSSEC - SSH Single Probe (5710) | `rule_id == 5710` | `block://?ttl=3600&fleet_wide=1`, no incident |
| 5 | OSSEC - SSH Brute Force | `rule_id` in `[5712, 5720, 5551, 5758]` | `block://?ttl=3600&fleet_wide=1`, no incident |
| 6 | OSSEC - Generic Web Errors | `details` regex matches `Web (Attack )?4(00\|04\|05)` | `block://?ttl=300&fleet_wide=1` after **10 events in 10 minutes**, no incident |
| 10 | OSSEC 31104 - Web Attack Detection | `rule_id == 31104` | `block://?ttl=3600&fleet_wide=1`, no incident |
| 50 | OSSEC - Critical Severity | `level >= 12` | `block://?ttl=3600&fleet_wide=1` + incident created for review |

**Why each ruleset exists:**

- **Priority 1 — Bot/Scanner Patterns**: Matches known automated scanner paths and blocks the IP for one hour. The regex was extended in v1.1.4 to include `/vendor/phpunit` and `eval-stdin` paths used by PHP exploit scanners.
- **Priority 2 — Login Session Noise**: PAM `session opened` / `session closed` log entries have no source IP and are pure local audit bookkeeping. They are not attacks. Without this ruleset, OSSEC level 3 events for these messages were hitting the catch-all and generating admin-visible incidents at the rate of hundreds per day.
- **Priority 3 — SSH Single Probe (5710)**: OSSEC rule 5710 fires on the first SSH attempt with a non-existent username. Scanners typically probe once and move on, so they never accumulate enough events to trip the multi-attempt brute-force rule (priority 5). Without this ruleset, each single-probe scanner IP created its own unmatched incident. Block it immediately.
- **Priority 5 — SSH Brute Force**: Multi-attempt SSH brute force (rules 5712/5720/5551/5758). Immediate block, no incident.
- **Priority 6 — Generic Web Errors**: Any Web 400/404/405 that is not already caught by the bot/scanner URL pattern check (priority 1). Catches phpunit/vendor scanner sweeps and other generic HTTP probers. Short block (5 minutes) — these are internet noise, not targeted attacks. Gated at 10 events in 10 minutes: one 404 is a stale bookmark or a dead link in an email, while a sweep produces dozens in seconds. `bundle_minutes` is 10 rather than 0 so the events land on one incident and the count can actually climb — see the warning under [Retuning an already-bootstrapped ruleset](#retuning-an-already-bootstrapped-ruleset).
- **Priority 10 — Web Attack Detection**: OSSEC rule 31104 specifically. One-hour block, no incident.
- **Priority 50 — Critical Severity**: Level 12+ is unusual. Only this default creates an incident — something genuinely unexpected happened and warrants human review.

**Usage:**

```python
from mojo.apps.incident.models import RuleSet

# Called once at deployment time, safe to call repeatedly
RuleSet.ensure_ossec_rules()
```

Rulesets installed by `ensure_ossec_rules()` are identified by name. If you need to modify a default ruleset's behavior (e.g., change a TTL or priority), edit it via the admin interface or REST API after installation — the method will not overwrite existing records.

---

## Default auth and bouncer rulesets

These are the defaults that firewall real people. `ensure_auth_rules()` and `ensure_bouncer_rules()` install them; both are idempotent and both are called by `ensure_default_rules()`.

### The design rule

django-mojo is an open REST platform — anyone can call it, and a large share of callers sit behind a corporate NAT, a school, or carrier-grade NAT where one address fronts thousands of unrelated people. Reactive blocking of a demonstrated bad actor is wanted. **False positives are the thing to avoid.** So the bar for every one of these rulesets is "highly probable bad actor", never "a user fumbled their credentials."

Mechanically that means: **any default whose match a legitimate person can produce must carry a `trigger_count`.** A `block://` handler with no `trigger_count` fires on the *first* matching event — one mistyped username would firewall the whole egress. The two rulesets below that stay ungated do so because no legitimate user can produce their match at all.

### Auth — `ensure_auth_rules()`

| Name | Category | Matches | Gate | Action |
|---|---|---|---|---|
| Auth - Credential Stuffing | `login:unknown` | `level >= 8` | **25 events / 60 min** | `block://?ttl=1800&fleet_wide=1` |
| Auth - Password Brute Force | `invalid_password` | `level >= 5` | **5 events / 15 min** | `block://?ttl=1800&fleet_wide=1` |
| Auth - Bouncer Token Abuse | `security:bouncer:token_invalid` | `level >= 7` | **10 events / 30 min** | `block://?ttl=1800&fleet_wide=1` |

- **Credential Stuffing** — a `login:unknown` event means the submitted username does not exist. One is a typo, or someone who forgot which email they signed up with. 25 in an hour from one address is a list being worked. `bundle_minutes` is 60 to match the counting window.
- **Password Brute Force** — the login view emits `invalid_password` at level 5 only after a username has resolved, so this counts failed guesses against *known* accounts. Note the per-account sliding window in `mojo/decorators/limits.py` is the control that survives IP rotation; this ruleset is the per-address complement, not a replacement.
- **Bouncer Token Abuse** — see the level split below. Only tampering reaches level 7, so ordinary token lifecycle failures never match this ruleset at all, and 10 tampered tokens in 30 minutes are still required before it blocks.

#### Bouncer token failure levels

`@md.requires_bouncer_token()` (`mojo/decorators/bouncer.py`) reports `security:bouncer:token_invalid` at a level that depends on *why* validation failed:

| Cause | Level | Why |
|---|---|---|
| `expired` | 4 | The 15-minute TTL ran out while the user read the page. |
| `nonce_consumed` | 4 | Double-submitted form — the nonce is single-use. |
| `ip_mismatch` | 4 | Cellular or CGNAT handoff changed the egress IP mid-session. |
| `invalid_format` | 7 | Not a well-formed token. |
| `invalid_signature` | 7 | Forged or tampered with. |
| `page_type_mismatch` | 7 | A valid token replayed against a different endpoint. |
| `duid_mismatch` | 7 | A valid token replayed from a different device. |

**Log-only mode caps every cause at level 4.** When `BOUNCER_REQUIRE_TOKEN` is False the deployment has not enabled enforcement, so nothing the bouncer observes may get an address firewalled — whatever the cause. Turning enforcement on is what makes the level-7 causes able to reach a blocking rule.

### Bouncer — `ensure_bouncer_rules()`

| Name | Category | Matches | Gate | Action |
|---|---|---|---|---|
| Bouncer - Honeypot Credential Stuffing | `security:bouncer:honeypot_post` | `level >= 9` | **none — first event** | `block://?ttl=3600&fleet_wide=1` |
| Bouncer - Bot Campaign Detection | `security:bouncer:campaign` | `level >= 10` | **none — first event** | `block://?ttl=86400&fleet_wide=1` + notify |
| Bouncer - High Confidence Bot Block | `security:bouncer:block` | `risk_score >= 80` | **3 events / 30 min** | `block://?ttl=3600&fleet_wide=1` |
| Bouncer - In-Session Freeze | `security:bouncer:session_freeze` | `level >= 9` | **3 events / 60 min** | `block://?ttl=86400&fleet_wide=1` + notify |
| Bouncer - In-Session Shadow Ban | `security:bouncer:session_shadow_ban` | `level >= 8` | n/a — no block | `notify://perm@manage_security` |
| Bouncer - In-Session Step-Up Required | `security:bouncer:session_step_up` | `level >= 6` | n/a — no block | no handler; bundles for visibility |
| Bouncer - In-Session Suspect | `security:bouncer:session_suspect` | (no rules) | n/a — no block | no handler; bundles for visibility |

- **Honeypot and Campaign stay ungated deliberately.** A honeypot POST means credentials were submitted to a page no real user can reach; campaign detection already requires 5+ distinct blocks sharing a signal pattern. Both are proof on their own — adding a trigger gate would only delay a certain block.
- **High Confidence Bot Block** is a heuristic on browser signals. One 80+ score can be a privacy extension, an unusual corporate browser build, or somebody's headless integration test, so the firewall waits for the pattern to repeat.
- **In-Session Freeze** costs the address a full day of firewall. One scorer verdict — which a paused laptop or an assistive tool can produce — is far too cheap a trigger for that penalty.

### Retuning an already-bootstrapped ruleset

`_create_ruleset()` uses `get_or_create(defaults=...)`. **Changing a default in code only affects deployments that have not bootstrapped yet.** An existing deployment keeps whatever rows it already has — deliberately, so a framework upgrade can never silently rewrite security policy an operator tuned by hand. The trade-off is that upgrading does not fix an existing deployment for you; the REST calls below do.

**Which deployments are affected.** Any deployment bootstrapped before the trigger gates were added has these five rulesets with `trigger_count = null`, meaning each one firewalls an IP **fleet-wide on a single event**:

| Ruleset | One event means |
|---|---|
| Auth - Credential Stuffing | One mistyped username → the whole egress blocked for 30 minutes |
| Auth - Bouncer Token Abuse | One expired token → the whole egress blocked for 30 minutes |
| Bouncer - High Confidence Bot Block | One heuristic bot score → 1 hour |
| Bouncer - In-Session Freeze | One risk-90 session → 24 hours |
| OSSEC - Generic Web Errors | One 404 → 5 minutes |

On a corporate NAT or CGNAT, "the whole egress" is everybody behind that address.

**Check what you have:**

```
GET /api/incident/event/ruleset?size=100
```

Look for any ruleset whose `handler` contains `block://` and whose `trigger_count` is `null`.

**Apply the current defaults.** POST to the ruleset's id with the fields to change (requires `manage_security` or `security`):

```
POST /api/incident/event/ruleset/<id>
{"trigger_count": 25, "trigger_window": 60, "bundle_minutes": 60}
```

The five, with the values the framework now ships:

| Ruleset | `trigger_count` | `trigger_window` | `bundle_minutes` |
|---|---|---|---|
| Auth - Credential Stuffing | 25 | 60 | 60 |
| Auth - Bouncer Token Abuse | 10 | 30 | 30 (already) |
| Bouncer - High Confidence Bot Block | 3 | 30 | 30 (already) |
| Bouncer - In-Session Freeze | 3 | 60 | 60 (already) |
| OSSEC - Generic Web Errors | 10 | 10 | **10 — was 0** |

> **`bundle_minutes` must cover `trigger_window`.** The threshold counts events on *one* incident. `bundle_minutes = 0` disables bundling, so every event lands on its own incident, the count never climbs past 1, and adding a `trigger_count` **disables the rule** rather than loosening it. This is why OSSEC - Generic Web Errors needs its `bundle_minutes` raised alongside the gate.

Equivalent from a Django shell or a deployment script:

```python
from mojo.apps.incident.models import RuleSet

RuleSet.objects.filter(name="Auth - Credential Stuffing").update(
    trigger_count=25, trigger_window=60, bundle_minutes=60)
```

To verify a retune took effect, publish a single matching event and confirm the incident sits at `status="pending"` rather than transitioning to `new`.

---

## MojoSec evidence projection

MojoSec receipts remain the durable original audit record. Bounded raw request
targets/referrers/user agents live in `MojoSecReceipt.replay_features`; that
field is sensitive, excluded from the default graph, and the receipt model is
`DENY_AI`. The Event receives
only the central per-kind projection: canonical source/peer IP, method, user,
TTY, host, status/upstream numbers, token-normalized path, HTTP(S) referrer
origin plus queryless token-normalized path, and structured UA family/major,
digest, and centrally scrubbed bounded display. Web request identity,
protocol/TLS, ports, byte counts and upstream measurements also project after
field-local validation; one bad subfield is omitted rather than retrying the
whole deterministic event. Sudo command context is intentionally richer for
security administrators: `metadata.mojosec.evidence` exposes the exact accepted
`command` (2,048 UTF-8 bytes maximum), `command_path` and `cwd` (512 bytes each),
actor, target user, TTY, boot ID, audit session, and explicit `attribution`.
True-only `<field>_truncated` markers identify sensor-retained prefixes, and
any present marker value other than literal boolean `true` invalidates and
omits both the marker and its paired field.
`command_family` is additive classification from a valid complete path: every
such path yields a known server-owned family or literal `unknown`; invalid,
missing, and truncated paths yield no family. The command digest remains
receipt-only. No secret-pattern redaction,
argument removal, or URL rewriting occurs on this existing
`view_security`/`security` Event surface. System-service kinds
(`system.service_error`, `system.oom`) project only a validated failed-unit
name (or `kernel`) and a bounded `failure_kind`; the raw journal message stays
receipt-only.
Malformed optional sudo fields are omitted independently; exact accepted
command text, including secret-looking arguments, remains visible to authorized
Event readers by product policy.

Source-bearing SSH, reliably attributed sudo, and known web kinds populate
`Event.source_ip`. Sudo evidence always reports `attribution`: `audit_session`
requires a valid IP, sensor-shaped actor and boot-ID strings, and an audit
session; `who` requires a valid IP plus sensor-shaped actor and TTY strings.
Every incomplete, invalid, or non-string proof tuple becomes explicit `none`
and leaves `Event.source_ip` null. For `web.probe` and `web.denied`, sensor
fingerprints include each projected identity scalar, so an aggregate cannot
misrepresent interleaved IP/host/method/status values. `web.error` fingerprints omit
source/peer IP by design — one server fault hits every client at once, so its
aggregate deliberately collapses across callers — and its `Event.source_ip`
holds only a latest-occurrence witness, not an actor attribution.
For a count-one web Event, occurrence-specific values remain top-level. For a
count greater than one, every volatile value appears only under
`last_occurrence_sample`, with `semantics="last_occurrence"` and
`observed_at` equal to authoritative `last_seen`; it is never presented as a
property of the entire bucket. `auth.session_open` is level-2 local PAM
service evidence, distinct from remote `auth.ssh_login`, and uses only exact
audit-session attribution.
The source and host recommendation remain evidence only: automated action
requires an enrolled installation and an exact active server-owned RuleSet.

## Integration with GeoLocatedIP

The incident system and `GeoLocatedIP` form a feedback loop:

1. **Events enrich GeoLocatedIP**: When events arrive with a `source_ip`, `sync_metadata()` calls `GeoLocatedIP.geolocate()` to attach geo and threat data.
2. **Incidents escalate threat levels**: `GeoLocatedIP.update_threat_from_incident(priority)` is called when incidents are created. This escalates `threat_level` (never downgrades). It does not auto-block — blocking is handled by the rule engine (`block://` handlers) which has full context on conditions and TTLs.
3. **GeoLocatedIP data feeds rules**: Rules can match on `risk_score`, `is_tor`, `is_vpn`, `threat_level`, `country_code` — any GeoIP field that ends up in event metadata.
4. **Block/unblock flows through GeoLocatedIP**: The `block://` handler and admin actions both go through `GeoLocatedIP.block()`, ensuring a single code path for DB updates and fleet broadcasts.
5. **Events decide `is_known_attacker` / `is_known_abuser`**: `check_threats()` reads `incident.Event` rows for the address. Only categories on the two attacker allowlists count, and only inside a 24-hour window — a `rest_error` from a server bug, a permission denial, or the bouncer's own block decision counts toward **nothing**. See [Threat Intelligence](../account/geoip.md#threat-intelligence).
6. **Escalation decays**: because steps 2 and 4 only ratchet up, the incident app runs a daily `recheck_active_threats` cron that re-scores recently-active addresses and lets a lower recomputed `threat_level` replace the stored one.

Two consequences worth keeping in mind when adding a new event category:

- A new category is **not** attack evidence until someone adds it to
  `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES` or
  `..._SUSPECT_CATEGORIES`. Raising an event's `level` no longer makes it count.
  This is the intended failure direction — new categories are far more often
  operational noise than attacks.
- If you report an event about a specific account, report it from the
  **instance** (`user.report_incident(...)`), not the class. Instance reporting
  stamps `model_name`/`model_id`, and the distinct-account breadth gate — the
  thing that separates a stuffing run from one locked-out user — can only see
  events that name an account.

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

A RuleSet bundling by `SOURCE_IP` with `trigger_count=20` and `trigger_window=1` fires only when a real abuse pattern emerges — not on a single slow request.

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
    trigger_count=10,
    trigger_window=5,
)
```

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `INCIDENT_LEVEL_THRESHOLD` | `7` | Minimum level to auto-create an incident without a matching RuleSet |
| `INCIDENT_EVENT_PRUNE_DAYS` | `30` | Days to retain events with level < 6 |
| `INCIDENT_PRUNE_DAYS` | `90` | Days to retain resolved/closed/ignored incidents before pruning. Incidents with `metadata.do_not_delete = True` are exempt. |
| `INCIDENT_EVENT_METRICS` | — | Enable metrics recording for events and incidents |
| `INCIDENT_METRICS_MIN_GRANULARITY` | `"hours"` | Granularity for incident metrics |
| `FIREWALL_BLOCKED_IPSET_NAME` | `"mojo_blocked"` | Name of the kernel ipset used for permanent IP blocks. Change only if you have a naming conflict with an existing ipset. |
| `GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS` | `24` | Window the `ip_recent_*` rule fields and the `is_known_attacker` / `is_known_abuser` predicates count over |
| `GEOLOCATION_RECHECK_THREATS_MAX` | `500` | Rows the daily `recheck_active_threats` decay cron processes |

### CloudWatch alarm events

The AWS SNS receiver records CloudWatch transitions as
`scope="aws:cloudwatch"`, `category="aws:cloudwatch:alarm"`. It calls the
incident engine without wildcard fallback, so only an explicit scope/category
RuleSet can open work. Use `BundleBy.MODEL_NAME_AND_ID` with
`bundle_minutes=None`: `model_id` identifies one active alarm occurrence and
changes after recovery. `ALARM` evaluates policy, `INSUFFICIENT_DATA` preserves
the active incident, and `OK` resolves it through the same programmatic
lifecycle path used by `ResolveHandler`.

See [AWS CloudWatch monitoring](../aws/cloudwatch.md#sns-alarm-ingestion) for
the receiver, allowlist, idempotency, and ticket/Maestro configuration.

### GuardDuty finding events

The GuardDuty SNS receiver records findings as `scope="aws:guardduty"`,
`category="aws:guardduty:<finding type>"`. It publishes with
`use_catchall=False`, so only an explicit scope RuleSet can open work — write
the policy against the **scope**, since a per-type category would need one
RuleSet per finding type. `RuleSet.ensure_guardduty_rules()` installs the
shipped opt-in policy; it is not part of `ensure_default_rules()`.

Severity maps to level deliberately below the threshold: Critical, High and
Medium all map to **6**, Low to 4, Informational to 2. Nothing reaches
`INCIDENT_LEVEL_THRESHOLD`, so enabling the receiver alone creates no incident,
no LLM triage job, and no IP threat stamp — the same posture as CloudWatch
alarms. 6 rather than 4 for Medium and above because `prune_events` deletes
`level__lt=6`.

Use `BundleBy.MODEL_NAME_AND_ID` with `bundle_minutes=None`: `model_id` carries
a rotating occurrence handle that is cleared when the incident goes terminal.
That rotation is what stops a finding recurring months later from bundling back
into its resolved incident — `determine_bundle_criteria` has no status filter.
A repeat while the incident is live links the Event and escalates priority
without re-dispatching handlers.

`source_ip` is set only when the finding's remote address is an **origin**
(inbound connection, API call, port probe, Kubernetes API call). An outbound
connection's peer is a destination our own host chose and is recorded in
metadata only, so it never reaches inbound threat scoring.

See [AWS GuardDuty ingestion](../aws/guardduty.md) for the EventBridge wire,
the allowlist, the dedupe contract, and the bounded metadata list.

The rest of the `GEOLOCATION_INTERNAL_*` family (the attacker allowlists, the
breadth gate, the shared-egress suppressor, the dry-run switch) is documented in
[account/geoip.md](../account/geoip.md#threat-intelligence). All of them are
re-read on every call, so a DB-backed `Setting` retunes detection without a
restart.

---

## Account observability categories

The account app routes eight operational diagnostics through
`report_event_suppressed` (window = 3600s, no budget, `fail_open=True`) instead
of file-log warnings, so a misconfiguration surfaces as a suppressed, correlatable
incident rather than a line in `mojo.log` that no one reads. None is `>= 7`, so
none auto-creates an incident on its own — they are signal for rules and the feed,
not pages.

| Category | Level | Suppression unit (per hour) | Meaning |
|---|---|---|---|
| `auth:handoff_group_token_inert` | 6 | global (`inert`) | The gating map/resolver is configured but `AUTH_HANDOFF_GROUP_TOKEN_MODE` is `off` — a security control switched off while configured on. Every destination receives a platform JWT. |
| `auth:handoff_group_token_entry_widened` | 3 | per derived host | A gating-map entry carried a scheme, port or path; all were **dropped**. The bare-host DENY rule now covers the host **AND all of its subdomains** (more, not less). List the bare host. |
| `account:realtime_disconnect_failed` | 6 | per user pk | A disabled/revoked user's live websocket could not be force-closed. `auth_key` was still rotated (outstanding JWTs are dead); the socket may persist until it drops naturally. |
| `account:login_no_client_ip` | 5 | global (**not** per-user) | A login was recorded with no resolved client IP — a reverse-proxy/ingress that is not forwarding the client address, affecting **every** request. A per-user key would flood the plane, so the key is deliberately global. |
| `geoip:abuse_push_unconfigured` | 5 | global | Outbound abuse-signal push-back is enabled but `GEOIP_MOJO_PROVIDER_URL` or `GEOIP_API_KEY_MOJO` is unset — every push is dropped. Names the settings, never their (secret) values. |
| `geoip:abuse_push_rejected` | 4 | per HTTP status | The upstream provider rejected an abuse-signal push with a 4xx; not retried. Carries the ip, status and response body. |
| `geoip:abuse_push_missing_ip` | 4 | global | An abuse-signal push job carried no `ip`; dropped. Body names the payload KEYS only — never the values, which are abuse state. |
| `geoip:abuse_push_no_signals` | 4 | global | An abuse-signal push job carried an ip but no signal fields; dropped. Body names the payload KEYS only. |

**Two deliberately-unconverted file logs.** `handoff_group._should_report` and
`redirect_allowlist.report_unlisted_destination` each keep a `logit.warning` in
the `except` around their own Redis suppression call. That branch **is** the
suppression machinery's degraded path (Redis unreachable); filing an incident
there is unsuppressible-by-construction and would recurse on any `report_event`
fault, so it stays a file log and the caller reports UNSUPPRESSED on purpose. A
later log-to-incident sweep must leave both alone.

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
