# Conversation and Message REST responses missing user attribution

**Type**: bug
**Status**: open
**Date**: 2026-04-06
**Severity**: high

## Description
The assistant Conversation model has a `user` ForeignKey and proper `OWNER_FIELD` security, but the REST graphs never expose the user in API responses. The list endpoint returns conversations with no user info — admins who can see all conversations have no way to tell who owns what. The detail graph has a `"user": "basic"` sub-graph configured but `user` is not in the `fields` array so it never renders. Additionally, the Message model has no `user` field at all, so there is no per-message user attribution.

## Context
When multiple users use the assistant, admins viewing conversation lists see a flat list with no ownership info. This makes it impossible to audit or manage conversations from an admin perspective. The security filtering works (regular users only see their own), but the lack of visible user data is confusing and operationally useless for admins.

## Acceptance Criteria
- Conversation list (default graph) includes `user` with at least id and display name
- Conversation detail graph includes `user` (fix: add `"user"` to the detail fields list — the sub-graph config is already there)
- Both graphs render the user object in API responses
- No changes to security filtering (owner scoping already works correctly)

## Investigation
**Likely root cause**: REST graph field lists omit `user`

**Confidence**: confirmed

**Code path**:
- `mojo/apps/assistant/models/conversation.py:26-33` — GRAPHS config: default has no user field, detail has the sub-graph but user is missing from fields list
- `mojo/apps/assistant/models/conversation.py:8` — user FK exists on the model
- `mojo/apps/assistant/models/conversation.py:63-69` — Message RestMeta GRAPHS: no user field on model or graph

**Regression test**: not feasible — requires running server with auth context

**Related files**:
- `mojo/apps/assistant/models/conversation.py` — add user to both graph field lists, consider adding user FK to Message model
