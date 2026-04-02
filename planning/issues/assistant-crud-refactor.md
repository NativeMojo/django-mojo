# Assistant: Replace hand-rolled endpoints with standard CRUD

**Type**: issue
**Status**: planned
**Date**: 2026-04-02
**Priority**: medium

## Description

The assistant conversation endpoints (list, detail, delete) are hand-rolled in `rest/assistant.py` instead of using the framework's standard `on_rest_request` with RestMeta permissions. This bypasses owner-scoping, graph serialization, and the permission system that already handles all of this.

Additionally, the conversation detail endpoint parses structured blocks (`_parse_blocks`) at read time from raw content — this should happen at write time so blocks are stored as a plain JSON field.

## What's Wrong

1. `GET /api/assistant/conversation` — manual queryset filter + manual serialization instead of `on_rest_request`
2. `GET /api/assistant/conversation/<pk>` — manual owner check + manual message serialization + runtime block parsing
3. `DELETE /api/assistant/conversation/<pk>` — manual owner check + manual delete instead of `CAN_DELETE = True`
4. Blocks are parsed from raw content on every read instead of stored once at write time

## Plan

**Status**: planned
**Planned**: 2026-04-02

### Objective

Replace hand-rolled conversation endpoints with standard CRUD using RestMeta permissions, and store blocks as a separate field at write time.

### Steps

1. **`mojo/apps/assistant/models/conversation.py`** — Message model changes:
   - Add `blocks = models.JSONField(default=None, null=True, blank=True)` field on Message
   - Update Message RestMeta graph: `"fields": ["id", "role", "content", "tool_calls", "blocks", "created"]`
   - Update Conversation RestMeta:
     - `VIEW_PERMS = ["view_admin", "owner"]`
     - `CAN_DELETE = True`
   - Add `detail` graph on Conversation: `"fields": ["id", "title", "created", "modified", "messages"]` with `"graphs": {"messages": "default"}`

2. **`mojo/apps/assistant/services/agent.py`** — Store blocks at write time:
   - In `run_assistant()` where the final assistant Message is created (around line 260), call `_parse_blocks()` before saving
   - Store `content=clean_text` and `blocks=blocks_list` on the Message
   - Same change in `run_assistant_ws()` (around line 415)
   - The response dict still returns `response` and `blocks` as before

3. **`mojo/apps/assistant/rest/assistant.py`** — Replace hand-rolled endpoints:
   - Keep `POST /api/assistant` (the message-send endpoint) — this is custom
   - Replace `GET conversation`, `GET conversation/<pk>`, `DELETE conversation/<pk>` with:
     ```python
     @md.URL('conversation')
     @md.URL('conversation/<int:pk>')
     @md.uses_model_security(Conversation)
     def on_conversation(request, pk=None):
         return Conversation.on_rest_request(request, pk)
     ```
   - Remove `on_list_conversations`, `on_get_conversation`, `on_delete_conversation`, `_get_conversation_detail`

4. **`bin/create_testproject`** — Run to generate migration for the new `blocks` field

5. **`tests/test_assistant/2_test_conversations.py`** — Update tests:
   - Verify list returns only owner's conversations via standard CRUD
   - Verify detail with `?graph=detail` includes messages with blocks
   - Verify delete works for owner, denied for non-owner
   - Verify blocks are stored on Message at write time

6. **`tests/test_assistant/3_test_live_assistant.py`** — Update live tests:
   - `test_rest_conversation_history_persisted` — use `?graph=detail` for message list
   - `test_rest_conversation_history_includes_blocks` — verify blocks field on messages

### Design Decisions

- **Store blocks at write time**: Parse once, read many. No runtime parsing on detail endpoint. The `content` field stores clean text (fences stripped), `blocks` field stores the parsed JSON array.
- **`VIEW_PERMS = ["view_admin", "owner"]`**: Admin users see all conversations. Non-admin owners see only their own. The framework handles the list filtering automatically via `on_rest_handle_list`.
- **`CAN_DELETE = True`**: Enables DELETE via standard CRUD. Permission falls through to VIEW_PERMS which includes `"owner"`, so owners can delete their own conversations.

### Edge Cases

- **Existing messages without blocks field**: Migration adds nullable field, existing messages get `blocks=None`. No backfill needed — old messages just don't have blocks.
- **Messages with no blocks**: `blocks` stays `None` (not empty list). The graph serializer includes it as `null`.

### Testing

- Standard CRUD owner scoping → `tests/test_assistant/2_test_conversations.py`
- Blocks stored at write time → `tests/test_assistant/3_test_live_assistant.py`

### Docs

- `docs/web_developer/assistant/README.md` — Update conversation detail response format (blocks on messages, graph param)
- `docs/django_developer/assistant/README.md` — Note blocks field on Message model
