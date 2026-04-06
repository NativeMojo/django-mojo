# Assistant Rich Blocks

**Type**: request
**Status**: resolved
**Date**: 2026-04-06
**Priority**: medium

## Description

Add three new block types to the assistant: `action`, `list`, and `alert`. The current block types (table, chart, stat) cover data display well but leave gaps for common interaction patterns.

## Motivation

- **Confirmation flows are prose-only**: The assistant requires user confirmation before mutating operations (block IP, disable user, retry job) but has no structured way to present the action. The user reads a paragraph and types "yes." An action block with confirm/cancel buttons is faster and less error-prone.
- **Single-record details get shoved into 1-row tables**: Tools like `get_user_detail`, `get_incident`, `get_job_stats` return one object. A table with 1 row and 8 columns is awkward. A key/value list block is the right shape.
- **Warnings blend into narrative**: Permission denials, rate limit alerts, "this IP is already blocked" — these deserve visual distinction (colored banners) not just another paragraph.

## New Block Types

### `action` — Confirmation cards

For mutating operations that need user approval. The frontend renders styled buttons; clicking one sends the choice back as a message.

```json
{
  "type": "action",
  "title": "Block IP",
  "description": "Block 1.2.3.4 on all firewall sets for 24 hours",
  "actions": [
    {"label": "Confirm", "value": "confirm"},
    {"label": "Cancel", "value": "cancel"}
  ]
}
```

Schema:
- `title` (string, required): What the action is
- `description` (string, optional): Details about what will happen
- `actions` (array, required): 1-4 buttons, each with `label` (display text) and `value` (sent back to assistant as user message when clicked)

Frontend behavior:
- Buttons are disabled after one is clicked (no double-submit)
- The clicked value is sent as a user message: e.g., "confirm" or "cancel"
- The block visually indicates which action was taken

### `list` — Key/value detail cards

For single-record summaries: user profiles, incident details, job info, system health.

```json
{
  "type": "list",
  "title": "Incident #42",
  "items": [
    {"label": "Category", "value": "auth:brute_force"},
    {"label": "Priority", "value": 8},
    {"label": "Status", "value": "investigating"},
    {"label": "Events", "value": 23},
    {"label": "Created", "value": "2026-04-06 14:30 UTC"}
  ]
}
```

Schema:
- `title` (string, optional): Header for the card
- `items` (array, required): 1-20 items, each with `label` (string) and `value` (string or number)

### `alert` — Status banners

For warnings, errors, success messages, and informational notices that need visual prominence.

```json
{
  "type": "alert",
  "level": "warning",
  "title": "Rate Limited",
  "message": "User exceeded 100 req/min threshold. Current rate: 142 req/min."
}
```

Schema:
- `level` (string, required): One of `info`, `success`, `warning`, `error`
- `title` (string, optional): Short headline
- `message` (string, required): Detail text

## Implementation

### Backend

1. **`mojo/apps/assistant/services/agent.py`** — Add `action`, `list`, `alert` to `VALID_BLOCK_TYPES`.

2. **`mojo/apps/assistant/services/agent.py`** — Update `SYSTEM_PROMPT` structured data blocks section:
   - Add schema and usage guidance for all three new types
   - `action`: "Use when you need user confirmation before a mutating operation. Always include a cancel option."
   - `list`: "Use for single-record details instead of a 1-row table. Prefer this for user profiles, incident details, job info."
   - `alert`: "Use for warnings, errors, and important notices that need visual distinction from narrative text. Don't overuse — reserve for genuinely important information."

3. **`mojo/apps/assistant/services/agent.py`** — Add `action` block handling in `_parse_blocks()`: when an action block is parsed, tag it with a unique `action_id` (UUID) so the frontend can track which action was clicked. Store the action_id in the block dict.

4. **`mojo/apps/assistant/handler.py`** — Add `assistant_action` to `ASSISTANT_MESSAGE_TYPES`. When the frontend sends back an action response, it includes `action_id` and the chosen `value`. The handler stores this as a user message with metadata indicating it's an action response, then triggers the normal assistant flow.

### Frontend (web developer docs)

5. **Action block renderer**: Styled card with title, description, and button row. Buttons use the `level` or a default style. On click: disable all buttons, highlight the chosen one, send `{"type": "assistant_action", "conversation_id": "...", "action_id": "...", "value": "confirm"}` via WebSocket.

6. **List block renderer**: Card with title and vertical label/value pairs. Labels left-aligned, values right-aligned or below depending on length.

7. **Alert block renderer**: Colored banner — blue (info), green (success), yellow (warning), red (error). Icon + title + message.

### Docs

8. **`docs/web_developer/assistant/README.md`** — Add rendering specs for all three block types, including WebSocket message format for action responses.

9. **`docs/django_developer/assistant/README.md`** — Document new block types, schemas, and when the LLM uses each.

### Tests

10. **`tests/test_assistant/`** — Add block parsing tests:
    - `test_parse_action_block` — valid action block parsed with action_id added
    - `test_parse_list_block` — valid list block parsed
    - `test_parse_alert_block` — valid alert block parsed
    - `test_invalid_block_type_rejected` — unknown type silently dropped
    - `test_action_block_missing_actions_rejected` — action without actions array rejected
    - `test_alert_block_invalid_level_rejected` — alert with bad level rejected

## Resolution

**Status**: resolved
**Date**: 2026-04-06

### What Was Built
Three new block types (action, list, alert) with structural validation, action_id tagging, assistant_action WS message handling, and system prompt guidance.

### Files Changed
- `mojo/apps/assistant/services/agent.py` — Added action/list/alert to VALID_BLOCK_TYPES, _validate_block(), system prompt updates
- `mojo/apps/assistant/handler.py` — Added assistant_action message type routing
- `docs/django_developer/assistant/README.md` — New block type schemas and usage guidance
- `docs/web_developer/assistant/README.md` — Rendering specs, WS message format, action interaction flow
- `docs/web_developer/assistant/blocks.md` — Comprehensive UI implementation guide
- `CHANGELOG.md` — v1.1.14 entry

### Tests
- `tests/test_assistant/16_test_rich_blocks.py` — 14 tests covering all block types, validation, edge cases
- Run: `bin/run_tests --agent -t test_assistant.16_test_rich_blocks`

### Security Review
No concerns — action blocks are informational; actual mutations still require tool execution with permission gates.

### Follow-up
None
