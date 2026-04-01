# Security System — Architecture & Configuration Guide

The security system is a multi-layered defense pipeline that detects, correlates, triages, and enforces security policy across the platform. This document covers the full system end-to-end.

```
                           ┌─────────────────────────┐
                           │     Event Sources        │
                           │  OSSEC · Bouncer · Auth  │
                           │  Health · App Code       │
                           └────────────┬─────────────┘
                                        │ report_event()
                                        ▼
                           ┌─────────────────────────┐
                           │    Event (raw signal)    │
                           │  category · level · IP   │
                           │  metadata · GeoIP enrich │
                           └────────────┬─────────────┘
                                        │
                                        ▼
                           ┌─────────────────────────┐
                           │    Rule Engine           │
                           │  RuleSet.check_by_cat()  │
                           │  field match · bundling  │
                           └────────────┬─────────────┘
                                        │ match found
                                        ▼
                           ┌─────────────────────────┐
                           │    Incident              │
                           │  bundled events · status │
                           │  priority · history      │
                           └────────────┬─────────────┘
                                        │
                                        ▼
                           ┌─────────────────────────┐
                           │    Handler Chain         │
                           │  block · email · notify  │
                           │  ticket · llm · sms · job│
                           └────────────┬─────────────┘
                                        │
                            ┌───────────┼───────────┐
                            ▼           ▼           ▼
                        ┌───────┐ ┌──────────┐ ┌─────────┐
                        │ Block │ │  Ticket  │ │  Alert  │
                        │ IP    │ │  + LLM   │ │  Email  │
                        │ Fleet │ │  Triage  │ │  SMS    │
                        └───────┘ └──────────┘ └─────────┘
```

## 1. Events

Events are the raw signals that enter the security pipeline. Every security-relevant action produces an Event.

### Reporting an Event

```python
from mojo.apps.incident import reporter

reporter.report_event(
    details="Failed login for unknown user 'admin123'",
    title="Unknown username login attempt",
    category="login:unknown",
    level=8,
    request=request,          # optional — auto-extracts IP, UA, path, user
    scope="global",           # "global" or "group"
    source_ip="1.2.3.4",     # auto-extracted from request if omitted
    hostname="web-01",        # auto-set to current hostname if omitted
    model_name="User",        # optional — links event to a model
    model_id=42,              # optional — specific instance
)
```

### Event Fields

| Field | Type | Description |
|-------|------|-------------|
| `details` | str | Human-readable description of what happened |
| `title` | str | Short title (defaults to first 80 chars of details) |
| `category` | str | Dot-separated category for rule matching (e.g., `login:unknown`, `security:bouncer:block`) |
| `level` | int | Severity 1-15 (1=debug, 5=info, 8=warning, 10=high, 12=critical, 15=emergency) |
| `scope` | str | `"global"` or `"group"` |
| `source_ip` | str | Origin IP address |
| `hostname` | str | Server hostname that generated the event |
| `metadata` | dict | Arbitrary key-value pairs for rule matching |

### GeoIP Enrichment

When an event has a `source_ip`, the system automatically looks up or creates a `GeoLocatedIP` record. This enriches the event with country, city, ISP, threat indicators (Tor, VPN, proxy, known attacker), and block status.

### Deduplication

Events are deduplicated within a configurable window (default 60 seconds). If an event with the same `category`, `level`, and optionally `source_ip`/`hostname` was created within the window, the existing event's `metadata.dedup_count` is incremented instead of creating a duplicate.

**Setting:** `INCIDENT_DEDUP_WINDOW_SECONDS` (default: `60`)

### Event Categories

Categories are hierarchical strings using `:` as separator. The rule engine matches on exact category.

| Category Pattern | Source | Description |
|-----------------|--------|-------------|
| `ossec:alert` | OSSEC | IDS/HIDS alerts from OSSEC |
| `login:unknown` | Auth | Login attempt with unknown username |
| `login:failed` | Auth | Failed password for known user |
| `security:bouncer:block` | Bouncer | Bot blocked by risk scoring |
| `security:bouncer:monitor` | Bouncer | Suspicious bot placed on watch |
| `security:bouncer:honeypot` | Bouncer | POST to decoy/honeypot page |
| `security:bouncer:campaign` | Bouncer | Coordinated bot campaign detected |
| `security:bouncer:token_invalid` | Bouncer | Invalid/replayed/expired token |
| `system:health:runner` | Health | Dead job runner detected |
| `system:health:scheduler` | Health | Missing scheduler process |
| `system:health:tcp` | Health | High TCP connection count |
| `system:health:cpu` | Health | CPU threshold exceeded |
| `system:health:memory` | Health | Memory threshold exceeded |
| `system:health:disk` | Health | Disk threshold exceeded |
| `api_error` | App | Application error (default category) |

### Event Levels

| Level | Meaning | Typical Use |
|-------|---------|-------------|
| 1-4 | Debug/Info | Logged but rarely acted on |
| 5-6 | Notice | Normal operational events |
| 7-8 | Warning | Suspicious activity, soft threshold |
| 9-10 | High | Confirmed malicious or system failure |
| 11-12 | Critical | Active attack or critical outage |
| 13-15 | Emergency | Requires immediate response |

Events at or above `INCIDENT_LEVEL_THRESHOLD` (default: 7) automatically create an incident even without a matching rule.

## 2. Rule Engine

Rules match incoming events and determine what happens next. The rule engine is the brain of the security pipeline.

### RuleSet

A RuleSet groups one or more Rules together with a handler chain. When an event arrives, the engine finds matching RuleSets by category and evaluates their rules.

```python
from mojo.apps.incident.models import RuleSet

ruleset = RuleSet.objects.create(
    name="SSH Brute Force",
    category="ossec:alert",
    priority=5,                    # lower = checked first
    handler="block://?ttl=3600",   # what to do on match
    bundle_by=RuleSet.BundleBy.SOURCE_IP,
    bundle_minutes=30,             # group events into one incident
    match_by=RuleSet.MatchBy.ANY,  # ANY rule matches = RuleSet matches
)
```

### Rule (Field Matcher)

Each Rule checks one field on the event. Multiple rules in a RuleSet combine with AND (match_by=ALL) or OR (match_by=ANY).

```python
from mojo.apps.incident.models import Rule

Rule.objects.create(
    parent=ruleset,
    field_name="rule_id",          # check event.metadata['rule_id'] or event.rule_id
    comparator="==",
    value="5758",
    value_type="str",
    is_required=1,
)
```

### Comparators

| Comparator | Description | Example |
|-----------|-------------|---------|
| `==`, `eq` | Equality | `field_name="level"`, `value="10"` |
| `>` | Greater than | `field_name="risk_score"`, `value="80"` |
| `>=` | Greater or equal | `field_name="level"`, `value="8"` |
| `<` | Less than | |
| `<=` | Less or equal | |
| `contains` | Substring match | `field_name="http_path"`, `value=".php"` |
| `regex` | Regex match (case-insensitive) | `field_name="http_path"`, `value="\\.(php|asp|env)"` |

### Value Types

The `value_type` field controls how both the event field and the comparison value are cast before comparison:

| Type | Cast | Notes |
|------|------|-------|
| `str` | `str()` | Default. String comparison. |
| `int` | `int()` | Numeric comparison for levels, scores, counts |
| `float` | `float()` | Decimal comparison |
| `bool` | `bool()` | Boolean comparison |

If casting fails, the rule does not match (returns False).

### Field Resolution

When checking a rule, the engine looks for the field in this order:
1. `event.metadata.get(field_name)` — custom metadata fields
2. `getattr(event, field_name)` — model fields (level, category, source_ip, etc.)

This means you can match on any metadata key you pass to `report_event()`.

### Matching Flow

```python
# How events are matched to rules:
ruleset = RuleSet.check_by_category(category="ossec:alert", event=event)
# 1. Find all RuleSets where category matches
# 2. Order by priority (ascending — lower = first)
# 3. Skip disabled rulesets (metadata.disabled=True)
# 4. For each: evaluate rules (ALL must match, or ANY must match)
# 5. Return first matching RuleSet, or None
```

### Bundling

When a RuleSet matches, the engine decides whether to create a new incident or bundle into an existing one.

**Bundle By** controls the grouping key:

| Value | Constant | Groups events by |
|-------|----------|-----------------|
| 0 | `NONE` | Never bundles — each event = new incident |
| 1 | `HOSTNAME` | Same server hostname |
| 4 | `SOURCE_IP` | Same source IP (most common for security) |
| 3 | `MODEL_NAME_AND_ID` | Same model type + ID |
| 9 | `SOURCE_IP_AND_HOSTNAME` | Same IP + same server |

**Bundle Minutes** controls the time window. If a matching incident exists within `bundle_minutes` of the current event, the event is added to that incident instead of creating a new one.

Example: `bundle_by=SOURCE_IP, bundle_minutes=30` means "group all events from the same IP into one incident, as long as they arrive within 30 minutes of each other."

## 3. Handlers

Handlers define what happens when a rule matches. A RuleSet's `handler` field is a comma-separated chain of handler URLs. All handlers in the chain execute for each match.

```python
# Single handler
handler = "block://?ttl=3600"

# Handler chain — block IP, create ticket, and notify
handler = "block://?ttl=3600,ticket://?priority=9,notify://perm@manage_security"
```

### Handler Types

#### `block://?ttl=<seconds>`

Blocks the event's `source_ip` across the entire fleet. Creates a `GeoLocatedIP` block record, broadcasts an iptables block to all servers, records the action in `IncidentHistory`, and auto-resolves the incident.

`geo.block()` is idempotent — if the IP is already actively blocked the call returns `True` without re-broadcasting or incrementing `block_count`.

| Param | Default | Description |
|-------|---------|-------------|
| `ttl` | `3600` | Block duration in seconds (0 = permanent) |
| `reason` | `auto:ruleset` | Base reason string. Incident and event IDs are appended automatically: `auto:ruleset:incident:42:event:87` |

```
block://?ttl=600      # Block for 10 minutes
block://?ttl=86400    # Block for 24 hours
block://?ttl=0        # Permanent block
```

**Important:** Never use `block://` for health events — health issues are infrastructure problems, not attacks.

#### `ticket://?status=<status>&priority=<n>`

Creates a Ticket linked to the incident for human review.

| Param | Default | Description |
|-------|---------|-------------|
| `status` | `open` | Initial ticket status |
| `priority` | `5` | Priority 1-10 (10 = highest) |
| `category` | | Optional ticket category |
| `assignee` | | Optional username to assign to |

```
ticket://?priority=9&status=open
ticket://?priority=5&assignee=oncall
```

#### `notify://<targets>`

Sends in-app notification + push notification to resolved targets.

```
notify://perm@manage_security           # All users with manage_security perm
notify://alice,bob                       # Specific users
notify://perm@manage_security,alice      # Mixed
```

#### `email://<targets>`

Sends email alert to resolved targets. Only sends to users with verified emails.

```
email://perm@manage_security
email://alice,oncall
```

**Requires:** `INCIDENT_EMAIL_FROM` setting.

#### `sms://<targets>`

Sends SMS alert to resolved targets. Only sends to users with verified phone numbers.

```
sms://perm@manage_security
sms://oncall
```

#### `llm://`

Invokes the LLM security agent for autonomous triage. No parameters — the agent receives the event and incident context and decides what to do.

```
llm://
```

**Requires:** `LLM_HANDLER_API_KEY` setting and the `anthropic` Python package (`anthropic>=0.52.0`).

#### `job://<module.function>?<params>`

Dispatches a custom async job.

```
job://myapp.jobs.analyze_traffic?window=3600
```

### Target Resolution

Targets in `notify://`, `email://`, and `sms://` handlers support three formats:

| Format | Resolves to |
|--------|-------------|
| `perm@permission_name` | All active users with that permission |
| `protected@metadata_key` | All active users with `metadata.protected.{key} = True` |
| `username` | Single user by username |

Targets are comma-separated and deduplicated. For `email://`, only users with `is_email_verified=True` receive mail. For `sms://`, only users with `is_phone_verified=True` receive texts.

### Handler Execution

Handlers execute asynchronously via the job queue. When a rule matches:
1. An incident is created (or existing incident is found via bundling)
2. Each handler in the chain is published as a separate async job
3. Handler execution is recorded in `IncidentHistory`
4. Failures are logged but do not block other handlers in the chain

## 4. Incidents

An Incident is a correlated group of events that represents a single security issue.

### Lifecycle

```
     new ──→ investigating ──→ resolved
      │           │                │
      │           ▼                │
      └────→  ignored  ◄──────────┘
```

| Status | Meaning |
|--------|---------|
| `new` | Just created, no action taken yet |
| `investigating` | Human or LLM is actively reviewing |
| `resolved` | Issue addressed, no further action needed |
| `ignored` | False positive or acceptable risk |

### Incident Fields

| Field | Description |
|-------|-------------|
| `status` | Current lifecycle status |
| `priority` | 1-10 (derived from highest event level) |
| `category` | Copied from triggering event category |
| `scope` | `"global"` or `"group"` |
| `details` | Description from triggering event |
| `source_ip` | IP from triggering event |
| `hostname` | Server from triggering event |
| `rule` | FK to the RuleSet that matched (nullable) |
| `event_count` | Number of bundled events |

### Incident History

Every state change is recorded in `IncidentHistory` — status changes, handler executions, LLM actions, manual notes. This provides a full audit trail.

### Merging

Duplicate incidents can be merged via the `merge` POST_SAVE_ACTION. The target incident absorbs all events and history from the source.

### LLM Analysis (POST_SAVE_ACTION)

The `analyze` action triggers deep LLM analysis on an incident — finding related incidents, proposing merge candidates, and creating a new (disabled) RuleSet for human approval. It runs asynchronously via the job queue.

```python
# How the action is triggered via REST:
# POST /api/incident/incident/<id>  {"analyze": 1}

# Programmatically:
incident.on_action_analyze(None)
```

**Guard behavior:**
- Returns `{"status": False, "error": "..."}` if `LLM_HANDLER_API_KEY` is not configured.
- Returns `{"status": False, "error": "Analysis already in progress"}` if `metadata.analysis_in_progress` is `True`.
- Sets `metadata.analysis_in_progress = True` before dispatching the job; clears it when the job finishes (success or failure).

**Result storage:** When analysis completes, the agent's final summary is stored in `incident.metadata["llm_analysis"]["summary"]` (truncated to 3000 characters) and a `handler:llm` entry is added to `IncidentHistory`.

## 5. Tickets

Tickets are actionable work items created by `ticket://` handlers or the LLM agent.

### Structure

| Field | Description |
|-------|-------------|
| `title` | Short description |
| `note` | Detailed description or analysis |
| `status` | `open`, `in_progress`, `resolved`, `closed` |
| `priority` | 1-10 |
| `category` | Optional grouping |
| `incident` | FK to related incident |
| `assignee` | FK to assigned user |
| `llm_linked` | Boolean — if True, human replies trigger LLM re-invocation |

### Ticket Notes

Notes are threaded comments on a ticket. When a ticket is `llm_linked`, adding a note (that doesn't start with `[LLM Agent]`) triggers the LLM agent to review and respond.

## 6. LLM Security Agent

The LLM agent provides autonomous security triage. When invoked via the `llm://` handler, it investigates the event, takes action, and communicates findings via tickets.

### How It Works

1. `llm://` handler publishes an async job with event_id, incident_id, ruleset_id
2. Agent receives the event context + any custom `agent_prompt` from the RuleSet
3. Agent runs an investigation loop (up to 15 tool calls)
4. Agent takes action (block IPs, create tickets, update incidents, send alerts)
5. Agent can persist learnings to `RuleSet.metadata.agent_memory` for future invocations

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `LLM_HANDLER_API_KEY` | None | Anthropic API key. **Required** to enable LLM handlers. |
| `LLM_HANDLER_MODEL` | `claude-sonnet-4-20250514` | Model to use for triage |

If `LLM_HANDLER_API_KEY` is not set, `llm://` handlers silently skip.

Both settings are read at invocation time (not at startup), so changes take effect on the next LLM job without a server restart.

**Dependency:** The LLM agent requires the `anthropic` Python package (`anthropic>=0.52.0`), which is included as a framework dependency.

### Available Tools

The standard triage agent (`execute_llm_handler`) has 12 tools. The analysis agent (`execute_llm_analysis`) has all 14 — the 12 base tools plus 2 analysis-only tools.

**Investigation:**

| Tool | Description | Triage | Analysis |
|------|-------------|--------|----------|
| `query_events` | Search recent events by category, IP, hostname, time window | Yes | Yes |
| `query_event_counts` | Aggregate event counts grouped by category | Yes | Yes |
| `query_ip_history` | Look up GeoLocatedIP record — threat level, block history, country | Yes | Yes |
| `query_related_incidents` | Find other incidents from same IP or category | Yes | Yes |
| `query_incident_events` | List all events bundled into an incident | Yes | Yes |
| `query_open_incidents` | Query open/new/investigating incidents, filtered by category | No | Yes |

**Action:**

| Tool | Description | Triage | Analysis |
|------|-------------|--------|----------|
| `update_incident` | Change status to investigating/resolved/ignored + add note | Yes | Yes |
| `block_ip` | Block IP fleet-wide with TTL and reason | Yes | Yes |
| `create_ticket` | Create ticket for human review with priority | Yes | Yes |
| `add_note` | Add investigation note to incident history | Yes | Yes |
| `send_alert` | Send email/SMS/notify to specific targets | Yes | Yes |
| `merge_incidents` | Merge related incidents into a target incident | No | Yes |

**Configuration:**

| Tool | Description | Triage | Analysis |
|------|-------------|--------|----------|
| `create_rule` | Create a new RuleSet (created disabled, requires human approval) | Yes | Yes |
| `update_rule_memory` | Persist learnings to RuleSet metadata for future invocations | Yes | Yes |

**Analysis-only tool details:**

`merge_incidents` — Takes `target_incident_id` (int) and `incident_ids` (list of ints). Moves all events from the source incidents into the target and deletes the sources. Only merges incidents with the same `category`; already-resolved or ignored incidents are excluded automatically.

`query_open_incidents` — Takes optional `category` (string) and `limit` (int, max 100, default 50). Returns incidents in `new`, `open`, or `investigating` status with event counts. Used to identify merge candidates across the incident backlog.

### Agent Memory

Each RuleSet can have an `agent_memory` field in its metadata. The agent reads this at the start of each invocation and can update it with learnings. This provides continuity across invocations — the agent remembers patterns it has seen before for this rule type.

### Custom Agent Prompts

Add an `agent_prompt` field to a RuleSet's metadata to give the agent rule-specific instructions:

```python
ruleset.metadata = {
    "agent_prompt": "This rule fires on SSH brute force. Check if the IP has been seen before. If more than 3 incidents from this IP in 24h, block for 24h instead of 1h.",
    "agent_memory": ""  # Agent will populate this
}
ruleset.save()
```

### Ticket Re-Invocation

When a ticket is `llm_linked` and a human adds a note, the full conversation history is sent back to the agent. This allows humans to:
- Ask the agent to investigate further
- Approve actions the agent proposed
- Give the agent new instructions

The agent's response is posted as a new `[LLM Agent]` note on the ticket.

### Deep Analysis Mode (`execute_llm_analysis`)

In addition to the real-time triage agent, there is a separate **analysis job** designed for manual on-demand investigation of an incident. It is triggered by the `analyze` POST_SAVE_ACTION on Incident (see section 4).

**Entry point:** `mojo.apps.incident.handlers.llm_agent.execute_llm_analysis`

**Job payload:** `{"incident_id": <int>}`

**How it differs from triage:**

| Aspect | `execute_llm_handler` (triage) | `execute_llm_analysis` (analysis) |
|--------|-------------------------------|-----------------------------------|
| Trigger | Automatic — `llm://` handler on rule match | Manual — admin POST `{"analyze": 1}` |
| Prompt | `TRIAGE_PROMPT` — classify, triage, act fast | `ANALYSIS_PROMPT` — deep pattern analysis |
| Tools | 12 base tools | 14 tools (includes `merge_incidents`, `query_open_incidents`) |
| Pre-loaded context | Event + incident metadata | Full event list (up to 50) + related open incidents (up to 20) |
| Result | Ticket + history note | `incident.metadata["llm_analysis"]["summary"]` + history note |

**ANALYSIS_PROMPT workflow:** The agent is instructed to follow this sequence:
1. Set the incident to `investigating`
2. Review pre-loaded events and related open incidents
3. Use `query_open_incidents` to find all open incidents in the same category
4. Merge clearly related incidents using `merge_incidents`
5. Identify the pattern and check existing rules to avoid duplication
6. Create a new disabled RuleSet via `create_rule` with proper bundling
7. Resolve the merged incident with a note explaining the new rule
8. Summarize: how many merged, what rule was proposed, what pattern it covers

**Merge constraints enforced by `ANALYSIS_PROMPT`:**
- Only merge incidents with the same category
- Only merge if the pattern is clearly the same underlying cause
- Do not merge already-resolved or ignored incidents
- Always set `bundle_by` and `bundle_minutes` on any new rule to prevent future duplicates

**Context pre-loading:** Before the agent loop starts, `_build_analysis_message` fetches the 50 most recent events on the incident and up to 20 related open incidents in the same category. This avoids round-trip tool calls for information the agent almost always needs.

## 7. Enforcement — IP Blocking & Firewall

### Single IP Blocking

When a `block://` handler fires:
1. `GeoLocatedIP.block(ip, ttl, reason)` updates the database record — idempotent: skips re-blocking if the IP is already actively blocked
2. An async `broadcast_block_ip` broadcast is sent to all servers in the fleet
3. Each server runs `iptables -I INPUT -s {ip} -j DROP`
4. If `blocked_until` is set, the sweep cron auto-unblocks when TTL expires
5. The handler records the block action in `IncidentHistory` and auto-resolves the incident

### Unblocking

Automatic: The `sweep_expired_blocks` cron runs every minute, finds IPs where `blocked_until < now()`, updates the database, and broadcasts `broadcast_unblock_ip` to the fleet.

Manual: Via GeoLocatedIP POST_SAVE_ACTIONS (`unblock`, `whitelist`).

### IPSets (Bulk Blocking)

IPSets block large sets of IPs using kernel-level ipset (much faster than individual iptables rules). Used for country blocks, abuse lists, etc.

```python
from mojo.apps.incident.models import IPSet

ipset = IPSet.objects.create(
    name="country_cn",
    description="Block all Chinese IPs",
    source_url="https://example.com/cn-cidrs.txt",
    is_enabled=True,
)
```

The `refresh_ipsets` cron fetches CIDRs from source URLs weekly and syncs to all servers.

### Firewall Requirements

- Runs on Linux with iptables/ipset installed
- Must run as `ec2-user` (has passwordless sudo for iptables/ipset)
- IPv4 and IPv6 supported
- All IPs validated against injection patterns before execution
- Commands timeout after 10 seconds

## 8. Bouncer Integration

Bouncer (the bot detection system) feeds events into the incident pipeline. See [Bouncer docs](../account/bouncer.md) for the full bouncer architecture.

### How Bouncer Events Flow into Incidents

```
Bouncer assess → risk_score ≥ block threshold
    │
    ├─→ report_event(category="security:bouncer:block", level=10)
    │       → Rule engine matches → block:// handler → IP blocked fleet-wide
    │
    ├─→ report_event(category="security:bouncer:honeypot", level=10)
    │       → When POST received on decoy page
    │
    └─→ Learner job runs → signatures learned
            → report_event(category="security:bouncer:campaign", level=10)
                → When 5+ blocks share same signal pattern
```

### Bouncer Metrics

8 metrics are recorded for monitoring:

| Metric | When |
|--------|------|
| `bouncer:assessments` | Every assessment after scoring |
| `bouncer:blocks` | Each blocked request |
| `bouncer:blocks:country:{CC}` | Blocked request by country code |
| `bouncer:monitors` | Each monitored (watch) request |
| `bouncer:pre_screen_blocks` | Signature cache hit (no scoring needed) |
| `bouncer:honeypot_catches` | POST to decoy page |
| `bouncer:signatures_learned` | New bot signature auto-created |
| `bouncer:campaigns` | Bot campaign detected |

### Signature Learning

After a high-confidence block (score >= `BOUNCER_LEARN_MIN_SCORE`, default 80), the learner background job analyzes the block and may create escalation signatures:

| Signature Type | Threshold | TTL | Description |
|---------------|-----------|-----|-------------|
| Subnet /24 | 5 blocks from same /24 | 1 day | Blocks entire subnet |
| User Agent | 5 blocks with same UA | 7 days | Blocks matching UA string |
| Fingerprint | 3 blocks with same fingerprint | 30 days | Blocks browser fingerprint |
| Signal Set (Campaign) | 5 blocks with same signal pattern | 30 days | Blocks coordinated attacks |

Signatures are cached in Redis for pre-screen checks. When a request matches a cached signature, it is blocked immediately without running full scoring.

## 9. OSSEC Integration

OSSEC (IDS/HIDS) sends alerts via webhook to `/api/incident/ossec/alert` or `/api/incident/ossec/alert/batch`.

### Setup

1. Set `OSSEC_SECRET` in Django settings
2. Configure OSSEC to POST alerts to your server with the secret in the `Authorization` header
3. Default rules handle common OSSEC patterns (bot scanners, SSH brute force, web attacks)

### Alert Flow

```
OSSEC → POST /api/incident/ossec/alert
    → Validates secret
    → Normalizes alert fields (rule_id, level, description, source_ip)
    → report_event(category="ossec:alert", level=ossec_level, metadata={rule_id, ...})
    → Rule engine matches on rule_id field
```

## 10. Health Monitoring

The health monitoring system runs every 3 minutes (when enabled) and reports infrastructure events.

### What It Checks

| Check | Category | Level | Threshold |
|-------|----------|-------|-----------|
| Dead job runners | `system:health:runner` | 10 | Runner not responding |
| Missing scheduler | `system:health:scheduler` | 10 | No scheduler lock in Redis |
| TCP connections | `system:health:tcp` | 8 | > `HEALTH_TCP_MAX` (default 2000) |
| CPU usage | `system:health:cpu` | 8 | > `HEALTH_CPU_CRIT` (default 90%) |
| Memory usage | `system:health:memory` | 8 | > `HEALTH_MEM_CRIT` (default 90%) |
| Disk usage | `system:health:disk` | 8 | > `HEALTH_DISK_CRIT` (default 85%) |

### Enable Health Monitoring

```python
# In Django settings
HEALTH_MONITORING_ENABLED = True
```

Default health rules are auto-created on first health check run. They send notifications and create tickets — they never block IPs (health issues are not attacks).

## 11. Cronjobs & Background Jobs

### Scheduled Cronjobs

| Job | Schedule | What it does |
|-----|----------|--------------|
| `prune_events` | Daily 9:45 AM | Deletes events older than `INCIDENT_EVENT_PRUNE_DAYS` days with level < 6 |
| `sweep_expired_blocks` | Every minute | Unblocks IPs where `blocked_until` has passed |
| `refresh_ipsets` | Weekly (Sunday 3 AM) | Re-fetches IPSet source URLs and syncs CIDRs to fleet |
| `check_system_health` | Every 3 minutes | Checks runner health, system metrics (if `HEALTH_MONITORING_ENABLED`) |

### Async Jobs (Broadcast)

These jobs are dispatched to all servers in the fleet. Broadcast handlers receive a plain dict (not a `Job` instance) from the pub/sub system.

| Job | Trigger | Data |
|-----|---------|------|
| `broadcast_block_ip` | `block://` handler | `{"ips": ["1.2.3.4"], "ttl": 600}` |
| `broadcast_unblock_ip` | Sweep cron or manual | `{"ips": ["1.2.3.4"]}` |
| `broadcast_sync_ipset` | IPSet refresh | `{"name": "country_cn", "cidrs": [...]}` |
| `broadcast_remove_ipset` | IPSet disabled | `{"name": "country_cn"}` |

### Async Jobs (Single Server)

| Job | Trigger | What it does |
|-----|---------|--------------|
| `execute_handler` | Rule match | Parses handler URL, dispatches to handler class |
| `execute_llm_handler` | `llm://` handler | Runs LLM triage agent loop (receives `Job` instance) |
| `execute_llm_analysis` | `analyze` POST_SAVE_ACTION | Deep LLM analysis: merge candidates, pattern detection, rule proposal (receives `Job` instance) |
| `execute_llm_ticket_reply` | Ticket note added | Re-invokes LLM on ticket conversation (receives `Job` instance) |
| `learn_from_block` | Bouncer block | Runs signature learning analysis |

Single-server job functions follow the engine's calling convention: `func(job)` where `job` is a `Job` model instance with `job.payload` holding the data. Broadcast handlers use `func(data)` where `data` is a plain dict.

## 12. Configuration Reference

### Required Settings

| Setting | Description |
|---------|-------------|
| `INCIDENT_EMAIL_FROM` | Sender email for alert notifications (must match a configured Mailbox) |
| `ADMIN_PORTAL_URL` | URL to admin portal (used in email/notification links) |

### Optional Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `INCIDENT_LEVEL_THRESHOLD` | `7` | Min event level to auto-create incident without rule match |
| `INCIDENT_DEDUP_WINDOW_SECONDS` | `60` | Event dedup window in seconds |
| `INCIDENT_EVENT_PRUNE_DAYS` | `30` | Days to keep low-level events before pruning |
| `INCIDENT_EVENT_METRICS` | `False` | Enable metrics recording for events |
| `INCIDENT_METRICS_MIN_GRANULARITY` | `"hours"` | Metrics time granularity |

### LLM Agent Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `LLM_HANDLER_API_KEY` | None | Anthropic API key. Required for `llm://` handlers. |
| `LLM_HANDLER_MODEL` | `claude-sonnet-4-20250514` | Claude model for triage |

### OSSEC Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `OSSEC_SECRET` | None | Shared secret for OSSEC webhook auth |

### Health Monitoring Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `HEALTH_MONITORING_ENABLED` | `False` | Enable health check cron |
| `HEALTH_TCP_MAX` | `2000` | TCP connection alert threshold |
| `HEALTH_CPU_CRIT` | `90` | CPU % alert threshold |
| `HEALTH_MEM_CRIT` | `90` | Memory % alert threshold |
| `HEALTH_DISK_CRIT` | `85` | Disk % alert threshold |

### Bouncer Learning Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `BOUNCER_LEARN_ENABLED` | `True` | Enable signature learning after blocks |
| `BOUNCER_LEARN_MIN_SCORE` | `80` | Min risk score to trigger learning |
| `BOUNCER_LEARN_SUBNET_THRESHOLD` | `5` | Blocks from /24 before subnet signature |
| `BOUNCER_LEARN_SUBNET_TTL` | `86400` | Subnet signature TTL (1 day) |
| `BOUNCER_LEARN_UA_THRESHOLD` | `5` | Blocks with same UA before UA signature |
| `BOUNCER_LEARN_UA_TTL` | `604800` | UA signature TTL (7 days) |
| `BOUNCER_LEARN_FP_THRESHOLD` | `3` | Blocks with same fingerprint before FP signature |
| `BOUNCER_LEARN_CAMPAIGN_THRESHOLD` | `5` | Blocks with same signals before campaign detection |
| `BOUNCER_LEARN_SIGNAL_SET_TTL` | `2592000` | Campaign signature TTL (30 days) |

### Bouncer Scoring Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `BOUNCER_THRESHOLDS` | `{"block": 60, "monitor": 40}` | Global risk score thresholds |
| `BOUNCER_THRESHOLDS_OVERRIDES` | `{}` | Per-page-type threshold overrides |
| `BOUNCER_SCORE_WEIGHTS` | (see below) | Per-signal point values |
| `BOUNCER_CONCURRENT_MTAB_LIMIT` | `4` | Max concurrent tabs before penalty |

## 13. Default Rules Reference

These rules are auto-created by `RuleSet.ensure_default_rules()` and serve as the baseline security policy. They can be customized or disabled via the admin API.

### OSSEC Rules

| Rule | Category | Matches | Handler | Bundle |
|------|----------|---------|---------|--------|
| Bot/Scanner URL Patterns | `ossec:alert` | URL regex: `.php`, `.git`, `.asp`, `.env`, `cgi-bin`, `wp-content` | `block://?ttl=600` | SOURCE_IP |
| SSH Brute Force | `ossec:alert` | rule_id: 5758, 5712, 5720, 5551 | `block://?ttl=3600` | SOURCE_IP |
| Web Attack 31104 | `ossec:alert` | rule_id == 31104 | `block://?ttl=600` | SOURCE_IP |
| Critical Severity | `ossec:alert` | level >= 12 | `block://?ttl=3600` | SOURCE_IP, 60min |

### Auth Rules

| Rule | Category | Matches | Handler | Bundle |
|------|----------|---------|---------|--------|
| Credential Stuffing | `login:unknown` | level >= 8 | `block://?ttl=1800` | SOURCE_IP, 15min |
| Bouncer Token Abuse | `security:bouncer:token_invalid` | level >= 7 | `block://?ttl=1800` | SOURCE_IP, 30min |

### Bouncer Rules

| Rule | Category | Matches | Handler | Bundle |
|------|----------|---------|---------|--------|
| Honeypot Detection | `security:bouncer:honeypot` | level >= 9 | `block://?ttl=3600` | SOURCE_IP, 30min |
| Bot Campaign | `security:bouncer:campaign` | level >= 10 | `block://?ttl=86400,notify://perm@manage_security` | SOURCE_IP, 60min |
| High Confidence Bot | `security:bouncer:block` | risk_score >= 80 | `block://?ttl=3600` | SOURCE_IP, 30min |

### Health Rules

| Rule | Category | Matches | Handler | Bundle |
|------|----------|---------|---------|--------|
| Runner Down | `system:health:runner` | level >= 10 | `notify://perm@manage_security,ticket://?priority=9` | HOSTNAME, 30min |
| Scheduler Missing | `system:health:scheduler` | level >= 10 | `notify://perm@manage_security,ticket://?priority=9` | NONE, 60min |
| TCP Overload | `system:health:tcp` | level >= 8 | `notify://perm@manage_security` | HOSTNAME, 30min |

Health rules **never** use `block://` — infrastructure issues should not block IPs.

## Related Documentation

- [Bouncer Architecture](../account/bouncer.md) — bot detection, scoring, tokens, signatures
- [GeoIP System](../account/geoip.md) — IP geolocation, blocking, threat escalation
- [Permissions](../core/permissions.md) — `security` category permission for admin access
- [Web Developer: Security Dashboard](../../web_developer/security/README.md) — REST API reference for building security UIs
- [Web Developer: Incident API](../../web_developer/logging/incidents.md) — REST API for incidents, events, tickets
