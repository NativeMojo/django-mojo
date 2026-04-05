# Assistant Incident Event Reporting

**Type**: request
**Status**: open
**Date**: 2026-04-05
**Priority**: high

## Description

Add `incident.report_event()` calls throughout the assistant for security-relevant actions and error conditions. The assistant currently logs everything to `assistant.log` via `logit` but reports zero events to the incident system. This means the rule engine, auto-blocking, ticketing, and alerting pipeline is completely blind to assistant activity — even when that activity includes permission denials, tool failures, mutating operations on user accounts, and IP blocks.

## Context

The incident system docs say: *"Every part of the framework should treat the incident system as its primary channel for reporting anything security-relevant or operationally significant."* The assistant violates this. It has 40+ tools, including ones that block IPs, disable users, force logouts, modify permissions, and create security rules — all without generating a single event.

Today the assistant has 15+ `logger.info/warning/error/exception` calls that should be `report_event()` calls (or both). The rate limit decorator on the REST endpoint already reports via `incident.report_event()`, but everything inside the agent loop is invisible.

### Logging Hierarchy

The framework has three logging tiers — the assistant currently only uses the lowest:

1. **File logging** (`logit.get_logger()` / `logit.info()`) — debug only, writes to `var/log/`, invisible to the platform. The assistant uses this exclusively today.
2. **Audit logging** (`logit.Log` via `MojoModel.class_logit()` / `self.logit()`) — queryable in DB, visible in admin, used by the assistant's own `query_logs` tool. Some mutating tools in `users.py` already use `class_logit()` but most don't.
3. **Incident events** (`incident.report_event()`) — flows through the rule engine → incidents → handlers (block, ticket, notify, email). Zero usage in the assistant today.

This request adds tier 3 (incident events) and upgrades existing tier 1 calls where appropriate. Existing file logging stays for debug purposes — events supplement, not replace.

**Hard rule: Permission denied must ALWAYS be an incident event.** Permission denials are security signals that need to flow through the rule engine for probing/brute-force detection. A `logger.info("permission denied")` is invisible to the pipeline — it must be `report_event()` at minimum.

### What's at stake

- A compromised admin account using the assistant to block legitimate IPs, disable users, or modify permissions would generate zero security events
- Repeated permission denials through the assistant (probing for tool access) wouldn't trigger brute-force detection rules
- Tool execution failures that might indicate injection or abuse wouldn't surface
- Mutating operations (block_ip, disable_user, force_logout, update_user_permission, create_rule) happen with only a `logit` entry — no event for rule matching, no incident creation, no automated response

## Events to Add

### Permission & Auth (category: `assistant:permission_denied`)

| Trigger | Level | Description |
|---|---|---|
| Tool permission denied | 5 | User attempted a tool they don't have permission for. The LLM shouldn't request filtered-out tools — if this fires, something unusual is happening. |
| Unknown tool requested | 6 | LLM requested a tool not in the registry. Could indicate prompt injection attempting to call arbitrary functions. |
| WS permission denied | 5 | User without `view_admin` attempted to use the assistant via WebSocket. |

### Mutating Tool Execution (category: `assistant:tool:<tool_name>`)

Every mutating tool execution should report an event — not because it's an error, but because these are security-significant actions that should flow through the incident pipeline for audit and rule matching.

| Trigger | Level | Description |
|---|---|---|
| `block_ip` executed | 6 | IP blocked via assistant. Include IP, TTL, reason, requesting user. |
| `disable_user` executed | 6 | User account disabled via assistant. Include target user ID. |
| `enable_user` executed | 5 | User account enabled via assistant. |
| `force_logout` executed | 6 | User sessions invalidated via assistant. Include target user ID. |
| `update_user_permission` executed | 7 | Permission added/removed via assistant. Include target user, permission, action. This is high-level because permission changes are among the most sensitive operations. |
| `create_rule` executed | 6 | Security rule created via assistant. Include rule details. |
| `update_incident` executed | 4 | Incident status changed via assistant. Informational. |
| `create_ticket` executed | 4 | Ticket created via assistant. Informational. |
| `cancel_job` executed | 5 | Job canceled via assistant. |
| `retry_job` executed | 4 | Job retried via assistant. Informational. |
| `create_group` executed | 5 | Group created via assistant. |
| `invite_to_group` executed | 5 | User invited to group via assistant. |

### Error Conditions (category: `assistant:error`)

| Trigger | Level | Description |
|---|---|---|
| Tool execution exception | 6 | A tool handler raised an unhandled exception. Could indicate bad input, injection attempt, or infrastructure failure. Include tool name, sanitized error. |
| Agent loop exception | 7 | The entire agent loop crashed. Include user ID, conversation ID, sanitized error. |
| Max turns exhausted | 5 | Agent hit the turn limit. Could indicate a confused LLM in a loop, or a prompt injection causing infinite tool calls. |
| LLM API auth failure | 7 | API key invalid or expired. Operational — needs attention. |
| LLM API rate limit | 5 | Hit Claude rate limit. Operational. |

### Conversation Activity (category: `assistant:session`)

| Trigger | Level | Description |
|---|---|---|
| Conversation started | 2 | New conversation created. Informational — establishes baseline activity. |
| High tool call volume | 5 | A single conversation used 10+ tool calls. Not necessarily bad, but unusual volume worth tracking. Could trigger rules if sustained. |

## Acceptance Criteria

- Every mutating tool execution generates an event via `incident.report_event()`
- Permission denials in the tool loop generate events (not just logger calls)
- Tool exceptions and agent crashes generate events
- Events include `request` context when available (REST path) and user context always
- Events use consistent category naming: `assistant:permission_denied`, `assistant:tool:<name>`, `assistant:error`, `assistant:session`
- Existing `logger` calls remain (events supplement, not replace, log entries)
- No events for read-only tool calls (would be extremely high volume, low signal)

## Investigation

**What exists**:
- `mojo/apps/assistant/services/agent.py` — 7 logger calls that should also be events (permission denied ×2, unknown tool ×1, tool exception ×2, agent exception ×2)
- `mojo/apps/assistant/handler.py` — 4 logger calls that should also be events (permission denied ×1, agent crash ×1, handler crash ×1, delivery failure ×1)
- `mojo/apps/assistant/services/tools/users.py` — mutating tools (`disable_user`, `enable_user`, `force_logout`, `update_user_permission`) already use `class_logit()` for audit but no `report_event()`
- `mojo/apps/assistant/services/tools/security.py` — `block_ip`, `create_rule`, `update_incident`, `create_ticket` — no event reporting
- `mojo/apps/assistant/services/tools/jobs.py` — `cancel_job`, `retry_job` — no event reporting
- `mojo/apps/assistant/services/tools/groups.py` — `create_group`, `invite_to_group` — no event reporting
- `mojo/decorators/limits.py` — rate limit decorator already reports events (the REST endpoint benefits from this)
- `mojo/apps/incident/__init__.py` — `report_event()` is the public API

**What changes**:
- `mojo/apps/assistant/services/agent.py` — Add `report_event()` calls alongside existing logger calls for permission denials, unknown tools, tool exceptions, agent crashes, max turns
- `mojo/apps/assistant/handler.py` — Add `report_event()` for WS permission denied and handler crashes
- `mojo/apps/assistant/services/tools/security.py` — Add events after successful mutating tool executions
- `mojo/apps/assistant/services/tools/users.py` — Add events after successful mutating tool executions
- `mojo/apps/assistant/services/tools/jobs.py` — Add events after successful mutating tool executions
- `mojo/apps/assistant/services/tools/groups.py` — Add events after successful mutating tool executions

**Constraints**:
- Events must not include sensitive data (passwords, auth keys, API keys) — sanitize before reporting
- Events for successful tool calls should fire AFTER the operation succeeds, not before (don't report events for failed attempts that return error dicts)
- `report_event()` calls must be wrapped in try/except to avoid breaking the assistant if the incident system is down
- Event volume: read-only tools are excluded to keep volume manageable. Only mutating operations, errors, and permission issues generate events.

**Related files**:
- `mojo/apps/incident/__init__.py` — `report_event()` API
- `docs/django_developer/logging/incidents.md` — event patterns and category conventions
- `mojo/decorators/limits.py` — existing pattern for reporting rate limit events

## Category Convention

Following the incident docs naming guidance:

```
assistant:permission_denied    — tool or endpoint permission failures
assistant:tool:block_ip        — mutating tool execution (one per tool)
assistant:tool:disable_user
assistant:tool:enable_user
assistant:tool:force_logout
assistant:tool:update_permission
assistant:tool:create_rule
assistant:tool:update_incident
assistant:tool:create_ticket
assistant:tool:cancel_job
assistant:tool:retry_job
assistant:tool:create_group
assistant:tool:invite_to_group
assistant:error                — tool exceptions, agent crashes, max turns
assistant:error:api            — LLM API failures (auth, rate limit)
assistant:session              — conversation starts, high-volume alerts
```

This enables RuleSets like:
- Bundle `assistant:permission_denied` by `SOURCE_IP`, trigger at 5 in 10 minutes → block + ticket (probing detection)
- Bundle `assistant:tool:update_permission` by `SOURCE_IP`, trigger at 3 in 5 minutes → ticket (rapid permission changes)
- Bundle `assistant:error` by `HOSTNAME`, trigger at 10 in 30 minutes → notify (infrastructure issue)

## Tests Required

- Permission denied events fire when tool is blocked (mock `report_event`, verify called with correct category/level)
- Mutating tool events fire after successful execution
- Events include user ID and tool name in metadata
- Events do NOT fire for read-only tool calls
- Events do NOT fire when mutating tool returns an error dict (operation failed)
- `report_event` failure doesn't break the assistant (try/except)
- Category naming matches convention

## Suggested RuleSets

Include a helper method (like `RuleSet.ensure_ossec_rules()` pattern) to install default assistant rulesets:

| Name | Category | Bundle By | Trigger | Handler |
|---|---|---|---|---|
| Assistant Permission Probing | `assistant:permission_denied` | SOURCE_IP | 5 in 10 min | `ticket://?priority=7&category=security` |
| Assistant Rapid Permission Changes | `assistant:tool:update_permission` | SOURCE_IP | 3 in 5 min | `ticket://?priority=8&category=security,notify://perm@manage_security` |
| Assistant Errors Spike | `assistant:error` | HOSTNAME | 10 in 30 min | `notify://perm@manage_security` |

## Out of Scope

- Events for read-only tool calls (too high volume)
- Events for normal conversation activity beyond session start (avoid noise)
- Changes to the incident system itself
- Dashboard or UI for assistant event monitoring (use existing incident UI)
