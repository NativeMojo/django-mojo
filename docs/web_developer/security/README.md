# System Security — Web Developer Guide

How to build a security operations dashboard using django-mojo's APIs. This guide ties together incidents, events, firewall, bouncer, logs, and metrics into a single picture.

## Permissions

Two permissions control all security-related access:

| Permission | Access |
|------------|--------|
| `view_security` | Read-only: incidents, events, history, tickets, rules, firewall status |
| `manage_security` | Full: edit incidents, manage tickets, create rules, block IPs, merge incidents |

## The Security Pipeline

```
Detection → Event → Rules → Incident → Handlers → Enforcement
```

1. **Detection** — failed logins, rate limits, OSSEC alerts, bouncer blocks, app errors
2. **Events** — every detection creates an Event record with category, level, and metadata
3. **Rules** — RuleSets match events by category and apply threshold/bundling logic
4. **Incidents** — matched events are grouped into Incidents for investigation
5. **Handlers** — rules fire actions: block IPs, send emails/SMS, create tickets, invoke LLM
6. **Enforcement** — IP blocks propagate fleet-wide via iptables/ipset

## APIs at a Glance

| API | Path | What it provides |
|-----|------|-----------------|
| Incidents | `/api/incident/incident` | Security incidents with status, priority, category |
| Events | `/api/incident/event` | Raw security events that feed into incidents |
| History | `/api/incident/incidenthistory` | Audit trail for each incident |
| Tickets | `/api/incident/ticket` | Human review items, LLM conversation threads |
| Ticket Notes | `/api/incident/ticketnote` | Ticket conversation (human + LLM) |
| RuleSets | `/api/incident/event/ruleset` | Rule engine configuration — categories, bundling, trigger thresholds, handlers |
| Rules | `/api/incident/event/ruleset/rule` | Conditions within a RuleSet (field comparisons) |
| GeoIP | `/api/system/geoip` | IP records, block status, threat level, geolocation |
| Logs | `/api/logit/log` | Audit logs, firewall history |
| Metrics | `/api/metrics/fetch` | Time-series data for dashboards |
| Bouncer (client) | `/api/account/bouncer/assess` | Bot detection (called by bouncer JS, not your app) |
| Bouncer Devices | `/api/account/bouncer/device` | Device reputation, risk tiers, block counts |
| Bouncer Signals | `/api/account/bouncer/signal` | Assessment audit trail with full signal payloads |
| Bot Signatures | `/api/account/bouncer/signature` | Manage bot signatures (auto-learned + manual) |
| IPSet | `/api/incident/ipset` | Bulk CIDR blocking: countries, datacenters, abuse lists |

See individual API docs for full details:
- [Incidents](../logging/incidents.md)
- [Events & Reporting](../logging/reporting_events.md)
- [Firewall & GeoIP](../account/firewall.md)
- [Logs](../logging/logs.md)
- [Metrics](../metrics/metrics.md)
- [Bouncer](../account/bouncer.md)
- [GeoIP](../account/geoip.md)

## Building a Security Dashboard

### 1. Incident Queue

The main view. Show incidents that need attention:

```
GET /api/incident/incident?status=new&sort=-priority,-created&size=50
```

This returns incidents that haven't been handled by a human or the LLM agent.

If the LLM agent is configured, most incidents flow through automatically:
- `new` → LLM picks it up → `investigating` → `resolved` or `ignored`
- Humans only see `status=open` (things the LLM escalated or humans claimed)

**Recommended tabs:**

| Tab | Filter | Purpose |
|-----|--------|---------|
| Unhandled | `status=new` | Needs attention (human or LLM) |
| My Work | `status=open` | Human-owned incidents |
| LLM Active | `status=investigating` | LLM is working on these |
| Resolved | `status=resolved` | Recently resolved |
| Ignored | `status=ignored` | Noise (review periodically) |

### 2. Incident Detail

For a single incident, fetch the incident + its history + its events:

```
GET /api/incident/incident/301
GET /api/incident/incidenthistory?parent=301&sort=created
GET /api/incident/event?incident=301&sort=-created
```

The history shows the full timeline: creation, handler execution, LLM assessments, admin edits, merges.

### 3. Firewall Status

Show currently blocked IPs and recent firewall activity:

```
GET /api/system/geoip?is_blocked=true&sort=-blocked_at
GET /api/logit/log?kind=firewall:block&sort=-created&size=20
```

**Firewall log `kind` values:**

| Kind | Meaning |
|------|---------|
| `firewall:block` | IP blocked (manual or rule) |
| `firewall:unblock` | IP unblocked |
| `firewall:whitelist` | IP whitelisted |
| `firewall:unwhitelist` | Whitelist removed |
| `firewall:auto_block` | Auto-blocked from incident escalation |

All firewall logs include structured `payload` JSON with `ip`, `reason`, `trigger`, and action-specific fields. Parse `payload` for dashboard cards.

### 4. Bouncer Status

Show bot detection activity and device reputation:

```
GET /api/account/bouncer/signal?decision=block&sort=-created&graph=list&size=20
GET /api/account/bouncer/device?risk_tier=blocked&sort=-block_count&size=20
GET /api/account/bouncer/signature?is_active=true&sort=-hit_count&size=20
```

Bouncer events also create incidents — query them alongside other security incidents:

```
GET /api/incident/incident?category__startswith=security:bouncer&sort=-created
```

See [Bouncer Admin APIs](../account/bouncer.md#admin-visibility-apis) for full endpoint reference, signal payloads, and dashboard patterns.

### 5. Metrics Dashboards

Fetch time-series data for charts:

**Firewall metrics:**

```
GET /api/metrics/fetch?slug=firewall:blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?slug=firewall:auto_blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?category=firewall&granularity=days
```

**Bouncer metrics:**

```
GET /api/metrics/fetch?slug=bouncer:blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?slug=bouncer:pre_screen_blocks&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?category=bouncer&granularity=days
```

**Incident metrics:**

```
GET /api/metrics/fetch?slug=incidents&account=incident&granularity=hours&dr_start=2026-03-20
GET /api/metrics/fetch?slug=incidents:escalated&account=incident&granularity=hours
GET /api/metrics/fetch?slug=incidents:resolved&account=incident&granularity=hours
GET /api/metrics/fetch?slug=incidents:threshold_reached&account=incident&granularity=hours
```

**Event volume:**

```
GET /api/metrics/fetch?slug=incident_events&account=incident&granularity=hours
GET /api/metrics/fetch?category=incident_events_by_country&account=incident&granularity=days
```

**Available metric slugs:**

| Slug | Category | What it tracks |
|------|----------|---------------|
| `firewall:blocks` | firewall | Manual + rule-based IP blocks |
| `firewall:auto_blocks` | firewall | Auto-blocks from threat escalation |
| `firewall:blocks:country:{CC}` | firewall | Blocks by country code |
| `firewall:broadcasts` | firewall | Fleet-wide block broadcasts |
| `incidents` | — | Incidents created |
| `incidents:escalated` | — | Priority escalations |
| `incidents:resolved` | — | Incidents resolved |
| `incidents:threshold_reached` | — | Pending → new transitions |
| `bouncer:assessments` | bouncer | Total bouncer scoring runs |
| `bouncer:blocks` | bouncer | Bouncer blocks (full scoring) |
| `bouncer:blocks:country:{CC}` | bouncer | Bouncer blocks by country |
| `bouncer:monitors` | bouncer | Suspicious but allowed |
| `bouncer:pre_screen_blocks` | bouncer | Signature cache hits (served decoy) |
| `bouncer:honeypot_catches` | bouncer | Credential attempts on decoy pages |
| `bouncer:signatures_learned` | bouncer | Auto-created bot signatures |
| `bouncer:campaigns` | bouncer | Coordinated bot campaign detections |
| `incident_events` | — | Total events |
| `incident_events:country:{CC}` | incident_events_by_country | Events by country |

### 6. Ticket Management

Tickets are how the LLM agent communicates with humans:

```
GET /api/incident/ticket?status=open&sort=-priority
```

Tickets with `metadata.llm_linked=true` are part of an LLM conversation. When you post a note, the LLM reads it and responds:

```
POST /api/incident/ticketnote
{
  "parent": 10,
  "note": "Approved. Enable the rule with threshold 10."
}
```

The LLM will post a follow-up note automatically. Check `ticketnote?parent=10&sort=created` to see the conversation.

### 7. Event Reporting (Client-Side)

Report security events from your frontend:

```js
fetch('/api/incident/event', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    category: 'xss:attempt',
    level: 8,
    details: 'Script tag detected in comment field',
    metadata: {
      field: 'comment',
      input: '<script>...',
      page: '/posts/new'
    }
  })
});
```

Events with `level >= 7` automatically create incidents. See [Reporting Events](../logging/reporting_events.md) for full field reference.

## Event Sources

These are the built-in detection sources. Your app can add custom events via the reporting API.

| Source | Category | Level | What it detects |
|--------|----------|-------|-----------------|
| Failed login (unknown user) | `login:unknown` | 8 | Credential stuffing |
| Failed login (wrong password) | `invalid_password` | 1 | Brute force |
| Invalid/expired token | `invalid_token`, `expired_token` | 8 | Token abuse |
| Rate limit hit | `rate_limit:{endpoint}` | 5 | API abuse |
| Bouncer block | `security:bouncer:block` | 8 | Bot detected |
| MFA failures | `totp:login_failed` | 1 | MFA bypass attempt |
| OSSEC alerts | `ossec` | varies | OS-level threats |
| System health | `system:health:{type}` | 5-10 | Infrastructure issues |

## Configuring RuleSets

RuleSets are the core of the rule engine. Each RuleSet watches a specific event category, groups related events into incidents, and fires a handler when enough events accumulate. You create and manage them via the REST API — no code deployment required.

### Endpoints

| Method | Path | Description | Permission |
|--------|------|-------------|------------|
| `GET` | `/api/incident/event/ruleset` | List all rulesets | `view_security` |
| `GET` | `/api/incident/event/ruleset/<id>` | Get a single ruleset | `view_security` |
| `POST` | `/api/incident/event/ruleset` | Create a ruleset | `manage_security` |
| `POST` | `/api/incident/event/ruleset/<id>` | Update a ruleset | `manage_security` |
| `DELETE` | `/api/incident/event/ruleset/<id>` | Delete a ruleset | `manage_security` |

Rules (the conditions within a ruleset) are managed at `/api/incident/event/ruleset/rule`.

### RuleSet Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable label |
| `category` | string | Event category to match. Use `*` as a catch-all fallback. |
| `priority` | int | Evaluation order — lower number = higher priority. First match wins. |
| `match_by` | int | `0` = ALL rules must match, `1` = ANY rule can match |
| `bundle_by` | int | How to group events into one incident (see below) |
| `bundle_minutes` | int | Time window for bundling. `0` = each event gets its own incident, `null` = bundle forever, `>0` = bundle within N minutes |
| `handler` | string | Handler chain to fire (see [Handlers](#incident-handlers)) |
| `trigger_count` | int | Hold incident at `pending` until this many events accumulate. `null` = fire on first event. |
| `trigger_window` | int | Only count events within this many minutes when evaluating `trigger_count`. `null` = count all events on the incident. |
| `retrigger_every` | int | Re-fire the handler every N additional events while the incident stays active. `null` = fire once only. |

### bundle_by Values

| Value | Name | Group events by | When to use |
|-------|------|-----------------|-------------|
| `0` | NONE | — (no grouping) | Each event creates its own incident. Use for one-shot alerts like health checks. |
| `1` | HOSTNAME | Same server | Server-level problems (disk full, CPU spike) that should be tracked per machine. |
| `2` | MODEL_NAME | Same model type | Permission denials across all instances of a model (e.g. all `Order` edits). |
| `3` | MODEL_NAME_AND_ID | Same model instance | Activity on a specific record (e.g. repeated edits to one user account). |
| `4` | SOURCE_IP | Same source IP | Attack patterns from one IP — brute force, scanning, credential stuffing. |
| `5` | HOSTNAME_AND_MODEL_NAME | Same server + model | Server-specific model errors. |
| `6` | HOSTNAME_AND_MODEL_NAME_AND_ID | Same server + model instance | Very specific server-scoped activity. |
| `7` | SOURCE_IP_AND_MODEL_NAME | Same IP + model | IP attacking a specific model type. |
| `8` | SOURCE_IP_AND_MODEL_NAME_AND_ID | Same IP + model instance | IP targeting a specific record. |
| `9` | SOURCE_IP_AND_HOSTNAME | Same IP + server | IP causing problems on a specific instance (distributed attack targeting one node). |

For most security rules, `bundle_by=4` (SOURCE_IP) is the right choice.

### trigger_count + trigger_window: Suppress Until Threshold

Without `trigger_count`, the handler fires on the very first event. That's right for critical one-off alerts, but creates noise for gradual attacks. Use `trigger_count` to suppress until you're sure something is real:

**How it works:**
1. Events arrive and get bundled into one incident (which sits at `pending`)
2. Once the incident accumulates `trigger_count` events, it transitions to `new` and the handler fires
3. The `pending` incident is invisible in the main admin queue — it only surfaces when it becomes `new`

`trigger_window` scopes the count to recent events only. Events on the incident older than `trigger_window` minutes don't count toward the threshold.

**Example — block after 10 failed logins in 5 minutes:**

```
POST /api/incident/event/ruleset
{
  "name": "Brute Force Detection",
  "category": "auth:failed",
  "priority": 5,
  "match_by": 0,
  "bundle_by": 4,
  "bundle_minutes": 30,
  "trigger_count": 10,
  "trigger_window": 5,
  "handler": "block://?ttl=3600"
}
```

Events 1–9 from the same IP sit quietly at `pending`. Event 10 trips the threshold → incident goes `new` → IP gets blocked fleet-wide.

### retrigger_every: Keep Alerting as Things Escalate

Sometimes you want the handler to fire again if the attack keeps going. `retrigger_every` re-fires the handler every N additional events after the initial trigger, as long as the incident is still active (`new`, `open`, or `investigating`).

**Example — ticket at 5 payment failures, then escalate every 10 more:**

```
POST /api/incident/event/ruleset
{
  "name": "Payment Failure Escalation",
  "category": "payment:declined",
  "priority": 10,
  "match_by": 0,
  "bundle_by": 4,
  "bundle_minutes": 60,
  "trigger_count": 5,
  "retrigger_every": 10,
  "handler": "ticket://?priority=7,email://perm@manage_security"
}
```

- 5 failures → ticket created + email sent (initial trigger)
- 15 failures → ticket + email again
- 25 failures → ticket + email again
- etc.

Re-triggers add a `handler_retriggered` history entry on the incident so you can see the escalation trail.

### Handler Chains

Multiple handlers are chained with commas:

```
block://?ttl=3600,ticket://?priority=9,email://perm@manage_security
```

**Block handler parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `ttl` | `600` | Seconds until auto-unblock. `0` = permanent. |
| `reason` | `auto:ruleset` | Reason recorded on the GeoLocatedIP block record |

**Ticket handler parameters:**

| Param | Description |
|-------|-------------|
| `priority` | Ticket priority (1–15, defaults to event level) |
| `status` | Initial status (`open`, `new`) |
| `title` | Override ticket title |
| `category` | Ticket category (default: `incident`) |

**Notification targets** (for `email://`, `sms://`, `notify://`):

| Syntax | Who gets notified |
|--------|-------------------|
| `perm@manage_security` | All users with the `manage_security` permission |
| `protected@alerts` | Users who opted into `metadata.protected.alerts` |
| `admin` | Specific user by username |

### Common Patterns

| Scenario | bundle_by | trigger_count | trigger_window | retrigger_every | handler |
|----------|-----------|---------------|----------------|-----------------|---------|
| Block after 10 SSH failures in 5 min | SOURCE_IP (4) | 10 | 5 | — | `block://?ttl=3600` |
| Block on first credential-stuffing attempt | SOURCE_IP (4) | — | — | — | `block://?ttl=1800` |
| Ticket after 3 payment declines | SOURCE_IP (4) | 3 | 60 | — | `ticket://?priority=7` |
| Notify on first health alert | HOSTNAME (1) | — | — | — | `notify://perm@manage_security` |
| Email at 5 auth failures, re-alert every 10 | SOURCE_IP (4) | 5 | 30 | 10 | `email://perm@manage_security` |
| Block bot + create ticket for review | SOURCE_IP (4) | — | — | — | `block://?ttl=3600,ticket://?priority=8` |
| Silent audit (no handler) | MODEL_NAME_AND_ID (3) | — | — | — | — |

### Rule Conditions

Each ruleset can have zero or more `Rule` records that filter which events it applies to. Create them at `/api/incident/event/ruleset/rule`:

```
POST /api/incident/event/ruleset/rule
{
  "parent": 42,
  "name": "Level >= 7",
  "field_name": "level",
  "comparator": ">=",
  "value": "7",
  "value_type": "int"
}
```

**Comparators**: `==`, `>`, `>=`, `<`, `<=`, `contains`, `regex`

**value_type**: `str`, `int`, `float`, `bool`

`field_name` can be any field on the event (`level`, `source_ip`, `hostname`, `country_code`) or any key in `event.metadata` (e.g. `risk_score`, `http_url`, `rule_id` for OSSEC).

A ruleset with no rules never matches. Always add at least one rule.

## Incident Handlers

Rules can fire these handlers when incidents are created:

| Handler | Syntax | What it does |
|---------|--------|-------------|
| Block IP | `block://?ttl=3600` | Fleet-wide IP block |
| Email | `email://perm@manage_security` | Email verified users |
| SMS | `sms://perm@manage_security` | SMS verified users (critical only) |
| Notify | `notify://perm@manage_security` | In-app + push notification |
| Ticket | `ticket://?priority=8` | Create ticket for human review |
| Job | `job://module.function` | Run custom async job |
| LLM | `llm://` | Autonomous LLM triage agent |

Handlers resolve notification targets via:
- `perm@name` — all users with that permission
- `protected@key` — users who opted in via `metadata.protected.{key}`
- `username` — specific user by username

## LLM Agent

When configured (`LLM_HANDLER_API_KEY` setting), the LLM agent acts as an automated first responder:

1. Triages every `status=new` incident
2. Queries context (events, IP history, related incidents, metrics)
3. Takes action: ignore noise, resolve real threats, block IPs, create tickets for humans
4. Learns over time by creating new rules and storing pattern knowledge
5. Communicates with humans through ticket notes

High-level events that don't match any rule are automatically sent to the LLM if configured. This ensures nothing falls through the cracks.

The LLM creates rules in a **disabled** state and opens a ticket for human approval. Respond to the ticket to approve, modify, or reject the proposed rule.

## Dashboard Chart Ideas

**Overview cards:**
- Total incidents today (use `incidents` metric)
- Unhandled count (`GET /api/incident/incident?status=new` → `count`)
- Active blocks (`GET /api/system/geoip?is_blocked=true` → `count`)
- Events/hour trend (use `incident_events` metric)

**Time-series charts:**
- Incident volume over time (`incidents` metric, hourly granularity)
- Block rate (`firewall:blocks` metric)
- Events by country (use `incident_events_by_country` category)
- Resolution rate (`incidents:resolved` vs `incidents` metrics)

**Tables:**
- Top source IPs by event count
- Recent firewall actions (logit with `kind=firewall:*`)
- Open tickets awaiting human response
- LLM-proposed rules pending approval

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `INCIDENT_LEVEL_THRESHOLD` | `7` | Min event level to auto-create incidents |
| `INCIDENT_EVENT_METRICS` | `True` | Enable incident/event metrics recording |
| `INCIDENT_DEDUP_WINDOW_SECONDS` | `60` | Dedup window for identical events (0=disabled) |
| `HEALTH_MONITORING_ENABLED` | `False` | Enable system health monitoring cron |
| `HEALTH_TCP_MAX` | `2000` | TCP connection threshold per node |
| `HEALTH_CPU_CRIT` | `90` | CPU % threshold |
| `HEALTH_MEM_CRIT` | `90` | Memory % threshold |
| `HEALTH_DISK_CRIT` | `85` | Disk % threshold |
| `OSSEC_SECRET` | `None` | Optional secret for OSSEC endpoints |
| `LLM_HANDLER_API_KEY` | `None` | Claude API key (enables LLM agent) |
| `LLM_HANDLER_MODEL` | `claude-sonnet-4-20250514` | Claude model for LLM agent |
| `INCIDENT_EMAIL_FROM` | `None` | SES mailbox for incident emails |
| `ADMIN_PORTAL_URL` | `None` | URL for deep links in notifications |

## IPSet Bulk Blocking

IPSets are the primary mechanism for blocking entire countries, datacenters, or large abuse lists at the kernel level. Each IPSet record maps to a Linux `ipset` hash:net — lookups are O(1) regardless of set size, making it practical to block tens of thousands of CIDRs without performance impact.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/incident/ipset` | List all IPSets |
| `GET` | `/api/incident/ipset/<id>` | Get a single IPSet |
| `POST` | `/api/incident/ipset` | Create a new IPSet |
| `POST` | `/api/incident/ipset/<id>` | Update an IPSet |
| `DELETE` | `/api/incident/ipset/<id>` | Delete an IPSet |

### Permissions

| Permission | Access |
|------------|--------|
| `view_security` or `security` | Read (list, detail) |
| `manage_security` or `security` | Create, update, actions |
| `manage_security` (required) | Delete |

### Field Reference

| Field | Type | Writable | Description |
|-------|------|----------|-------------|
| `id` | int | No | Primary key |
| `name` | string | Yes | Unique ipset name, e.g. `country_cn`, `abuse_ips`. Used as the kernel ipset identifier — no spaces or special characters. |
| `kind` | string | Yes | Type: `country`, `datacenter`, `abuse`, `custom` |
| `description` | string | Yes | Human-readable label |
| `source` | string | Yes | Data source: `ipdeny`, `abuseipdb`, `manual` |
| `source_url` | string | Yes | URL to fetch CIDR data from (auto-populated for ipdeny country sets) |
| `source_key` | string | Yes (write-only) | API key or identifier for the source. For AbuseIPDB this is the API key. Never returned in any response graph. |
| `is_enabled` | bool | Yes | Whether this set is active in iptables on all instances |
| `cidr_count` | int | No | Number of CIDRs currently loaded (auto-updated on sync) |
| `last_synced` | datetime | No | Timestamp of last successful fleet sync |
| `sync_error` | string | No | Last error message if a sync or refresh failed, null on success |
| `created` | datetime | No | Creation timestamp |
| `modified` | datetime | No | Last modification timestamp |

> **Note**: The `data` field (raw CIDR list) is excluded from the default response graph. Use `?graph=detailed` to include it.

### Graphs

| Graph | What's included |
|-------|----------------|
| `default` (no parameter) | All fields except `data` and `source_key` — suitable for list and summary views |
| `detailed` (`?graph=detailed`) | All fields including `data` (the full CIDR list, one per line); `source_key` is always excluded |

### Actions (POST_SAVE_ACTIONS)

Trigger actions by POSTing `{"action": "<name>"}` to `/api/incident/ipset/<id>`:

| Action | Description |
|--------|-------------|
| `sync` | Broadcast the current CIDR data to all instances — loads into kernel ipset and adds iptables DROP rule |
| `enable` | Set `is_enabled=true` and sync fleet-wide |
| `disable` | Set `is_enabled=false` and remove the ipset + iptables rule from all instances |
| `refresh_source` | Re-fetch CIDRs from `source_url` or the AbuseIPDB API, update `data` and `cidr_count`, then sync fleet-wide |

**Example — sync after manual CIDR edit:**

```
POST /api/incident/ipset/3
{"action": "sync"}
```

**Example — disable a country block:**

```
POST /api/incident/ipset/3
{"action": "disable"}
```

### Workflow: Block a Country

Block all traffic from China (`cn`):

```
POST /api/incident/ipset
{
  "name": "country_cn",
  "kind": "country",
  "description": "Block country: CN",
  "source": "ipdeny",
  "source_url": "https://www.ipdeny.com/ipblocks/data/countries/cn.zone",
  "is_enabled": true
}
```

Then fetch the latest CIDRs and load them onto all instances:

```
POST /api/incident/ipset/<id>
{"action": "refresh_source"}
```

`refresh_source` fetches the zone file, stores the CIDRs in `data`, and immediately syncs to all instances. A weekly cron also runs `refresh_source` automatically on all enabled IPSets.

Common country codes: `cn` (China), `ru` (Russia), `ir` (Iran), `kp` (North Korea).

### Workflow: Block Abuse IPs via AbuseIPDB

Block IPs with 100% confidence score from [AbuseIPDB](https://www.abuseipdb.com/):

```
POST /api/incident/ipset
{
  "name": "abuse_ips",
  "kind": "abuse",
  "description": "AbuseIPDB blacklist (confidence 100%)",
  "source": "abuseipdb",
  "source_key": "<your-abuseipdb-api-key>",
  "is_enabled": true
}
```

Then load the current blacklist:

```
POST /api/incident/ipset/<id>
{"action": "refresh_source"}
```

This fetches up to 10,000 IPv4 addresses with confidence ≥ 100% and syncs them fleet-wide. The weekly cron refreshes this automatically.

### Workflow: Manual CIDR List

For custom ranges (e.g., a specific datacenter or known attacker range):

```
POST /api/incident/ipset
{
  "name": "custom_block",
  "kind": "custom",
  "description": "Blocked datacenter ranges",
  "source": "manual",
  "is_enabled": true
}
```

Then update with `graph=detailed` to set the CIDR data:

```
POST /api/incident/ipset/<id>?graph=detailed
{
  "data": "192.0.2.0/24\n198.51.100.0/24\n203.0.113.0/24"
}
```

Then sync to load onto all instances:

```
POST /api/incident/ipset/<id>
{"action": "sync"}
```

The `data` field is plain text, one CIDR per line. Lines starting with `#` are treated as comments and ignored.

### Listing and Filtering

```
GET /api/incident/ipset
GET /api/incident/ipset?kind=country
GET /api/incident/ipset?is_enabled=true
GET /api/incident/ipset?search=abuse
```

Standard sort and pagination apply:

```
GET /api/incident/ipset?sort=-cidr_count&size=20
```
