# Security System Analysis & Improvements

**Status**: Done (all phases complete, future items tracked below)
**Priority**: High
**Created**: 2026-03-27

## Overview

Full analysis of how django-mojo handles security — from event detection through incident management to fleet-wide enforcement. This covers what exists, what's working, what's incomplete, and where we should go next.

## Current Architecture

### The Pipeline

```
Detection → Event → Rules → Incident → Handlers → Enforcement
```

**Five layers of defense:**

| Layer | System | What it does |
|-------|--------|-------------|
| 1. Pre-auth | Bouncer | Bot detection, device fingerprinting, risk scoring before login |
| 2. Rate limiting | `@md.rate_limit` / `@md.strict_rate_limit` | Per-IP/device/API-key throttling |
| 3. Event detection | `incident.report_event()` | Auth failures, token abuse, permission violations, OSSEC alerts |
| 4. Rule engine | RuleSet + Rules | Pattern matching, threshold logic, incident creation |
| 5. Enforcement | GeoLocatedIP + firewall.py | Fleet-wide IP blocking via iptables/ipset |

### Event Sources (What Feeds the System)

| Source | Categories | Level | Goes to |
|--------|-----------|-------|---------|
| Failed login (unknown user) | `login:unknown` | 8 | Incident |
| Failed login (wrong password) | `invalid_password` | 1 | Incident |
| Invalid/expired/reused token | `invalid_token`, `expired_token` | 8 | Incident |
| Rate limit violation | `rate_limit:{endpoint}` | 5 | Incident + Metrics |
| Bouncer block decision | `security:bouncer:block` | 8 | Incident |
| Bouncer monitor decision | `security:bouncer:monitor` | 5 | Incident |
| Bouncer campaign detected | `security:bouncer:campaign` | 1 | Incident |
| MFA/TOTP failures | `totp:login_failed`, `totp:confirm_failed` | 1 | Incident |
| TOTP recovery code used | `totp:recovery_used` | 1 | Incident |
| Email/phone change | `email_change:*`, `phone_change:*` | 1 | Incident |
| Password reset | `password_reset` | 1 | Incident |
| Account deactivation | `account:deactivated` | 1 | Incident |
| Session revocation | `sessions:revoked` | 1 | Incident |
| Magic login (unknown user) | `magic_login:unknown` | 8 | Incident |
| OSSEC alerts | `ossec` | varies | Incident |
| Permission changes | `permission:added/removed` | — | Logit only |
| Password changes | `password:changed` | — | Logit only |
| API key management | `api_key:generated/revoked` | — | Logit only |
| Firewall actions | `firewall:block/unblock/whitelist` | — | Logit only |
| Middleware errors | `api_error` | — | Logit only |

### Two Tracking Systems

| System | Model | Purpose | Queryable by |
|--------|-------|---------|-------------|
| **Incidents** | Event → Incident | Security detections, rule-based correlation | category, scope, level, source_ip, date range |
| **Logit** | Log | Audit trail, operational activity | kind, level, uid, model_name, date range |

**Key distinction**: Incidents are for *detections* (something suspicious happened). Logit is for *actions* (someone did something). An incident might trigger a firewall block (incident) which is then logged (logit).

### Rule Engine

RuleSets match events by category/scope and apply rules (field comparisons). When matched:

1. **Bundling** — group related events into one incident (by IP, hostname, model, or combinations)
2. **Thresholds** — hold in "pending" until min_count events in window_minutes
3. **Handlers** — chainable actions via URL syntax, all execute as async jobs:
   - `block://?ttl=600` — fleet-wide IP block
   - `ticket://?priority=8&category=security` — create ticket
   - `job://module.function?param=value` — publish async job
   - `email://perm@manage_security?template=name` — email verified users
   - `sms://perm@manage_security` — SMS verified users
   - `notify://perm@manage_security` — in-app + push notification
   - `llm://` — autonomous LLM triage agent via Claude API (tool use)

   **Target resolution** (comma-separated, mix and match):
   - `perm@name` — all active users with that permission
   - `protected@key` — all active users with `metadata.protected.{key} = True` (opt-in)
   - `username` — single user by username

   All notification handlers resolve targets to Users only. No notifications sent to non-User addresses. Email requires `is_email_verified=True`, SMS requires `is_phone_verified=True`.

### Fleet Enforcement

- `GeoLocatedIP.block()` → `jobs.broadcast_execute("block_ip", {ips, ttl})`
- Every runner calls `firewall.block(ip)` → `iptables -I INPUT -s {ip} -j DROP`
- Cron `sweep_expired_blocks` runs every minute, unblocks expired IPs fleet-wide
- IPSet bulk blocking for countries/abuse lists via kernel ipset
- Weekly cron refreshes ipset sources (ipdeny, AbuseIPDB)

## What's Working Well

1. **Defense in depth** — bouncer pre-auth → rate limiting → incident rules → IP blocking
2. **Fleet-wide consistency** — single authority (GeoLocatedIP in Postgres), broadcast enforcement
3. **Injection prevention** — firewall.py validates IPs with strict regex before any subprocess call
4. **User isolation** — firewall.py refuses to run as anything other than ec2-user
5. **Async logging** — middleware uses background thread queue, never blocks responses
6. **Audit trail** — IncidentHistory tracks state changes, handler execution, merges, and admin edits on incidents
7. **Sensitive data masking** — LOG_CHANGES masks password, key, secret, token fields
8. **OSSEC integration** — external alerts feed into the same rule engine as app events
9. **Threshold logic** — pending → new transitions prevent alerting on isolated events
10. **Bouncer learning** — auto-escalates subnet/fingerprint/user-agent signatures from block patterns

## Weaknesses & Gaps

### ~~Critical: Incomplete Handlers~~ — RESOLVED

All handlers are now implemented:

| Handler | Status | What it does |
|---------|--------|-------------|
| `block://` | ✅ Working | Fleet-wide IP blocking |
| `ticket://` | ✅ Working | Creates tickets from rules |
| `job://` | ✅ Working | Publishes async job with event context |
| `email://` | ✅ Working | Emails verified users via SES, supports templates |
| `sms://` | ✅ Working | SMS to verified users via PhoneHub |
| `notify://` | ✅ Working | In-app + push notification to users |
| `llm://` | ✅ Working | Autonomous LLM triage agent via Claude API with tool use |

### ~~High: Handler Execution is Synchronous~~ — RESOLVED

All handlers now execute asynchronously via the job queue. `RuleSet.run_handler()` publishes one job per handler spec to the `incident_handlers` channel. The `execute_handler()` job function loads the event/incident by ID and runs the handler in the background.

- `report_event()` stays synchronous for detection (Event + RuleSet matching + Incident creation)
- Handlers always run as background jobs — even cheap ones like `ticket://` for consistency
- No caller-side decision needed — the API stays simple
- History recording (success/fail) happens in the job callback
- A login request that triggers a `block://` rule doesn't wait for fleet broadcast

### ~~High: No Metrics on Security Events~~ — RESOLVED

The incident system now records comprehensive metrics. Firewall metrics added in Phase 1, incident lifecycle metrics added in Phase 2. Previously missing metrics:

- `firewall:blocks` / `firewall:auto_blocks`
- `incident:created` / `incident:escalated`
- `bouncer:blocks` / `bouncer:monitors`
- Per-country block counts
- Threat level distribution over time

We only need metrics for **blocking** events (blocks, auto-blocks, country-level blocks, broadcasts). Unblocks and whitelist changes don't need time-series tracking — they're low-volume administrative actions that are adequately tracked via logit entries.

Without block metrics, you can't build time-series dashboards or set up anomaly alerts.

### High: Logit Entries are Unstructured

Firewall actions use `self.log()` with free-text strings. The `payload` field exists but isn't used. This makes filtering and aggregation require string parsing. (Addressed in `planning/requests/firewall_event_tracking.md`.)

### ~~Medium: No Event Deduplication~~ — RESOLVED

`reporter.report_event()` now deduplicates within a configurable time window (`INCIDENT_DEDUP_WINDOW_SECONDS`, default 60s). Matching events (same category + level + source_ip + hostname) increment `metadata.dedup_count` on the existing event instead of creating new rows.

### Medium: Rule Engine Checks Metadata, Not Model Fields

Rules check `event.metadata.get(field_name)` but not `event.<field_name>` directly. Model fields like `level`, `category`, `model_name` are only accessible if `sync_metadata()` has been called. The known OSSEC bug (`bug_default_ossec_rules_never_match`) is caused by this — default rules checking model fields that aren't in metadata yet.

### ~~Medium: No Web Developer Security Overview Doc~~ — RESOLVED

Created `docs/web_developer/security/README.md` — a unified guide tying together incidents, events, firewall, bouncer, logs, and metrics. Includes dashboard building patterns, API reference, metrics slugs, LLM agent overview, and settings reference.

### ~~Low: OSSEC Ingest is Unauthenticated~~ — RESOLVED

`POST /ossec/alert` and `/ossec/alert/batch` now support optional secret-based auth. Any attacker who discovers these endpoints could flood the incident system with fake events. OSSEC is standard in every fleet deployment and should stay **on by default** — but the endpoints need optional secret-based auth.

**Solution**: `OSSEC_SECRET` Django setting:

| Value | Behavior |
|-------|----------|
| `None` (default) | Endpoints are open — doesn't break existing setups |
| `"some-secret"` | Requires `X-OSSEC-Secret` header on every request |

Load once at module level via `settings.get_static("OSSEC_SECRET", None)` — not `settings.get()` which hits the DB on every call. The check is a simple constant comparison, zero overhead when unset.

### ~~High: IncidentHistory is Completely Unused~~ — RESOLVED

IncidentHistory is now wired up at 7 touchpoints via `Incident.add_history()`:

| Action | Where | `kind` |
|--------|-------|--------|
| Incident created | `event.py` | `created` |
| Priority escalated | `event.py` | `priority_changed` |
| Status transition (pending → new) | `event.py` | `status_changed` |
| Threshold crossed | `event.py` | `threshold_reached` |
| Handler fired (success/fail) | `rule.py` | `handler:{scheme}` |
| Incidents merged | `incident.py` | `merged` |
| REST field changes (admin edits) | `incident.py on_rest_saved` | `status_changed`, `priority_changed`, `state_changed`, `updated` |

Still planned for future: handler completion callbacks (async), assignment changes, manual notes, LLM assessment.

### Low: No Auto-Merge Logic

Incidents can be manually merged via REST action, but there's no automatic merge when patterns indicate the same attack from different vectors.

### ~~High: No System Health Monitoring~~ — RESOLVED

We have two complementary sources for system health:

**CloudWatch** (`CloudWatchHelper` in `mojo.helpers.aws.cloudwatch`) already provides time-series metrics for EC2 instances, RDS databases, and ElastiCache (Redis) clusters. CPU, memory, disk, and network metrics are already available there — we should **not** duplicate those as custom metrics.

**Jobs system** (`jobs.get_sysinfo()`) collects real-time CPU, memory, disk, network (including TCP connection count) from every runner via `broadcast_execute`. `get_runners()` shows which runners are alive. `ping()` checks individual runner responsiveness. Every EC2 instance should have a job engine runner, so runner health ≈ instance health.

But nothing runs these checks on a schedule. There's no cron that:

- Checks if all expected runners are alive (heartbeat check)
- Checks if the scheduler is running
- Polls `get_sysinfo()` and flags resource exhaustion
- Fires an incident event when thresholds are breached

**TCP connections are the critical metric**: nginx per node starts having issues above ~2,000 connections. This is the most common exhaustion vector and isn't easily visible in CloudWatch.

**Key design principle**: Health checks should fire individual events, and the **rules engine** handles detecting consecutive issues via threshold/bundling logic. A single CPU spike is a blip — the rule engine's `min_count` + `window_minutes` catches sustained problems. This keeps the health check cron simple (detect and report) while reusing the existing incident pipeline for escalation.

**What's needed**: A periodic health check cron (every 1-5 minutes) that:
1. Calls `get_runners()` to verify expected runners are alive
2. Calls `get_sysinfo()` across all nodes
3. Checks thresholds: TCP connections > configurable limit (default ~2,000), CPU > 90%, memory > 90%, disk > 85%
4. Fires `incident.report_event()` for each threshold breach (category: `system:health:{check_type}`, e.g. `system:health:tcp`, `system:health:cpu`)
5. Verifies the scheduler is running (leader lock exists and is fresh)
6. Detects runner disappearance (was alive last check, now missing) — immediate high-severity event

**Not needed**: Custom time-series metrics for CPU, memory, or disk — CloudWatch already provides those. The health check only needs to fire events; the rules engine handles escalation. For example, a rule: "if `system:health:tcp` events for the same runner exceed 3 in 10 minutes, create a ticket and email the admin."

**CloudWatch complements this** by providing the historical time-series data (RDS connection counts, ElastiCache memory, EC2 CPU trends) that an admin can review when investigating an incident.

## Suggested Improvements

### Phase 1: Complete the Basics

1. ~~**Implement email handler**~~ — DONE. `email://perm@manage_security` resolves users by permission, sends to verified emails only via SES. Supports `?template=name` for custom email templates. Requires `INCIDENT_EMAIL_FROM` setting. Includes `ADMIN_PORTAL_URL` link when configured.

2. ~~**Implement notify handler**~~ — DONE. `notify://perm@manage_security` sends in-app + push notifications via `Notification.send()`. Supports comma-separated targets. Includes `action_url` deep link to incident.

3. ~~**Implement job handler**~~ — DONE. `job://module.path.function?param=value` publishes via `jobs.publish()`. Payload includes event context plus all URL params.

    **Also implemented:**
    - **SMS handler** — `sms://perm@manage_security` sends SMS via PhoneHub to users with verified phone numbers.
    - **Ticket handler** — `ticket://?status=open&priority=8` creates support tickets linked to incidents.
    - **Shared user resolver** — `_resolve_users()` handles three target types for all notification handlers:
      - `perm@name` — permission-based fan-out
      - `protected@key` — metadata opt-in (`metadata.protected.{key} = True`)
      - `username` — direct user lookup
    - All notifications go to Users only — no anonymous addresses.
    - **Settings**: `INCIDENT_EMAIL_FROM` (SES mailbox), `ADMIN_PORTAL_URL` (deep links in notifications)

    **Permissions cleanup (incident app):**
    - All incident app models consolidated to `view_security` (VIEW_PERMS) / `manage_security` (SAVE_PERMS, DELETE_PERMS)
    - Models updated: Incident, Event, IncidentHistory, Ticket, TicketNote, RuleSet, Rule, IPSet

4. ~~**Add firewall block metrics**~~ — DONE. `metrics.record()` on `block()`, `update_threat_from_incident()` auto-block, and broadcast. Slugs: `firewall:blocks`, `firewall:auto_blocks`, `firewall:blocks:country:{CC}`, `firewall:broadcasts`.

5. ~~**Add structured logit payloads**~~ — DONE. All firewall methods (`block`, `unblock`, `whitelist`, `unwhitelist`, `update_threat_from_incident`) now pass structured JSON payloads with `ip`, `reason`, `trigger`, and action-specific fields. Duplicate `self.log()` calls removed from `on_action_*` handlers.

6. ~~**Fix the metadata/model-field bug**~~ — DONE. `Rule.check_rule()` already falls back to `getattr(event, field_name)` if the field isn't in metadata.

7. ~~**Wire up IncidentHistory**~~ — DONE. Added `Incident.add_history()` helper and history entries at: incident creation, priority escalation, status transitions, threshold crossing, handler execution (with success/fail), merge, and REST field changes.

### Phase 2: Operational Improvements

8. ~~**System health monitoring cron**~~ — DONE. `check_system_health` cron runs every 3 minutes (gated by `HEALTH_MONITORING_ENABLED` setting, default `False`). The job:
   - Calls `get_runners()` and fires level-10 events for non-alive runners (`system:health:runner`)
   - Calls `get_sysinfo()` across all nodes and checks per-runner thresholds:
     - TCP connections > `HEALTH_TCP_MAX` (default 2000) → level 8 event (`system:health:tcp`)
     - CPU > `HEALTH_CPU_CRIT` (default 90%) → level 5 event (`system:health:cpu`)
     - Memory > `HEALTH_MEM_CRIT` (default 90%) → level 5 event (`system:health:memory`)
     - Disk > `HEALTH_DISK_CRIT` (default 85%) → level 5 event (`system:health:disk`)
   - Checks scheduler leader lock → level 10 event if missing (`system:health:scheduler`)
   - Events feed into the rules engine — threshold/bundling logic catches sustained problems
   - Does NOT record custom time-series metrics (CloudWatch handles those)

9. ~~**Make all handlers async**~~ — DONE. `RuleSet.run_handler()` now publishes one job per handler spec via `jobs.publish()` to the `incident_handlers` channel. The job function `execute_handler()` in `event_handlers.py` loads the event/incident by ID and runs the handler. Detection stays synchronous (Event → Rules → Incident), all handler execution is background. History recording (success/fail) happens in the job, not inline.

10. ~~**Event deduplication**~~ — DONE. `reporter.report_event()` now checks for a recent identical event (same category + level + source_ip + hostname within `INCIDENT_DEDUP_WINDOW_SECONDS`, default 60s) before creating a new row. If a match is found, increments `metadata.dedup_count` on the existing event instead. Dedup is disabled when the setting is 0 or None.

11. ~~**Secure OSSEC ingest**~~ — DONE. `OSSEC_SECRET` setting loaded once at module level via `settings.get_static("OSSEC_SECRET", None)`. When set, both `/ossec/alert` and `/ossec/alert/batch` require `X-OSSEC-Secret` header. Default `None` = open (no breakage for existing setups). Returns 403 with warning log on mismatch.

12. ~~**Incident metrics**~~ — DONE. Added `incidents:escalated` (on priority escalation in `event.py`), `incidents:threshold_reached` (on pending→new transition), and `incidents:resolved` (on REST status change to resolved in `incident.py`). All gated by `INCIDENT_EVENT_METRICS` setting, using same `account="incident"` pattern as existing metrics.

### Phase 3: Intelligence & Visibility

13. ~~**Web developer security overview doc**~~ — DONE. Created `docs/web_developer/security/README.md` — unified guide covering the full security pipeline, all APIs, dashboard building patterns, metrics reference, LLM agent overview, and settings. Also updated `docs/web_developer/logging/incidents.md` with new permissions, status lifecycle, history kinds, ticket/note APIs, and LLM integration. Updated firewall.md permissions.

14. ~~**LLM security agent**~~ — DONE. Autonomous agent that triages incidents, detects trends, and learns over time. Uses the **Claude API directly** (not Bedrock — simpler, no IAM, faster model access).

    **Goal**: Human security teams can't keep up with incident volume. Most incidents (especially OSSEC) are noise. The LLM agent acts as a first responder — triaging everything, handling the obvious, and only escalating real issues to humans via tickets.

    #### Entry point

    The `llm://` handler fires when a rule matches and an incident is created/transitioned. **Additionally, when an event exceeds `INCIDENT_LEVEL_THRESHOLD` but no rule matches, the LLM is automatically invoked if `LLM_HANDLER_API_KEY` is configured.** This ensures every high-level incident gets triaged — either by a rule or by the LLM.

    The LLM has access to `query_event_counts` and `query_metrics` tools, so it naturally sees trends when triaging individual incidents ("there are 50 similar events in the last hour"). This gives trend awareness without a separate cron.

    **Future**: A `llm_trend_analysis` periodic cron could catch cross-category correlations and patterns where no single rule fires. Deferred until we see gaps in the event-triggered approach.

    **Incident triage flow** (`llm://` handler or auto-fallback):
    1. Incident arrives with `status="new"` (unhandled)
    2. LLM picks it up → sets `status="investigating"`
    3. Queries context via tools (events, IP history, related incidents, geoip, metrics, event counts)
    4. Decides:
       - **Noise / false positive** → `status="ignored"` + history note with reasoning
       - **Real, handled** → `status="resolved"` + action (block IP, etc.) + history
       - **Unsure / needs human** → create Ticket (llm_linked) + leave as `"investigating"`

    #### Incident status lifecycle

    | Status | Who sets it | Meaning |
    |--------|------------|---------|
    | `pending` | System | Below threshold, accumulating events |
    | `new` | System | Unhandled — neither human nor LLM has touched it |
    | `investigating` | LLM | LLM is actively working on it |
    | `resolved` | LLM or Human | Real + handled (blocked, notified, etc.) |
    | `ignored` | LLM or Human | Noise / false positive |
    | `open` | Human | Human has taken ownership |
    | `paused` | Human | Human paused investigation |
    | `closed` | Human | Final — done, archived |

    Humans work from `status="open"`. The LLM clears the `"new"` queue.

    #### Prompt architecture

    1. **System prompt** (always loaded) — generic security context: what the incident system is, available tools, how to reason about threats
    2. **`RuleSet.metadata.agent_prompt`** — custom per-rule instructions (e.g., "OSSEC rule 5710 from internal IPs is always false positive")
    3. **`RuleSet.metadata.agent_memory`** — LLM's own learnings for this rule type, updated over time
    4. **Event/incident context** — structured data injected in the user message

    #### Tool set (Claude API tool use)

    **Read tools:**

    | Tool | What it does |
    |------|-------------|
    | `query_events` | Recent events by category/IP/hostname with time range |
    | `query_event_counts` | Aggregate counts by category over time windows (trend detection) |
    | `query_ip_history` | GeoLocatedIP record — threat level, block history, country, past incidents |
    | `query_related_incidents` | Other incidents from same source IP, including past LLM assessments |
    | `query_incident_events` | All events bundled into this incident |
    | `query_metrics` | Fetch metric time-series (firewall, incident, API metrics) |

    **Write tools:**

    | Tool | What it does |
    |------|-------------|
    | `update_incident` | Change status + add IncidentHistory note |
    | `block_ip` | `GeoLocatedIP.block()` with reason + IncidentHistory entry |
    | `create_ticket` | Ticket for human review (`metadata.llm_linked=True`) |
    | `add_note` | Add IncidentHistory entry with reasoning |
    | `send_alert` | Dispatch email/sms/notify to targets via existing handlers |
    | `create_rule` | New RuleSet + Rules (created **disabled**, pending human approval via ticket) |
    | `update_rule_memory` | Write learnings to `RuleSet.metadata.agent_memory` |

    #### Ticket conversation loop (human ↔ LLM)

    Tickets are the primary mechanism for LLM ↔ human interaction:

    ```
    LLM creates Ticket (metadata.llm_linked=True, incident attached)
      → LLM posts TicketNote: "I see pattern X. Proposed rule: Y. Approve?"
      → Human posts TicketNote: "Yes but increase threshold to 10"
      → TicketNote.on_rest_saved() hook → detects llm_linked → publishes job
      → LLM re-invoked with full note history as context
      → LLM posts TicketNote: "Done. Rule created with min_count=10. Enabling."
      → LLM enables rule, resolves ticket
    ```

    The `TicketNote.on_rest_saved()` hook checks `ticket.metadata.llm_linked`. If True, publishes a job that feeds the full ticket conversation (all notes) back to the LLM so it can continue the interaction.

    #### Escalation paths (severity-based)

    | LLM assessment | Action |
    |---------------|--------|
    | Noise | `ignored` + history note |
    | Low risk, resolved | `resolved` + history |
    | Needs human input | Ticket + notify (in-app) |
    | Concerning | Ticket + email |
    | Critical | Ticket + SMS + block if applicable |

    #### Memory & learning

    The LLM builds knowledge over time through two mechanisms:

    1. **Rule creation** — When the LLM sees a recurring pattern, it creates a new RuleSet (disabled) and a ticket for human approval. Once approved, the rule engine handles that pattern directly — no LLM needed. The LLM effectively **trains the rule engine**.

    2. **Rule memory** — `RuleSet.metadata.agent_memory` stores per-rule-type learnings. Example: "OSSEC rule 5710 from internal IPs in 10.0.0.0/8 is always a false positive from our deploy process." The LLM reads this on every invocation for that rule type, so it learns from past decisions.

    3. **Past assessments** — `query_related_incidents` returns past LLM assessments stored in `incident.metadata.llm_assessment`. The LLM sees its own history.

    #### Configuration

    - `LLM_HANDLER_API_KEY` — Claude API key (required to enable)
    - `LLM_HANDLER_MODEL` — model to use (default: claude-sonnet)
    #### Future considerations

    - **Token budget** — daily token cap / rate limiting on LLM usage. For now, cap via Claude console API settings.
    - **Trend analysis cron** — periodic job that catches cross-category correlations and patterns where no single rule fires. Add when gaps are found in the event-triggered approach.
    - **Slack handler** — `slack://channel_name?severity=high` for easier human ↔ system interaction. Slack could eventually become a ticket interface.

15. ~~**Anomaly detection without LLM**~~ — Folded into item 14. The LLM trend analysis cron replaces the need for a separate statistical anomaly detection system. The LLM can spot patterns, cross-reference, and reason about context in ways simple std-dev thresholds cannot. The trend analysis cron queries `query_event_counts` and `query_metrics` to detect spikes and anomalies.

## Future: Security Permissions Cleanup

Consolidated to two clean permissions:

| Perm | Access |
|------|--------|
| `view_security` | Read-only: incidents, events, history, tickets, firewall status |
| `manage_security` | Full: create/edit/delete incidents, rules, merge, block IPs, manage tickets |

**Incident app — DONE**: All models (Incident, Event, IncidentHistory, Ticket, TicketNote, RuleSet, Rule, IPSet) updated to `view_security`/`manage_security`.

**Remaining**: Other security-related models outside the incident app (e.g., GeoLocatedIP in account app) still use old permission names. Track existing usage across all apps and ensure backward compatibility for deployments using the old perm names.

## Documentation

| Document | Location | Status |
|----------|----------|--------|
| Security Overview (web dev) | `docs/web_developer/security/README.md` | Done |
| Incident API (web dev) | `docs/web_developer/logging/incidents.md` | Done (updated) |
| Firewall API (web dev) | `docs/web_developer/account/firewall.md` | Done (permissions updated) |
| Security Architecture (django dev) | `docs/django_developer/security/README.md` | Future |
| Incident Handlers (django dev) | `docs/django_developer/logging/handlers.md` | Future |
| Bouncer Tuning (django dev) | `docs/django_developer/account/bouncer_tuning.md` | Future |

## Acceptance Criteria (for this analysis)

- [x] Full analysis of current security architecture
- [x] All event sources mapped
- [x] Handler stub gap identified
- [x] Metrics gaps identified
- [x] Improvement phases prioritized
- [x] LLM integration concept described
- [x] LLM agent design: triage + trend analysis + ticket conversation loop + memory/learning
- [x] Phase 1 implementation (handlers, metrics, structured payloads, history, permissions)
- [x] Phase 2 implementation (async handlers, dedup, OSSEC_SECRET, health cron, incident metrics)
- [x] Phase 3 implementation (LLM agent, security overview docs)
- [ ] Team review and sign-off
