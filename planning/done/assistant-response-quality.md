# Assistant Response Quality: Markdown Condensing & Duration Tracking

**Type**: request
**Status**: done
**Date**: 2026-04-06
**Priority**: high

## Description

Two improvements to assistant response output quality:

1. **Markdown condensing** — LLM responses often have excessive whitespace (extra blank lines between paragraphs, list items, table rows) that bloats the UI. Markdown tables break visually because of extra blank lines between rows. When the response also includes structured blocks (e.g., a `table` block), the same data often appears as a markdown table in the text — this duplication wastes space and confuses users.

2. **Request duration tracking** — Track how long assistant requests take end-to-end. Store `duration_ms` on the final assistant Message so the frontend can display it. During streaming (WS), the UI handles live elapsed time, but once the response arrives the backend's measured duration becomes the source of truth.

## Context

Screenshots show: (a) markdown tables with extra blank lines between rows causing render failures, (b) excessive vertical spacing between paragraphs/bullets, (c) a markdown table duplicating data already present in an `assistant_block` table.

The system prompt (line 202 of agent.py) already tells the LLM "Do NOT repeat the data that is already in the blocks" — but LLMs don't always comply. A backend condenser can catch what the prompt cannot.

## Acceptance Criteria

### Markdown Condensing
- Consecutive blank lines in assistant text are collapsed to at most one blank line (currently allows two — `\n{3,}` → `\n\n`)
- Markdown tables with blank lines between rows are repaired (blank lines within `|...|` sequences are removed)
- When a `table` block is extracted AND the remaining text contains a markdown table with the same title or matching column headers, the markdown table is stripped from the text
- Processing happens in `_parse_blocks` / a new `_condense_markdown` step before the text is stored

### Duration Tracking
- `duration_ms` integer field added to `Message` model (nullable, only set on final assistant response messages)
- `run_assistant` and `run_assistant_ws` record `time.time()` at start, compute duration at end, store on the final Message
- Duration included in WS `assistant_response` payload and REST response as `duration_ms`
- Existing messages without duration remain valid (null)

## Investigation

**What exists**:
- `_parse_blocks()` in `agent.py:78-99` already strips block fences and collapses `\n{3,}` → `\n\n`
- System prompt line 202 instructs LLM not to repeat block data in text (not always followed)
- `Message` model has no duration field currently
- WS response already includes `message_id` and `created` but no duration

**What changes**:
- `mojo/apps/assistant/services/agent.py` — add `_condense_markdown()` function, call it in `_parse_blocks`; add timing to `run_assistant` and `run_assistant_ws`
- `mojo/apps/assistant/models/conversation.py` — add `duration_ms` field to `Message`
- `mojo/apps/assistant/handler.py` — include `duration_ms` in WS response payload
- `mojo/apps/assistant/rest/assistant.py` — include `duration_ms` in REST response
- Migration file for the new field

**Constraints**:
- Condensing must not corrupt valid markdown (code blocks, block quotes)
- Table deduplication should be conservative — only strip when there's a clear match (same title or 2+ matching column names)
- Duration must not slow down the response path (just `time.time()` bookends)

**Related files**:
- `mojo/apps/assistant/services/agent.py`
- `mojo/apps/assistant/models/conversation.py`
- `mojo/apps/assistant/handler.py`
- `mojo/apps/assistant/rest/assistant.py`
- `tests/test_assistant/16_test_rich_blocks.py`

## Tests Required

- Markdown with 3+ consecutive blank lines is collapsed to single blank line
- Markdown table with blank lines between rows is repaired
- Duplicate markdown table removed when matching `table` block exists
- Non-table markdown (code blocks, blockquotes) is not corrupted by condensing
- `duration_ms` is populated on final assistant Message after a successful request
- `duration_ms` appears in WS response payload
- `duration_ms` is null for non-final messages (tool_use, tool_result, user)

## Out of Scope

- Frontend changes (UI rendering of duration, markdown rendering fixes)
- Changing the system prompt further (already instructs against duplication)
- Condensing tool result content (only assistant response text)
