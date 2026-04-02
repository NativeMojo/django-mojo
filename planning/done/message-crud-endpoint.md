# Separate CRUD endpoint for assistant messages

**Type**: request
**Status**: rejected
**Date**: 2026-04-02
**Priority**: low

## Description

Give Message its own read-only REST endpoint (`GET /api/assistant/conversation/message`) instead of serving messages nested inside the Conversation detail graph. This would enable independent pagination, polling for new messages, and per-message operations.

## Rejection Reasons

1. **Conversations are bounded** — `LLM_ADMIN_MAX_HISTORY` caps at 50 messages by default. The nested detail graph handles this volume fine in a single response.
2. **No current use case** — there is no need to list messages across conversations, paginate within a conversation, or operate on individual messages.
3. **Simpler permission model** — messages inherit access from the conversation owner via the detail graph. A separate endpoint would need its own permission chain (owner check through `conversation__user`).
4. **One request vs two** — the UI currently fetches a conversation and its messages in a single `?graph=detail` call. A separate endpoint would require a second request.

## When to Revisit

- Conversations regularly exceed 100+ messages and the detail graph becomes too heavy
- The UI needs to poll for new messages without refetching the full conversation
- Per-message operations are needed (edit, delete, reactions, bookmarks)
