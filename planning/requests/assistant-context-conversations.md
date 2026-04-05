# Assistant Context Conversations for Tickets and Incidents

**Type**: request
**Status**: done
**Date**: 2026-04-05
**Priority**: medium

## Description

Add an API endpoint that creates an assistant Conversation pre-loaded with the full context of a Ticket or Incident. The UI gets an "Open Assistant" button on TicketView and IncidentView that calls this endpoint and opens the assistant with everything the admin needs to continue the work — ticket description, notes, incident history, events, LLM agent analysis — all formatted as the first message in the conversation.

No model changes to Ticket or Incident. No FK. No automated handoff. Just a simple "start a conversation about this thing" API.

## Context

The legacy LLM agent (`incident/handlers/llm_agent.py`) triages incidents autonomously and creates tickets when it needs human input. Today, human interaction with those tickets happens through TicketNotes with an awkward LLM re-invocation loop (`execute_llm_ticket_reply`). The admin assistant is a much better interface for this — it has 40+ tools, real conversation persistence, and a natural chat UX.

Rather than complex integration between the two systems, a simple "Open Assistant" button lets admins jump from a ticket or incident into a full assistant conversation with all the context pre-loaded. The assistant already has all the security tools (`query_incidents`, `get_incident_timeline`, `block_ip`, `update_incident`, `create_ticket`, etc.) so the admin can immediately dig deeper, ask questions, or take action.

This also works for non-LLM tickets — any ticket or incident can be discussed with the assistant.

## Design

### API Endpoint

`POST /api/assistant/context` — Create a conversation pre-loaded with context from a ticket or incident.

**Request**:
```json
{
  "source_type": "ticket",
  "source_id": 123
}
```
or:
```json
{
  "source_type": "incident",
  "source_id": 456
}
```

**Response**:
```json
{
  "status": true,
  "data": {
    "conversation_id": 789
  }
}
```

The endpoint:
1. Loads the ticket or incident (permission check: `view_security`)
2. Builds a context message with full details (see below)
3. Creates a Conversation owned by the requesting user
4. Stores the context as the first `user` message
5. Stores the conversation metadata: `{"source_type": "ticket", "source_id": 123}`
6. Returns the conversation_id — the UI navigates to the assistant with this conversation

The admin then sends their first real message ("what should we do about this?" or "is this IP known for abuse?") and the assistant responds with full tool access.

### Context Message Format

**For Tickets**:
```
I need help with this ticket:

## Ticket #123: Suspicious login pattern from 203.0.113.50
- **Status**: open
- **Priority**: 7
- **Category**: security
- **Created**: 2026-04-05 14:32 UTC
- **Assignee**: admin@example.com
- **Linked Incident**: #456

## Description
[LLM Agent] Detected repeated failed login attempts from 203.0.113.50 targeting multiple accounts...

## Notes (5 entries, newest first)
- [2026-04-05 15:10] admin@example.com: What accounts were targeted?
- [2026-04-05 14:45] [LLM Agent]: Based on my analysis, 12 accounts were targeted...
- [2026-04-05 14:32] System: Ticket created
```

**For Incidents**:
```
I need help with this incident:

## Incident #456
- **Status**: investigating
- **Priority**: 8
- **Category**: ossec:auth
- **Source IP**: 203.0.113.50
- **Hostname**: web-prod-01
- **Created**: 2026-04-05 14:30 UTC
- **RuleSet**: #12 (SSH Brute Force)

## Details
Multiple failed SSH authentication attempts detected...

## LLM Assessment
[If metadata.llm_assessment exists, include it]

## History (10 entries, newest first)
- [2026-04-05 14:45] handler:llm — [LLM Agent] Triage complete: Blocked IP, created ticket...
- [2026-04-05 14:32] handler:block — IP 203.0.113.50 blocked for 3600s
- [2026-04-05 14:30] created — Incident created by RuleSet #12

## Recent Events (up to 10)
- [evt-789] 2026-04-05 14:29 | level=6 | Failed password for root from 203.0.113.50
- [evt-788] 2026-04-05 14:28 | level=6 | Failed password for admin from 203.0.113.50
```

The context is formatted as a user message so the assistant treats it as the conversation's starting point. It's detailed enough that the assistant can immediately reason about the situation without needing to call tools for basic context.

### Duplicate Prevention

If a conversation already exists for this source (check `Conversation.metadata` for matching `source_type` + `source_id` + `user`), return the existing conversation_id instead of creating a new one. This prevents duplicate conversations when the admin clicks "Open Assistant" multiple times.

## Acceptance Criteria

- `POST /api/assistant/context` creates a conversation pre-loaded with ticket or incident context
- Context includes full details: description/details, notes/history, LLM assessment, recent events
- Conversation owned by the requesting user
- Conversation metadata tracks `source_type` and `source_id`
- Duplicate prevention: same user + same source returns existing conversation
- Permission: requires `view_security` (same as viewing tickets/incidents)
- Works for both LLM-linked tickets and regular tickets
- Works for incidents with or without LLM assessment

## Investigation

**What exists**:
- `mojo/apps/assistant/models/conversation.py` — Conversation model with `user`, `title`, `metadata`
- `mojo/apps/assistant/models/conversation.py` — Message model with `role`, `content`
- `mojo/apps/assistant/rest/assistant.py` — existing REST patterns
- `mojo/apps/incident/models/ticket.py` — Ticket + TicketNote models
- `mojo/apps/incident/models/incident.py` — Incident model with `add_history`, `metadata.llm_assessment`
- `mojo/apps/incident/models/history.py` — IncidentHistory model

**What changes**:
- New: `mojo/apps/assistant/services/context.py` — context builder functions: `build_ticket_context(ticket)`, `build_incident_context(incident)`. Loads related objects (notes, history, events) and formats the context message.
- Modified: `mojo/apps/assistant/rest/assistant.py` — add `POST /api/assistant/context` endpoint. Or new file `mojo/apps/assistant/rest/context.py` if cleaner.

**Constraints**:
- Context message must not include sensitive fields (passwords, auth_keys)
- Context message should be bounded — max 10 history entries, max 10 events, truncate long details
- The assistant doesn't need special tools for this — its existing security tools handle everything
- No changes to Ticket or Incident models

**Related files**:
- `mojo/apps/assistant/models/conversation.py`
- `mojo/apps/assistant/rest/assistant.py`
- `mojo/apps/incident/models/ticket.py`
- `mojo/apps/incident/models/incident.py`
- `mojo/apps/incident/models/history.py`

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| `POST` | `/api/assistant/context` | Create context-loaded conversation from ticket or incident | `view_security` |

## Tests Required

- Create conversation from ticket — context includes title, description, notes
- Create conversation from incident — context includes details, history, events, LLM assessment
- Duplicate prevention — same user + same source returns existing conversation_id
- Permission check — user without `view_security` gets denied
- Missing source — returns 404 for nonexistent ticket/incident
- Context truncation — large incidents don't produce unbounded messages
- Conversation metadata tracks source_type and source_id

## Out of Scope

- Automatic conversation creation by the legacy LLM agent (admin initiates manually)
- Changes to Ticket or Incident models (no FK to Conversation)
- Replacing the legacy `execute_llm_ticket_reply` flow (stays as-is for now)
- WebSocket support for context conversations (use existing assistant WS flow after creation)
- Two-way sync between ticket notes and conversation messages
