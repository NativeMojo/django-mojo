# Admin LLM Assistant

**Type**: request
**Status**: open
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
