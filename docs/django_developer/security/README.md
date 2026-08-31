# Security System — Architecture & Configuration Guide

The security system is a multi-layered defense pipeline that detects, correlates, triages, and enforces security policy across the platform. This document covers the full system end-to-end.

External LLM features use the mandatory provider-neutral
[LLM safety boundary](llm_safety.md): exact deployment policy, hard budgets,
durable ledger, credential-scoped breaker, emergency stop, and duplicate-safe
incident dispatch.

```
                           ┌─────────────────────────┐
                           │     Event Sources        │
                           │ MojoSec · OSSEC · Auth   │
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

Dedicated EC2 web nodes can feed this pipeline through the settings-free
[MojoSec host sensor](mojosec_sensor.md). MojoSec keeps host collection,
aggregation, and retry local while leaving policy and blocking authority here.

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

### Noise Control

There is **no automatic ingestion-time deduplication** — every `report_event()` call inserts its own `Event` row. This is deliberate: rule thresholds (`trigger_count`/`trigger_window`/`retrigger_every`), incident event counts, and metrics all count rows, so rows are the volume signal that tells you something is happening a lot. Noise is controlled at other layers:

- **Bundling** — a RuleSet's `bundle_by`/`bundle_minutes` groups many events into one incident without losing rows.
- **Trigger thresholds** — an incident holds at `pending` until `trigger_count` events arrive within `trigger_window`.
- **Opt-in suppression** — for diagnostics reachable from attacker-amplifiable paths, [`report_event_suppressed`](../logging/incidents.md#rate-limited-reporting--report_event_suppressed) files at most one event per `(category, key)` per window. On public, amplifiable categories pass `fail_open=False` and a `budget=` — the default fails **open** (files unsuppressed) during a Redis outage, and per-key suppression alone does not bound unbounded distinct keys.
- **Pruning** — old low-level events are removed after `INCIDENT_EVENT_PRUNE_DAYS`.

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
| `system:health:aws_versions` | Health | AWS managed-service major version drift (opt-in) |
| `system:health:infra_drift` | Health | Fleet drift — serving nodes vs. the recorded topology (opt-in) |
| `api_error` | App | Application error (default category) |
| `invalid_password` | Auth | Wrong password for known user |
| `totp:login_failed` | Auth | TOTP MFA failure |
| `totp:login_unknown` | Auth | TOTP attempt for unknown user |
| `passkey:login_failed` | Auth | Passkey authentication failure |
| `rate_limit:api` | API throttle | Global per-identity throttle engaged (DM-042) |
| `traffic:concentration` | Incident cron | One identity dominating traffic (DM-042) |
| `traffic:ws_connect` | Realtime | Per-IP websocket connect-rate storm (DM-042) |
| `traffic:ws_maxconn` | Realtime | Identity exceeded concurrent websocket cap (DM-042) |
| `auth:redirect_allowlist_unusable_entry` | OAuth `/begin` | A **deployment** redirect-allowlist entry (`ALLOWED_REDIRECT_URLS` or `AUTH_HANDOFF_ALLOWED_URLS`) can never match — operator config bug. Level 3, suppressed per source per hour, fail-open |
| `auth:redirect_allowlist_tenant_entry_unusable` | OAuth `/begin` | A **tenant** `Group.metadata["allowed_redirect_urls"]` entry can never match. Level 1, suppressed per `group:<pk>` per hour, budgeted (25 groups/h), fail-closed |
| `auth:oauth_redirect_refused` | OAuth `/begin` | A `redirect_uri` matched no allowlist source. Level 3, suppressed per host per hour, budgeted (50 hosts/h), fail-closed |

The three redirect-allowlist categories are Redis-suppressed via
`incident.report_event_suppressed` — see
[OAuth › redirect allowlist incidents](../account/oauth.md#redirect-allowlist-incidents).

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
| `maestro` | | Set `1` to also report the Ticket to the configured Maestro default board |
| `board` | | Remote Maestro board id; also opts the Ticket into Maestro reporting |

```
ticket://?priority=9&status=open
ticket://?priority=5&assignee=oncall
ticket://?priority=9&board=3
```

#### `maestro://?board=<remote-id>`

Reports the Incident itself to Maestro without creating a local Ticket. Omit
`board` to use the integration's server-side default:

```
maestro://
maestro://?board=3
```

See [Maestro Workspace Reporting](maestro_board.md).

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
| `metadata.llm_enabled` | Boolean — if True, human replies trigger LLM re-invocation (legacy `llm_linked` honored as an alias) |
| `metadata.requires_approval` | Boolean — set on tickets carrying a pending action note, for UI filtering |

The LLM is **opt-in per ticket** via two `POST_SAVE_ACTIONS`: `enable_llm`
(sets the flag and immediately invokes the agent with the full thread) and
`disable_llm`. Tickets the agent creates itself arrive with `llm_enabled`
already set.

### Ticket Notes

Notes are threaded comments on a ticket. When a ticket is LLM-enabled, adding a note (that doesn't start with `[LLM Agent]`) triggers the LLM agent to review and respond.

A note may instead carry **structured action metadata** — an `action` block
proposing something (rendered as Approve/Deny buttons) or an
`action_response` answering it, which dispatches a registered handler
deterministically (activate the proposed rule, execute the block, …) with no
LLM round-trip. See [Ticket Actions](ticket_actions.md) for the schema,
dispatch guards, and built-in handlers.

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
| `LLM_HANDLER_API_KEY` | None | Anthropic API key. **Required** to enable LLM handlers. Settable from the built-in Admin's Assistant setup (stored encrypted; read at call time, no restart needed). |
| `LLM_HANDLER_MODEL` | (auto-detect suggestion) | Legacy picker/helper input; guarded calls use the exact model owned by their safety-policy route |

If the policy-selected credential is absent, the guard returns the safe
`credential_missing` code. Managed incident attempts retry within their bound
and then terminalize; they do not remain indefinitely investigating.

Both settings are read at invocation time (not at startup), so changes take effect on the next LLM job without a server restart.

**Dependency:** The LLM agent requires the `anthropic` Python package (`anthropic>=0.52.0`), which is included as a framework dependency.

### Remote agent access (MCP)

A separate credential surface, off by default and unrelated to the keys above. `ASSISTANT_MCP_ENABLED` opens `/api/assistant/mcp` to remote AI clients; it is switched from the built-in Admin's Assistant setup only (catalog-protected, so `/api/settings` and every other generic writer refuse it) and is re-read on every request, so closing it takes effect immediately on every node.

The credentials are **OAuth grants, never API keys**: a client signs in through this installation's own sign-in page and receives an access token confined to the resource it consented to, plus a rotating refresh token. Two kinds exist, chosen by the consented scope: `mcp` reaches the tool door alone, and `api` — full REST access as that person — reaches every endpoint their own session token reaches, and nothing outside the API root. An `api` grant is therefore equal to a session token in reach, not more: it widens no permission, it honours the same step-up (`requires_fresh_auth`) rules, and it is revocable from the Admin at any time. A credential it mints in turn — an API key, say — is a separate credential with its own lifetime and is revoked separately, exactly as one minted from a browser session would be. Every active grant is listed in the Assistant setup view — client, operator, connected, last used, expiry — and the owner can disconnect one or all of them. Revocation randomises every column a live credential resolves through, so the access token is refused at the door and the refresh token at the token service on their next use. Switching the door off makes grants dormant rather than revoked: they stop working and stay listed.

What lands in the audit trail: `oauth:grant_created` and `oauth:grant_revoked` user-log lines per grant, and an `admin_settings` "Assistant setup changed" incident event (level 5) for the switch and for each revocation, keyed per grant id so the hourly suppression cannot swallow a second one.

The setup view also carries an owner-only self-check that fetches this installation's own public address and reports the HTTP status it answered with — a status-code oracle for the operator's **own** hostname, behind a superuser-only read, throttled by a 60-second Redis cache. Redirects are never followed. When Redis is unavailable the throttle is lost, not the refusal.

See [the OAuth authorization server](../../web_developer/account/oauth_server.md) for the flow and [the Admin panel docs](../account/admin_portal/assistant.md#remote-agent-access) for the switch, the nginx requirement and revocation.

### Available Tools

The standard triage agent (`execute_llm_handler`) has 16 tools. The analysis agent (`execute_llm_analysis`) has all 18 — the 16 base tools plus 2 analysis-only tools.

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
| `update_ticket` | Update an existing ticket's status/priority/assignee | Yes | Yes |
| `add_note` | Add investigation note to incident history | Yes | Yes |
| `add_ticket_note` | Append a note (with optional incident reference card) to an existing ticket | Yes | Yes |
| `send_alert` | Send email/SMS/notify to specific targets | Yes | Yes |
| `request_approval` | Post a structured Approve/Deny action note instead of acting directly — see [Ticket Actions](ticket_actions.md) | Yes | Yes |
| `merge_incidents` | Merge related incidents into a target incident | No | Yes |

**Configuration:**

| Tool | Description | Triage | Analysis |
|------|-------------|--------|----------|
| `create_rule` | Create a new RuleSet (created `is_active=False`; its review ticket carries an `incident.rule_approval` action note) | Yes | Yes |
| `suggest_rule_update` | Propose widening an existing active rule instead of duplicating it — opens an `incident.rule_update` approval ticket with a rule diff | Yes | Yes |
| `update_rule_memory` | Persist learnings to RuleSet metadata for future invocations | Yes | Yes |

**Analysis-only tool details:**

`merge_incidents` — Takes `target_incident_id` (int) and `incident_ids` (list of ints). Moves all events from the source incidents into the target and deletes the sources. Only merges incidents with the same `category`; already-resolved or ignored incidents are excluded automatically.

`query_open_incidents` — Takes optional `category` (string) and `limit` (int, max 100, default 50). Returns incidents in `new`, `open`, or `investigating` status with event counts. Used to identify merge candidates across the incident backlog.

### Tool Deduplication

`create_ticket` — Deduplication runs in two layers before creating a new ticket:

1. **Same-incident check** — If an open, `llm_linked` ticket already exists directly on the incident, the tool appends a `TicketNote` and an incident history entry to that ticket instead of spawning a duplicate.
2. **Same-category check** — If no ticket exists on the incident itself, but another open `llm_review` ticket is linked to a different incident in the same `incident.category`, the tool appends findings to that ticket instead. The note includes the related incident number and title so the human reviewer can see the full context.

In both dedup cases the tool response includes `deduplicated: true`. The agent's system prompt also receives an "Open Tickets for This Category" section listing up to 10 open tickets for the same category, with explicit instructions to prefer `add_ticket_note` over `create_ticket` when a matching ticket already exists.

`add_ticket_note` — Accepts an optional `incident_id` parameter. When provided, the tool automatically appends a clickable incident reference card to the note's context references, linking the note back to the specific incident that triggered it. This is the preferred approach when the LLM is adding findings from a new incident to an existing ticket.

`create_rule` — Before creating a new `RuleSet`, the tool computes a canonical signature from `category | handler | sorted rule conditions` and compares existing `llm_proposed` RuleSets in the same category. An active proposal is skipped only when both its signature and canonical threshold/bundle policy match. A pending same-category proposal receives an occurrence-count bump only when that policy matches; a different policy gets its own RuleSet and approval ticket so it remains reviewable. Legacy metadata-only threshold aliases do not participate in this comparison because they are not runtime policy. If a matching pending rule's approval ticket has been closed, a fresh ticket is opened on the existing RuleSet.

### LLM-Proposed Rule Thresholds

The incident agent's `create_rule` tool accepts `min_count` and
`window_minutes` as aliases for the canonical `RuleSet.trigger_count` and
`RuleSet.trigger_window` fields. New proposals persist those model fields at
creation time; the aliases are not copied into `RuleSet.metadata`. The approval
note displays both the operative threshold and the bundle window, so reviewers
see the policy that approval will activate.

Both aliases must be positive integers, and `window_minutes` requires
`min_count`. A multi-event threshold (`min_count > 1`) is rejected unless
`bundle_by` is a non-boolean integer from the `RuleSet` choices and is not
`NONE`, `bundle_minutes` is positive, and the bundle window is at least as long
as `window_minutes`. This ensures the requested number of events can remain on
one incident long enough to reach the threshold. Calls that fail validation
return the tool's normal `{ok: false, error: ...}` result without creating a
partial RuleSet or approval ticket.

Proposals created before this contract may have `metadata.min_count` or
`metadata.window_minutes` while their canonical trigger fields are null. Those
legacy rows are not promoted automatically: null trigger fields can also be an
intentional first-event policy, and changing an active rule can re-arm a
pending incident. Audit such `llm_proposed` RuleSets explicitly, compare their
proposal history with current operator intent, and set `trigger_count` /
`trigger_window` manually when remediation is appropriate.

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

When a ticket is LLM-enabled (`metadata.llm_enabled`, legacy `llm_linked` alias honored) and a human adds a note, the full conversation history is sent back to the agent. This allows humans to:
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
| Tools | 16 base tools | 18 tools (includes `merge_incidents`, `query_open_incidents`) |
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

**Cache-only rows:** `tor_exits` and `blocklist_de` are `IPSet` rows created with
`is_enabled=False` — they exist purely as a geoip-detection cache (see
[account/geoip.md](../account/geoip.md#threat-list-caches-tor-exit-list-blocklistde))
and are excluded from `refresh_ipsets`/`sync_firewall`. They're kept warm by the
separate `refresh_threat_lists` cron and can never be enabled: the REST `enable`
action rejects them (400) and `sync()` hard no-ops for them even if the flag is
force-set — otherwise the full Tor exit list / blocklist.de list would be pushed
into the kernel firewall fleet-wide.

### Firewall Reconciliation (`sync_firewall`)

`sync_firewall` reconciles **the kernel of the node it runs on** against DB truth. Every node needs its own run: iptables/ipset state is lost on restart, and no other node can repair it.

It is reached two ways:

- **Hourly, as a broadcast.** The cron dispatcher publishes `broadcast=True` on the `default` channel, which fans out one job per live runner. This is drift reconciliation.
- **At engine start, box-direct.** `asyncjobs.on_engine_start` (registered by the incident `AppConfig`) sets a force flag and publishes a forced reconcile to its own runner's channel. This is boot recovery, and it is what makes a rebooted node recover in seconds rather than up to an hour.

The hook publishes rather than reconciling inline because every firewall write goes through the root-owned broker, which refuses outside a JobEngine execution context — and a startup hook has none.

**Propagation boundary — state it plainly:** a node reconciles hourly only if it runs a jobs runner consuming `default`, and recovers at boot only if that engine consumes its own box-direct channel. With `JOBS_HOSTNAME_CHANNEL = False` no engine consumes its box-direct channel, so the fan-out would strand every job; the hourly path detects this and degrades to the pre-existing single-runner reconcile, and boot recovery is disabled with a warning.

**Performance:** `ipset_load()` uses `ipset restore` with an atomic swap instead of per-CIDR subprocess calls. All CIDRs are batched into a single stdin pipe to `sudo ipset restore`, regardless of set size. The live set is never empty during the swap — entries are loaded into a `<name>_tmp` set, which is then swapped with the live set and destroyed.

**One reconcile at a time per host.** That `<name>_tmp` name is deterministic, so two concurrent `set.replace` calls for one set interleave and can swap in a live set missing entries. A per-host Redis lock (`mojo:sync_firewall:lock:<host>`, `SET NX`, 900s) serialises them; a run that cannot take the lock skips, which is safe because the force flag outlives it.

**Redis keys are per HOST, not per runner** — two engines on one box share one kernel firewall:

| Key | Purpose |
|---|---|
| `mojo:sync_firewall:last_sync:<host>` | Skip-unchanged marker, TTL 7200s |
| `mojo:sync_firewall:force:<host>` | Pending forced reconcile, set by the startup hook |
| `mojo:sync_firewall:lock:<host>` | The reconcile lock above |

**Skip-unchanged behavior:** to stay lightweight on subsequent runs, `sync_firewall` skips IPSets and permanent blocks unchanged since **this host** last synced:

- For `mojo_blocked` (permanent IPs): checks whether any `GeoLocatedIP` with `is_blocked=True` has `modified > last_sync`.
- For each enabled `IPSet`: compares `ipset.modified` against the stored `last_sync` timestamp. Unchanged sets are skipped with a log message.
- On first run for a host (no marker), everything is loaded.
- A **forced** run ignores the marker entirely. It must: the marker lives in shared Redis and therefore survives the very reboot boot recovery exists to repair.

**The marker only advances on a clean run.** If any `ipset_load` failed while there was something to load, the marker is left alone and the force flag is not cleared, so the next reconcile retries instead of recording a success this node never achieved. An empty CIDR list is not a failure — `ipset_load` returns `(False, 0)` there deliberately, refusing to wipe a live set with an empty swap.

**Logging:** The job logs each loaded set (count/total CIDRs), skipped sets, any failed loads, and records the new sync timestamp in Redis on a clean completion.

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

### Auth Metrics

One aggregate counter is recorded for all authentication failure events:

| Metric | Account | Category | When |
|--------|---------|----------|------|
| `auth:failures` | `incident` | `auth` | Any event whose category is `invalid_password`, `login:unknown`, `totp:login_failed`, `totp:login_unknown`, or `passkey:login_failed` |

Bumped automatically by `Event.save()` when `INCIDENT_EVENT_METRICS` is enabled. The category set is defined in `AUTH_FAILURE_CATEGORIES` (a `frozenset` in `mojo/apps/incident/models/event.py`) — add new failure categories there without touching the recording logic.

```python
# Fetch the current hour's auth failure count
result = metrics.fetch_values(["auth:failures"], account="incident", granularity="hours")
count = result["data"]["auth:failures"]
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
2. Configure OSSEC to POST alerts with the secret in the `X-OSSEC-SECRET` header
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

Two further health categories are filed by separate opt-in daily jobs rather
than by `check_system_health`, and appear on the same
`/api/incident/health/summary` strip:

- `system:health:aws_versions` — see [AWS version drift](../aws/version_drift.md).
  Level 4 (inventory incomplete), 5 (major upgrade available), 8 (support
  deadline near) or 10 (deadline passed).
- `system:health:infra_drift` — see [fleet drift](../aws/infra_drift.md). Level 4
  (an AWS read did not answer) or 5 (a serving node is not in the recorded
  topology, or a recorded node is serving nothing). Never higher; a matching
  fleet files nothing at all.

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
| `prune_mojosec_receipts` | Daily 8:15 AM | Deletes published and dead MojoSec receipts older than `MOJOSEC_RECEIPT_RETENTION_DAYS`; never deletes live pending publication rows |
| `prune_mojosec_learning` | Daily 8:25 AM | Deletes bounded offline evaluation summaries older than `MOJOSEC_LEARNING_EVALUATION_RETENTION_DAYS`; feedback/proposal audit rows remain |
| `replay_mojosec_handler_outbox` | Every 5 minutes | Replays published MojoSec receipts whose handler dispatch is pending, failed, or stale-queued past `MOJOSEC_HANDLER_QUEUED_STALE_SECONDS`; dead-letters receipts at `MOJOSEC_HANDLER_MAX_ATTEMPTS` and pending receipts whose Event was pruned |
| `settle_mojosec_cases` | Every 5 minutes | Settles quiet MojoSec deployment cases past `MOJOSEC_DEPLOY_QUIET_SECONDS`, heals crashed case promotions, re-drives stranded case-routed receipts, and coalesces distributed web probes into campaign cases; system transitions only, never Events |
| `sweep_mojosec_actions` | Every 5 minutes | Proposes MojoSec block recommendations from correlated cases, auto-approves within bounds, executes/retries validated targets, and expires stale proposals and applied-target TTLs |
| `prune_events` | Daily 9:45 AM | Deletes events older than `INCIDENT_EVENT_PRUNE_DAYS` days with level < 6 |
| `sweep_expired_blocks` | Every 5 minutes | Unblocks IPs where `blocked_until` has passed |
| `sync_firewall` | Hourly (broadcast — every runner) | Each node rebuilds its OWN ipsets from DB truth; skips sets unchanged since that host's last sync. Boot recovery is the separate `on_engine_start` hook |
| `refresh_ipsets` | Weekly (Sunday 3 AM) | Re-fetches IPSet source URLs and syncs CIDRs to fleet |
| `refresh_threat_lists` | Every 6 hours | Refreshes the cache-only `tor_exits`/`blocklist_de` IPSet rows (`refresh_from_source()` only — never synced to the firewall); see [account/geoip.md](../account/geoip.md#threat-list-caches-tor-exit-list-blocklistde) |
| `recheck_active_threats` | Daily 4:20 AM | Re-scores up to `GEOLOCATION_RECHECK_THREATS_MAX` (500) recently-active `GeoLocatedIP` rows so a stale `threat_level` can **decay** — everything else only ratchets up. Skips `provider='mojo'` records and external blocklist lookups; see [account/geoip.md](../account/geoip.md#decay) |
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
| `INCIDENT_EVENT_PRUNE_DAYS` | `30` | Days to keep low-level events before pruning |
| `INCIDENT_EVENT_METRICS` | `False` | Enable metrics recording for events |
| `INCIDENT_METRICS_MIN_GRANULARITY` | `"hours"` | Metrics time granularity |

### LLM Agent Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `LLM_HANDLER_API_KEY` | None | Anthropic API key. Required for `llm://` handlers. Also settable from the built-in Admin's Assistant setup. |
| `LLM_HANDLER_MODEL` | (auto-detect) | Claude model for triage. If unset, auto-detects latest Sonnet via `mojo.helpers.llm.get_model()` |

### OSSEC Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `OSSEC_SECRET` | None | Shared secret for legacy OSSEC webhook auth; unset/empty disables both endpoints |
| `MOJOSEC_RECEIPT_RETENTION_DAYS` | `45` | Published receiver-idempotency retention; minimum 7 days |
| `MOJOSEC_HANDLER_MAX_ATTEMPTS` | `100` | Handler dispatch attempts before a receipt is dead-lettered (~8h of continuous failure) |
| `MOJOSEC_HANDLER_QUEUED_STALE_SECONDS` | `1800` | Age after which a queued receipt's vanished dispatch job is recovered inline by the replay cron |

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

| Rule | Category | Matches | Trigger | Handler | Bundle |
|------|----------|---------|---------|---------|--------|
| Credential Stuffing | `login:unknown` | level >= 8 | 25 events / 60min | `block://?ttl=1800` | SOURCE_IP, 60min |
| Password Brute Force | `invalid_password` | level >= 5 | 5 events / 15min | `block://?ttl=1800` | SOURCE_IP, 15min |
| Bouncer Token Abuse | `security:bouncer:token_invalid` | level >= 7 | 10 events / 30min | `block://?ttl=1800` | SOURCE_IP, 30min |

### Bouncer Rules

| Rule | Category | Matches | Trigger | Handler | Bundle |
|------|----------|---------|---------|---------|--------|
| Honeypot Detection | `security:bouncer:honeypot_post` | level >= 9 | first event | `block://?ttl=3600` | SOURCE_IP, 30min |
| Bot Campaign | `security:bouncer:campaign` | level >= 10 | first event | `block://?ttl=86400,notify://perm@manage_security` | SOURCE_IP, 60min |
| High Confidence Bot | `security:bouncer:block` | risk_score >= 80 | 3 events / 30min | `block://?ttl=3600` | SOURCE_IP, 30min |
| In-Session Freeze | `security:bouncer:session_freeze` | level >= 9 | 3 events / 60min | `block://?ttl=86400,notify://perm@manage_security` | SOURCE_IP, 60min |

Every blocking rule a legitimate user could trip carries a `trigger_count`, so one mistyped username or one expired token can never firewall a shared NAT egress. Honeypot and campaign stay on "first event" because no legitimate user can produce their match. See [Default auth and bouncer rulesets](../logging/incidents.md#default-auth-and-bouncer-rulesets) for the reasoning and for how to retune a deployment bootstrapped before the gates existed.

### Health Rules

| Rule | Category | Matches | Handler | Bundle |
|------|----------|---------|---------|--------|
| Runner Down | `system:health:runner` | level >= 10 | `notify://perm@manage_security,ticket://?priority=9` | HOSTNAME, 30min |
| Scheduler Missing | `system:health:scheduler` | level >= 10 | `notify://perm@manage_security,ticket://?priority=9` | NONE, 60min |
| TCP Overload | `system:health:tcp` | level >= 8 | `notify://perm@manage_security` | HOSTNAME, 30min |
| AWS Version Drift | `aws:versions` (matched by event **scope**) | level >= 5 | `notify://perm@manage_security,ticket://?priority=8&category=aws-version-drift&maestro=1` | NONE |
| Infrastructure Drift | `infra:drift` (matched by event **scope**) | level >= 5 | `notify://perm@manage_security` | NONE |

`ensure_health_rules()` creates the first three. The two AWS ones are opt-in
(`RuleSet.ensure_aws_version_rules()` / `RuleSet.ensure_infra_drift_rules()`, or
`aws-check --apply --section rules`) and deliberately sit on `aws:versions` and
`infra:drift` rather than inside the `system:health:` namespace, so neither can
satisfy the health-defaults bootstrap guard in
`mojo/apps/incident/cronjobs.py` and suppress the three above.

Infrastructure Drift is **notify only** — no `ticket://`. Drift is a one-minute
reconciliation in System Setup, and on an externally-managed estate it recurs
until someone records the node.

Health rules **never** use `block://` — infrastructure issues should not block IPs.

## Related Documentation

- [Authenticated-Abuse Hardening](abuse_hardening.md) — global per-identity API throttle, traffic-concentration detection, account kill switch, websocket connection limits, deployment hardening
- [Content Security Policy](csp.md) — the nonce-based CSP on the hosted auth pages, **opt-in and off by default** (`AUTH_CSP_ENABLED` ships `False`): how to turn it on, the default policy, the per-page `frame-ancestors` rule, the `{{ csp_nonce }}` contract for overridden templates, and the `AUTH_CSP_*` settings
- [Maestro Workspace Reporting](maestro_board.md) — deployment ApiKey setup, direct Incident or Ticket reporting, remote/default board routing, signed callbacks and echo suppression
- [Ticket Actions](ticket_actions.md) — structured Approve/Deny action notes on tickets: schema, dispatch guards, built-in handlers (rule approval/update, block confirm, escalate), handler registration, LLM opt-in contract
- [Bouncer Architecture](../account/bouncer.md) — bot detection, scoring, tokens, signatures
- [GeoIP System](../account/geoip.md) — IP geolocation, blocking, threat escalation
- [Permissions](../core/permissions.md) — `security` category permission for admin access
- [Web Developer: Security Dashboard](../../web_developer/security/README.md) — REST API reference for building security UIs
- [Web Developer: Incident API](../../web_developer/logging/incidents.md) — REST API for incidents, events, tickets
