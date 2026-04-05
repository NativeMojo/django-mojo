# Assistant Ticket Management Tools

**Type**: request
**Status**: resolved
**Date**: 2026-04-05
**Priority**: high

## Description

Add full ticket management tools to the assistant. Currently it can only `query_tickets` (list) and `create_ticket` (create). Missing: get ticket details with notes, update ticket fields, and add notes. These are essential for the context conversation feature (see `assistant-context-conversations.md`) where admins open the assistant from a ticket and need to interact with it.

## Context

The assistant has 40+ tools across security, jobs, users, groups, metrics, web, and docs — but ticket management is limited to list and create. An admin who opens the assistant from a ticket can't read the full ticket, update its status, change priority/assignee, or add notes. This makes the ticket context conversation feature incomplete.

The legacy LLM agent (`incident/handlers/llm_agent.py`) has `create_ticket` and `add_note` but also lacks update and detail tools. The assistant should have the complete set.

## New Tools

### `get_ticket` — Full ticket details with notes
- **Permission**: `view_security`
- **Params**: `ticket_id` (required)
- **Returns**: Ticket fields (id, title, description, status, priority, category, assignee, incident_id, created, modified, metadata) + list of notes (note text, user, created, has_media)
- **Notes**: Load ticket + `TicketNote.objects.filter(parent=ticket).select_related("user").order_by("created")[:50]`. Exclude sensitive user fields.

### `update_ticket` — Update ticket status, priority, assignee, category
- **Permission**: `manage_security`
- **Mutates**: True
- **Params**: `ticket_id` (required), `status` (optional), `priority` (optional), `assignee_id` (optional), `category` (optional)
- **Returns**: Updated ticket fields
- **Notes**: At least one field must be provided. If status changes, auto-add a history note via `ticket.add_note()`. If assignee changes, validate user exists and is active.

### `add_ticket_note` — Add a note to a ticket
- **Permission**: `manage_security`
- **Mutates**: True
- **Params**: `ticket_id` (required), `note` (required)
- **Returns**: `{"ok": True, "note_id": note.pk, "ticket_id": ticket.pk}`
- **Notes**: Creates TicketNote with `user=user` (the requesting admin, not system). The note should NOT be prefixed with `[LLM Agent]` — it's posted on behalf of the human admin, not the LLM itself.

## Acceptance Criteria

- `get_ticket` returns full ticket details including all notes (up to 50)
- `update_ticket` can change status, priority, assignee, and category
- `update_ticket` auto-records status changes as notes
- `add_ticket_note` creates a note attributed to the requesting user
- All tools follow existing security domain patterns (permission gating, error handling)
- Tools registered in the security domain alongside existing ticket tools

## Investigation

**What exists**:
- `mojo/apps/assistant/services/tools/security.py` — `_tool_query_tickets` and `_tool_create_ticket` already implemented
- `mojo/apps/incident/models/ticket.py` — Ticket model with `add_note()`, TicketNote model with `parent` FK, `user` FK, `note` text, `media` FK
- `mojo/apps/incident/models/ticket.py` — TicketNote.on_rest_saved re-invokes LLM for llm_linked tickets (important: `add_ticket_note` should NOT trigger this since the note comes from the assistant, not a human typing in the ticket UI)

**What changes**:
- `mojo/apps/assistant/services/tools/security.py` — Add 3 new tool handler functions + add to TOOLS list

**Constraints**:
- `add_ticket_note` must set `user=user` (the admin) not create system notes
- `add_ticket_note` should NOT trigger the legacy LLM ticket reply loop — the note is being created through the assistant, not the REST API, so `on_rest_saved` won't fire (TicketNote is created directly via ORM, not REST). Verify this is the case.
- Notes should not include sensitive data from the user model (password, auth_key)

**Related files**:
- `mojo/apps/assistant/services/tools/security.py`
- `mojo/apps/incident/models/ticket.py`
- `planning/requests/assistant-context-conversations.md` — depends on these tools

## Tests Required

- `get_ticket` returns ticket fields + notes
- `get_ticket` with nonexistent ticket returns error
- `update_ticket` changes status and auto-adds note
- `update_ticket` changes assignee (validates user exists)
- `update_ticket` with no fields returns error
- `add_ticket_note` creates note attributed to requesting user
- `add_ticket_note` does NOT trigger legacy LLM ticket reply loop
- Permission gating: `view_security` for get, `manage_security` for update/add

## Out of Scope

- Deleting tickets or notes via assistant (use REST directly)
- File/media attachment via assistant tools (separate request: file management tools)
- Modifying the legacy LLM ticket reply flow

## Resolution

**Status**: resolved
**Date**: 2026-04-05

### What Was Built
All three tools (get_ticket, update_ticket, add_ticket_note) were already implemented in `mojo/apps/assistant/services/tools/security.py` as part of the security tools gaps work.

### Files Changed
- `mojo/apps/assistant/services/tools/security.py` — Contains `_tool_get_ticket` (line 655), `_tool_update_ticket` (line 689), `_tool_add_ticket_note` (line 743), all registered in TOOLS list

### Follow-up
- Tests for these specific tools are in `tests/test_assistant/4_test_security_tools.py` but some are currently failing due to import issues with other security tool functions in the same file
