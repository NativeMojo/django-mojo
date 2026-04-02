# Admin LLM Assistant

**Type**: request
**Status**: planned
**Date**: 2026-04-01
**Priority**: high

## Description

Build an LLM-powered admin assistant that lets administrators query and manage the system through natural language. The assistant has access to security incidents, events, jobs, users, groups, and metrics — all gated by the requesting user's permissions. Think of it as a conversational admin panel: "show me failed jobs in the last hour", "what's the incident trend this week", "which users were rate-limited today".

## Context

Django-mojo already has a focused LLM agent for incident triage (`mojo/apps/incident/handlers/llm_agent.py`) that uses Claude with tool-calling to query events, update incidents, block IPs, and create tickets. This request extends that pattern into a general-purpose admin assistant that covers all admin domains, not just incidents.

The existing chat system provides real-time WebSocket infrastructure with rooms, messages, and moderation. The question is whether to build on chat (conversational UI) or provide a simpler REST endpoint (single request/response).

## Interface Options

### Option A: Chat-Based (Recommended)

Use the existing chat system with a special "assistant" room type. The admin opens a chat room with the AI, sends natural language, and gets streaming responses via WebSocket.

**Pros:**
- Leverages existing chat infrastructure (rooms, messages, WebSocket, history)
- Multi-turn conversation with context
- Real-time streaming responses
- Message history preserved for audit
- Frontend already knows how to render chat

**Cons:**
- More complex to implement (WebSocket integration, streaming)
- Chat system carries overhead (memberships, read receipts) not needed here

### Option B: REST API (Simpler)

A single REST endpoint: `POST /api/admin/assistant` with `{message, conversation_id?}`. Returns the LLM response with structured data.

**Pros:**
- Simple to implement and consume
- Easy to integrate into any admin UI
- Stateless (or lightweight conversation tracking)

**Cons:**
- No streaming (or requires SSE)
- No built-in history/audit trail
- Less conversational feel

### Option C: MCP Server

Expose django-mojo admin capabilities as an MCP (Model Context Protocol) server that external LLM clients (Claude Desktop, Claude Code, etc.) can connect to.

**Pros:**
- Works with any MCP-compatible client
- No need to build UI or manage LLM calls
- Users bring their own LLM subscription

**Cons:**
- Requires separate server process or transport layer
- Permission model is harder (MCP auth vs django-mojo auth)
- No existing MCP infrastructure in the codebase
- Less control over the LLM's behavior and prompts
- Harder to audit what users asked/did

### Recommendation

**Start with Option B (REST)**, with conversation tracking. It's the simplest to build, test, and secure. The same tool-calling backend can later be exposed as a chat room type (Option A) or MCP server (Option C) without rewriting the core logic. The key value is the tool definitions and permission-gated query layer — the transport is secondary.

## Architecture

### Core Components

1. **Tool Registry** — Defines what the LLM can do, organized by domain:
   - `security.*` — Query incidents, events, rules, tickets, IP history
   - `jobs.*` — Query jobs, job events, job logs, retry/cancel
   - `users.*` — Query users, activity, permissions, rate limits
   - `groups.*` — Query groups, members, permissions
   - `metrics.*` — Fetch time-series metrics, system health
   - `system.*` — Server status, error logs, active connections

2. **Permission Gate** — Before executing any tool, check that the requesting user has the required permissions for that domain. Map tool domains to existing permission strings:
   - `security.*` → requires `view_security` or `security`
   - `security.update_*`, `security.block_*` → requires `manage_security` or `security`
   - `jobs.*` → requires `view_jobs` or `jobs`
   - `jobs.cancel`, `jobs.retry` → requires `manage_jobs` or `jobs`
   - `users.*` → requires `view_admin` or `manage_users`
   - `groups.*` → requires `view_groups` or `groups`
   - `metrics.*` → requires `view_admin` or `metrics`

3. **Conversation Manager** — Tracks multi-turn conversations with message history, so the LLM has context across requests.

4. **System Prompt** — Describes the system, available tools, and behavioral guardrails (read-only by default, mutations require confirmation).

### Tool Inventory

#### Security Domain
| Tool | Description | Permission | Mutates? |
|------|-------------|------------|----------|
| `query_incidents` | Filter incidents by status, priority, date range, category | `view_security` | No |
| `query_events` | Filter events by category, IP, hostname, level, date range | `view_security` | No |
| `query_event_counts` | Aggregate event counts by category/IP/time | `view_security` | No |
| `query_tickets` | Filter tickets by status, priority, assignee | `view_security` | No |
| `query_rulesets` | List active rule sets and their configurations | `view_security` | No |
| `query_ip_history` | Look up IP reputation, block history, geo info | `view_security` | No |
| `get_incident_timeline` | Get full history/audit trail for an incident | `view_security` | No |
| `update_incident` | Change incident status, priority, add notes | `manage_security` | Yes |
| `block_ip` | Add IP to blocklist | `manage_security` | Yes |
| `create_ticket` | Escalate to human with description | `manage_security` | Yes |

#### Jobs Domain
| Tool | Description | Permission | Mutates? |
|------|-------------|------------|----------|
| `query_jobs` | Filter jobs by status, channel, func, date range | `view_jobs` | No |
| `query_job_events` | Get event log for a specific job | `view_jobs` | No |
| `query_job_logs` | Get structured logs for a job | `view_jobs` | No |
| `get_job_stats` | Counts by status, avg duration, failure rate | `view_jobs` | No |
| `get_queue_health` | Pending/running counts per channel, stuck jobs | `view_jobs` | No |
| `cancel_job` | Request job cancellation | `manage_jobs` | Yes |
| `retry_job` | Retry a failed job | `manage_jobs` | Yes |

#### Users Domain
| Tool | Description | Permission | Mutates? |
|------|-------------|------------|----------|
| `query_users` | Search/filter users by name, email, status, date | `view_admin` | No |
| `get_user_detail` | Full user profile, permissions, group memberships | `view_admin` | No |
| `get_user_activity` | Recent logins, events, actions for a user | `view_admin` | No |
| `query_rate_limits` | Users currently rate-limited | `view_admin` | No |
| `get_permission_summary` | What permissions a user has and where they come from | `view_admin` | No |

#### Groups Domain
| Tool | Description | Permission | Mutates? |
|------|-------------|------------|----------|
| `query_groups` | Filter groups by name, kind, status | `view_groups` | No |
| `get_group_detail` | Group info, member count, permissions, children | `view_groups` | No |
| `get_group_members` | List members with roles and permissions | `view_groups` | No |
| `get_group_activity` | Recent activity within a group | `view_groups` | No |

#### Metrics Domain
| Tool | Description | Permission | Mutates? |
|------|-------------|------------|----------|
| `fetch_metrics` | Time-series data for given slugs and date range | `view_admin` | No |
| `get_system_health` | Overview: active users, job queue depth, error rates, incident counts | `view_admin` | No |
| `get_incident_trends` | Incident/event trends over time with comparisons | `view_security` | No |

### Permission Enforcement

Every tool call goes through a permission gate before execution:

```
User sends message
  → LLM selects tool + args
  → Permission gate checks: does user have required perm for this tool?
    → No: return "You don't have permission to [action]. Required: [perm]"
    → Yes: execute tool, return results to LLM
  → LLM formats response for user
```

For mutating operations, the LLM should confirm intent before executing:
- "I'll block IP 1.2.3.4 — confirm?" (user says yes → execute)
- Confirmation flow handled via conversation turns

### Settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `LLM_ADMIN_ENABLED` | `False` | Feature flag to enable admin assistant |
| `LLM_ADMIN_MODEL` | `"claude-sonnet-4-6"` | LLM model to use |
| `LLM_ADMIN_API_KEY` | `None` | API key (falls back to `LLM_HANDLER_API_KEY`) |
| `LLM_ADMIN_MAX_TURNS` | `25` | Max tool-calling turns per request |
| `LLM_ADMIN_MAX_HISTORY` | `50` | Max messages to include as conversation context |
| `LLM_ADMIN_SYSTEM_PROMPT` | (built-in) | Override default system prompt |

## Acceptance Criteria

- Admin can send natural language queries and get accurate responses about system state
- Every tool call checks the requesting user's permissions before executing
- Unauthorized tool calls return a clear permission error to the LLM (not a 500)
- Mutating operations (block IP, cancel job, update incident) require explicit confirmation
- Multi-turn conversations maintain context
- Conversation history is stored for audit
- Feature is disabled by default (`LLM_ADMIN_ENABLED = False`)
- Reuses patterns from existing `llm_agent.py` (Claude API, tool-calling, error handling)

## Investigation

**What exists:**
- `mojo/apps/incident/handlers/llm_agent.py` — Full LLM agent with Claude tool-calling, 10 tools, system prompts, memory. Strong pattern to follow.
- `mojo/apps/incident/rest/event.py` — Incident/event REST endpoints with permission checks
- `mojo/apps/jobs/rest/jobs.py` — Job REST with cancel/retry actions
- `mojo/apps/account/rest/user.py`, `group.py` — User/group REST
- `mojo/apps/metrics/rest/base.py` — Metrics fetch/record
- `mojo/apps/chat/` — Full chat system (rooms, messages, WebSocket) for future Option A
- Permission system via `RestMeta`, `request.user.has_permission()`, `@md.requires_perms()`

**What changes:**
- New module: `mojo/apps/admin_assistant/` (or `mojo/apps/assistant/`)
  - `tools/` — Tool definitions organized by domain (security, jobs, users, groups, metrics)
  - `services/agent.py` — Core LLM agent (conversation management, tool dispatch, permission gate)
  - `models/conversation.py` — Conversation + message history for audit
  - `rest/assistant.py` — REST endpoint
- New docs in both `docs/django_developer/assistant/` and `docs/web_developer/assistant/`

**Constraints:**
- Must not bypass existing permission model — the LLM acts with the user's permissions, never elevated
- Must not expose sensitive fields (passwords, auth keys, secrets) even to authorized admins
- API key management — requires `LLM_ADMIN_API_KEY` or shared `LLM_HANDLER_API_KEY`
- Rate limiting — LLM calls are expensive; needs per-user throttling
- Tool results should be bounded (paginated, limited) to avoid blowing up LLM context

**Related files:**
- `mojo/apps/incident/handlers/llm_agent.py` — Primary pattern to follow
- `mojo/apps/incident/models/` — Incident, Event, RuleSet, Ticket models
- `mojo/apps/jobs/models/job.py` — Job, JobEvent, JobLog models
- `mojo/apps/account/models/user.py` — User model
- `mojo/apps/account/models/group.py` — Group model
- `mojo/apps/metrics/rest/base.py` — Metrics fetch logic
- `mojo/rest/model_permissions.py` — Permission introspection utilities
- `mojo/decorators/auth.py` — Permission decorators

## Endpoints

| Method | Path | Description | Permission |
|--------|------|-------------|------------|
| POST | `/api/assistant` | Send message, get LLM response | `view_admin` |
| GET | `/api/assistant/conversations` | List user's past conversations | `view_admin` |
| GET | `/api/assistant/conversation/<id>` | Get conversation history | `view_admin` |
| DELETE | `/api/assistant/conversation/<id>` | Delete conversation | `view_admin` + owner |

## Tests Required

- Permission gate blocks unauthorized tool calls (user without `view_security` cannot use `query_incidents`)
- Permission gate allows authorized tool calls
- Mutating tools require `manage_*` permissions, not just `view_*`
- Conversation history is stored and retrievable
- Conversation context is maintained across turns
- Tool results are bounded/paginated (no unbounded queries)
- Sensitive fields (auth_key, passwords) are never included in tool results
- Feature disabled when `LLM_ADMIN_ENABLED = False`
- Rate limiting prevents abuse
- Error handling when LLM API is unavailable

## Out of Scope

- Chat-based interface (Option A) — future enhancement after REST is proven
- MCP server (Option C) — separate initiative
- Custom admin actions beyond what existing APIs support (no new mutations)
- File upload/analysis
- Code execution or shell access
- Modifying the LLM agent's own configuration
- Multi-user shared conversations

## Plan

**Status**: planned
**Planned**: 2026-04-01

### Objective

Build a REST-based LLM admin assistant with permission-gated tools across security, jobs, users, groups, and metrics domains, extensible by external projects via a `register_tool()` API and Django autodiscover pattern.

### App Structure

```
mojo/apps/assistant/
├── __init__.py               # register_tool(), register_tools(), get_registry()
├── apps.py                   # AppConfig with autodiscover_modules("assistant_tools")
├── models/
│   ├── __init__.py
│   └── conversation.py       # Conversation + Message models for audit
├── rest/
│   ├── __init__.py
│   └── assistant.py          # REST endpoints (POST message, GET/DELETE conversations)
├── services/
│   ├── __init__.py
│   ├── agent.py              # Core agent loop, permission gate, conversation manager
│   └── tools/
│       ├── __init__.py       # Built-in tool registration (security, jobs, users, groups, metrics)
│       ├── security.py       # query_incidents, query_events, query_event_counts, query_ip_history, etc.
│       ├── jobs.py           # query_jobs, get_job_stats, get_queue_health, cancel_job, retry_job
│       ├── users.py          # query_users, get_user_detail, get_user_activity, query_rate_limits
│       ├── groups.py         # query_groups, get_group_detail, get_group_members
│       └── metrics.py        # fetch_metrics, get_system_health, get_incident_trends
└── migrations/
    └── __init__.py
```

### Steps

1. **`mojo/apps/assistant/__init__.py`** — Public API: `register_tool(name, description, input_schema, handler, permission, mutates=False, domain="custom")`, `register_tools(list_of_dicts)`, `get_registry()`. The registry is a module-level dict: `{name: {definition, handler, permission, mutates, domain}}`. Also expose `get_tools_for_user(user)` that filters tools by the user's permissions (for building the Claude `tools` list).

2. **`mojo/apps/assistant/apps.py`** — AppConfig with `name = 'mojo.apps.assistant'`. In `ready()`, call `autodiscover_modules("assistant_tools")` to import `assistant_tools.py` from every installed app. This lets external projects register tools by dropping an `assistant_tools.py` in any of their apps.

3. **`mojo/apps/assistant/models/conversation.py`** — Two models:
   - `Conversation(models.Model, MojoModel)` — `user` FK, `title` CharField, `metadata` JSONField, `created`/`modified`. RestMeta with `VIEW_PERMS = ["view_admin"]`, `OWNER_FIELD = "user"`.
   - `Message(models.Model, MojoModel)` — `conversation` FK, `role` CharField (choices: user/assistant/tool_use/tool_result), `content` TextField, `tool_calls` JSONField (nullable), `created`. RestMeta with `VIEW_PERMS = ["view_admin"]`.

4. **`mojo/apps/assistant/services/tools/security.py`** — Tool definitions and `_tool_*` implementations for the security domain. Follows `llm_agent.py` patterns: bounded queries (max 50), sensitive fields excluded, results as list-of-dicts. Tools: `query_incidents`, `query_events`, `query_event_counts`, `query_tickets`, `query_rulesets`, `query_ip_history`, `get_incident_timeline`, `update_incident` (mutates), `block_ip` (mutates), `create_ticket` (mutates).

5. **`mojo/apps/assistant/services/tools/jobs.py`** — Tools: `query_jobs`, `query_job_events`, `query_job_logs`, `get_job_stats`, `get_queue_health`, `cancel_job` (mutates), `retry_job` (mutates). Reuses existing `mojo.apps.jobs` manager and services.

6. **`mojo/apps/assistant/services/tools/users.py`** — Tools: `query_users`, `get_user_detail`, `get_user_activity`, `query_rate_limits`, `get_permission_summary`. Excludes `password`, `auth_key`, `onetime_code` from all results (matches User.RestMeta.NO_SHOW_FIELDS).

7. **`mojo/apps/assistant/services/tools/groups.py`** — Tools: `query_groups`, `get_group_detail`, `get_group_members`, `get_group_activity`.

8. **`mojo/apps/assistant/services/tools/metrics.py`** — Tools: `fetch_metrics`, `get_system_health`, `get_incident_trends`. Wraps `mojo.apps.metrics.fetch()` and aggregates cross-domain stats.

9. **`mojo/apps/assistant/services/tools/__init__.py`** — Imports all built-in tool modules and calls `register_tool()` for each. This runs when the assistant app loads, before autodiscover picks up external tools. Defines `PERMISSION_MAP` mapping domain prefixes to required permissions.

10. **`mojo/apps/assistant/services/agent.py`** — Core agent:
    - `run_assistant(user, message, conversation_id=None)` — Main entry point.
    - Checks `LLM_ADMIN_ENABLED` setting (default False).
    - Loads/creates `Conversation`, appends user `Message`.
    - Builds system prompt with available tools (filtered by user perms via `get_tools_for_user`).
    - Calls Claude API with tool-calling loop (max `LLM_ADMIN_MAX_TURNS` iterations, default 25).
    - **Permission gate**: before each tool execution, checks `user.has_permission(tool_permission)`. Returns permission error to LLM (not a 500) if denied.
    - Stores assistant response and tool calls/results as `Message` records.
    - Returns `{response, conversation_id, tool_calls_made}`.
    - Settings: `LLM_ADMIN_ENABLED`, `LLM_ADMIN_MODEL` (default `claude-sonnet-4-6`), `LLM_ADMIN_API_KEY` (falls back to `LLM_HANDLER_API_KEY`), `LLM_ADMIN_MAX_TURNS` (25), `LLM_ADMIN_MAX_HISTORY` (50), `LLM_ADMIN_SYSTEM_PROMPT` (override).

11. **`mojo/apps/assistant/rest/assistant.py`** — Four endpoints:
    - `POST /api/assistant` — Send message, get LLM response. Requires `view_admin`. Rate-limited per user.
    - `GET /api/assistant/conversation` — List user's conversations. Requires `view_admin`.
    - `GET /api/assistant/conversation/<int:pk>` — Get conversation with messages. Requires `view_admin` + owner.
    - `DELETE /api/assistant/conversation/<int:pk>` — Soft-delete conversation. Requires `view_admin` + owner.

12. **`testproject/config/settings/local/__init__.py`** — Add `"mojo.apps.assistant"` to `INSTALLED_APPS`.

13. **Run `bin/create_testproject`** — Generate migrations for the new models.

14. **`tests/test_assistant/__init__.py`** + **`tests/test_assistant/1_test_permissions.py`** — Test permission gate blocks unauthorized tool calls, allows authorized ones, mutating tools require `manage_*`, sensitive fields excluded, feature disabled returns error.

15. **`tests/test_assistant/2_test_conversations.py`** — Test conversation CRUD, message history stored and retrievable, conversation context maintained across turns, owner-only access.

### Design Decisions

- **Extensible via `register_tool()` + autodiscover**: External projects drop `assistant_tools.py` in any app and call `register_tool()`. The assistant's `AppConfig.ready()` auto-imports these modules, same pattern as Django's `admin.py`. No framework code changes needed to add tools.
- **Built-in tools centralized in assistant app for v1**: Built-in tools live in `assistant/services/tools/` rather than scattered across domain apps. Keeps the assistant self-contained and avoids coupling domain apps to the assistant. External projects use the autodiscover path.
- **Permission gate in agent loop, not tool functions**: Single enforcement point. Tool functions are pure data queries. The gate checks `user.has_permission(required_perm)` before dispatching. If permissions change mid-conversation, the next tool call reflects that.
- **Conversation model, not chat rooms**: Lightweight audit trail without chat overhead (memberships, read receipts, WebSocket). Future Option A can bridge conversations to chat rooms.
- **No confirmation flow in v1**: Mutating tools gated by `manage_*` perms. System prompt instructs LLM to confirm intent, but enforcement is permission-based. Keeps implementation simple.
- **Settings fallback chain**: `LLM_ADMIN_API_KEY` → `LLM_HANDLER_API_KEY`. Same pattern as incident agent.
- **Tool results always bounded**: Every query tool caps at 50 results. No unbounded querysets.

### Edge Cases

- **No API key configured**: `run_assistant` returns a clear error dict, REST endpoint returns 503. No 500.
- **LLM returns unknown tool**: Log warning, return `{error: "Unknown tool"}` to LLM, continue loop.
- **User permissions change mid-conversation**: Permission check is per tool call, always current.
- **Sensitive fields**: User tools exclude `password`, `auth_key`, `onetime_code` (matches `NO_SHOW_FIELDS`). Group tools exclude `metadata.secrets` if present.
- **Rate limiting**: `@md.rate_limit` on POST endpoint. Per-user, per-IP.
- **Feature disabled**: If `LLM_ADMIN_ENABLED` is False, POST endpoint returns `{status: False, error: "Assistant is not enabled"}` with 404.
- **External tool registration after ready()**: Tools registered after autodiscover still work — the registry is a live dict, and `get_tools_for_user()` reads it at call time.
- **Duplicate tool names**: `register_tool` raises `ValueError` if name already registered. First-registered wins.

### Testing

- Permission gate blocks unauthorized tool calls → `tests/test_assistant/1_test_permissions.py`
- Permission gate allows authorized tool calls → same file
- Mutating tools require `manage_*` not just `view_*` → same file
- Sensitive fields excluded from user tool results → same file
- Feature disabled returns error → same file
- Conversation CRUD (create, list, get, delete) → `tests/test_assistant/2_test_conversations.py`
- Message history stored and retrievable → same file
- Owner-only conversation access → same file

### Docs

- `docs/django_developer/assistant/README.md` — Architecture, settings reference, tool registry API, how to add custom tools via `assistant_tools.py`, permission mapping, built-in tool inventory
- `docs/web_developer/assistant/README.md` — REST endpoints, request/response format, conversation flow, permissions needed, error responses
