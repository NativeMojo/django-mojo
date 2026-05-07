# Assistant Skills — REST API Reference

Skills are reusable multi-step procedures the assistant can learn and replay. When a user teaches the assistant a workflow ("remember how to rebuild the reports"), it is stored as a skill and surfaced automatically on future matching requests.

## Overview

- Skills are scoped to a **tier**: `global` (all users), `user` (personal), or `group` (shared with a group).
- Skills contain an ordered list of **steps**. Each step references a tool the assistant will call.
- By default (`auto_execute: false`) the assistant asks the user before running the steps.
- Skills are discovered via natural language — the user does not need to invoke them explicitly.

---

## Endpoints

The `Skill` model is exposed as a standard RestMeta endpoint.

### List Skills

```
GET /api/assistant/skill
```

**Permission**: `view_admin`, `assistant`, or owner

Returns skills accessible to the requesting user. Personal (`tier="user"`) skills are automatically filtered to the requesting user.

**Query parameters**: Standard RestMeta pagination (`limit`, `page`, `order_by`).

**Response**:

```json
{
    "status": true,
    "data": [
        {
            "id": 1,
            "tier": "user",
            "name": "rebuild sales reports",
            "description": "Finds failed report jobs and retries them",
            "auto_execute": false,
            "is_active": true,
            "created": "2026-04-07 10:00:00+00:00",
            "modified": "2026-04-07 10:00:00+00:00"
        }
    ]
}
```

The default graph omits `triggers` and `steps`. Use `?graph=detail` to include them.

---

### Get Skill (detail)

```
GET /api/assistant/skill/<id>?graph=detail
```

**Permission**: `view_admin`, `assistant`, or owner

**Response**:

```json
{
    "status": true,
    "data": {
        "id": 1,
        "tier": "user",
        "name": "rebuild sales reports",
        "description": "Finds failed report jobs and retries them",
        "triggers": ["rebuild sales reports", "regenerate monthly reports"],
        "steps": [
            {
                "tool": "query_jobs",
                "description": "Find failed report jobs",
                "params": {"status": "failed"}
            },
            {
                "tool": "retry_job",
                "description": "Retry the failed job"
            }
        ],
        "auto_execute": false,
        "is_active": true,
        "metadata": {},
        "created": "2026-04-07 10:00:00+00:00",
        "modified": "2026-04-07 10:00:00+00:00",
        "user": {"id": 5, "username": "alice", "email": "alice@example.com"}
    }
}
```

---

### Create / Update Skill

Skills are created and updated through the assistant conversation, not via direct REST POST. Use the `save_skill` assistant tool in conversation (see [Skills in Conversations](#skills-in-conversations)).

If you need to manage skills directly via REST for admin tooling, `POST /api/assistant/skill` and `PUT /api/assistant/skill/<id>` are available. `SAVE_PERMS` requires `view_admin`.

---

### Delete Skill

```
DELETE /api/assistant/skill/<id>
```

**Permission**: `view_admin` or owner

**Response**:

```json
{"status": true}
```

**Error** (not owner — HTTP 404):

```json
{"status": false, "error": "not found"}
```

---

## Skills in Conversations

Skills surface through the normal assistant conversation flow. No special client-side handling is needed.

### Teaching a Skill

The user describes a workflow and asks the assistant to remember it:

> "When I say 'rebuild sales reports', query for failed report jobs in the last 24 hours and retry them. Save this as a skill."

The assistant calls `save_skill` internally. The response will confirm:

> "Done. I've saved 'rebuild sales reports' as a personal skill with 2 steps. Next time you say 'rebuild sales reports' I'll run it."

No `assistant_tool_call` events need special handling for skill tools — they appear the same as any other tool call.

### Editing a Skill

The user can ask the assistant to update part of an existing skill without rewriting the whole thing:

> "Update the 'rebuild sales reports' skill to also run on weekends — add 'rebuild weekend reports' as a trigger."

The assistant calls `update_skill` internally, changing only the fields the user specified. Other fields stay the same. You will see `update_skill` in `tool_calls_made` when this happens.

### Triggering a Skill

When the user sends a message that matches a stored skill's name or trigger phrases, the assistant recognizes it from the skill catalog embedded in its system prompt and calls `find_skill` with the skill's ID to load its steps. If a match is found and `auto_execute` is false, it presents an `action` block:

```json
{
    "type": "action",
    "title": "Run skill: rebuild sales reports",
    "description": "Step 1: Find failed report jobs\nStep 2: Retry the failed job",
    "actions": [
        {"label": "Run", "value": "confirm"},
        {"label": "Cancel", "value": "cancel"}
    ],
    "action_id": "..."
}
```

Send an `assistant_action` WebSocket message when the user clicks a button (see the main [README — Action Blocks](README.md#action-block-interaction-flow)).

If `auto_execute` is true, steps run immediately without the confirmation card.

### Listing Skills

> "What skills do you know?"

The assistant calls `list_skills` and responds with a summary grouped by tier.

### Deleting a Skill

> "Forget the 'rebuild sales reports' skill."

The assistant calls `delete_skill` (which requires user confirmation via an `action` block as it mutates data).

---

## Permission Summary

| Tier | Who can see it | Who can save / delete it |
|---|---|---|
| `global` | Any user with `assistant` permission | Users with `assistant` permission; superusers |
| `user` | The skill's owner; superusers | The owner; superusers |
| `group` | Any member of the group | Group members with `assistant` permission on their membership record |

---

## Tool Calls in `tool_calls_made`

When the assistant works with skills, these tools appear in `tool_calls_made`:

| Tool | When you see it |
|---|---|
| `find_skill` | The assistant loaded a skill by ID from the catalog, or searched by keyword |
| `save_skill` | A skill was created or fully replaced by name |
| `update_skill` | Part of an existing skill was changed (only the specified fields) |
| `list_skills` | The user asked to see available skills |
| `delete_skill` | A skill was deleted |

These are informational — no special client handling is required beyond rendering them in the tool call log if your UI shows it.
