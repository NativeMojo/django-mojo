# Assistant Task Planning

**Type**: request
**Status**: resolved
**Date**: 2026-04-06
**Priority**: high
**Depends on**: assistant-rich-blocks (progress block type)

## Description

Give the assistant the ability to create and execute multi-step plans. Today the LLM calls tools one at a time with no visible structure — the user sees a stream of tool calls and then a final answer. For complex requests ("give me a full security audit", "investigate why this user is having issues"), the assistant should create an explicit plan upfront, show progress as it executes, and let the user see what's happening at each stage.

## Motivation

- **No visibility into complex work**: When the user asks a broad question, the assistant makes 8-15 tool calls with no indication of progress. The user stares at "thinking..." for 30+ seconds with no idea what's happening.
- **No structure for the LLM**: Without a plan, the LLM improvises its approach turn by turn. It sometimes forgets to check something, backtracks, or calls redundant tools. A plan gives it a checklist.
- **Progress block has nothing to render**: The `progress` block type is useless without a plan to show progress against.
- **Foundation for parallel tasks**: The parallel execution feature (separate request) needs a plan to know which steps can run concurrently. Planning is a prerequisite.

## Design

### Plan Storage

Plans are stored as a JSON field on the Conversation model: `conversation.metadata["plan"]`.

```json
{
  "plan_id": "uuid",
  "title": "Security Audit (24h)",
  "steps": [
    {"id": 1, "description": "Check open incidents", "status": "done", "summary": "3 open incidents, 1 critical"},
    {"id": 2, "description": "Review blocked IPs", "status": "in_progress", "summary": null},
    {"id": 3, "description": "Scan failed login attempts", "status": "pending", "summary": null},
    {"id": 4, "description": "Check job failures", "status": "pending", "summary": null},
    {"id": 5, "description": "Summarize findings", "status": "pending", "summary": null}
  ],
  "created": "2026-04-06T14:30:00Z"
}
```

Step statuses: `pending`, `in_progress`, `done`, `skipped`.

### New Tools

**`create_plan`** (core tool, non-mutating)
- Input: `title` (string), `steps` (list of strings)
- Behavior: Creates the plan in `conversation.metadata["plan"]`, returns the plan with IDs assigned
- The agent loop detects this tool and pushes a `assistant_plan` WS event with the full plan
- LLM guidance: "Create a plan when the user's request requires 3+ tool calls across different areas. Don't plan for simple single-tool queries."

**`update_plan`** (core tool, non-mutating)
- Input: `step_id` (int), `status` (string), `summary` (string, optional)
- Behavior: Updates the step in `conversation.metadata["plan"]`, saves, pushes `assistant_plan_update` WS event
- The frontend updates the progress block in real time
- LLM guidance: "Mark each step in_progress before starting it, done when complete with a one-line summary. Skip steps that turn out to be unnecessary."

### Progress Block

The `progress` block type renders the plan visually. The LLM includes it in its response, but the frontend can also render it from WS events independently.

```json
{
  "type": "progress",
  "plan_id": "uuid",
  "title": "Security Audit (24h)",
  "steps": [
    {"id": 1, "description": "Check open incidents", "status": "done", "summary": "3 open, 1 critical"},
    {"id": 2, "description": "Review blocked IPs", "status": "in_progress", "summary": null},
    {"id": 3, "description": "Scan failed logins", "status": "pending", "summary": null},
    {"id": 4, "description": "Check job failures", "status": "pending", "summary": null},
    {"id": 5, "description": "Summarize findings", "status": "pending", "summary": null}
  ]
}
```

Frontend rendering: vertical step list with status icons (checkmark, spinner, circle, skip). Done steps show their summary. A progress bar or fraction at the top ("3 of 5 complete").

### WS Events

- `assistant_plan`: Full plan created — frontend renders the progress block
- `assistant_plan_update`: Single step status change — frontend updates in place without re-rendering the whole block

### Agent Loop Changes

In `run_assistant()` and `run_assistant_ws()`:

1. After executing `create_plan`, store the plan in conversation metadata and push `assistant_plan` WS event.
2. After executing `update_plan`, update the step in metadata and push `assistant_plan_update` WS event.
3. Both tools are handled as meta-tools (like `load_tools` in two-tier) — detected by name after execution, with side effects in the agent loop.

### System Prompt Additions

Add to the system prompt:

```
## Task Planning

For complex requests that require 3+ tool calls across different areas, create a plan first:
1. Call create_plan with a title and list of steps
2. For each step: call update_plan(step_id, "in_progress"), do the work, call update_plan(step_id, "done", summary="...")
3. After all steps, synthesize your findings into a final response

Don't plan for simple queries — if the user asks "how many open incidents?" just call the tool directly.

Planning helps the user see what you're doing and gives you a checklist to work through systematically.
```

## Implementation Steps

1. **`mojo/apps/assistant/services/agent.py`** — Add `progress` to `VALID_BLOCK_TYPES`.

2. **`mojo/apps/assistant/services/tools/planning.py`** — New file with `create_plan` and `update_plan` tools. Both `core=True`, permission `view_admin`. Domain: `planning` (or keep as core with no domain).

3. **`mojo/apps/assistant/services/agent.py`** — Add meta-tool handling for `create_plan` and `update_plan` in the tool-call loop (both `run_assistant` and `run_assistant_ws`). Extract to `_handle_plan_tools(conversation, tool_name, tool_input, tool_result, on_event)` helper.

4. **`mojo/apps/assistant/services/agent.py`** — Update system prompt with planning guidance.

5. **`mojo/apps/assistant/handler.py`** — Add `assistant_plan` and `assistant_plan_update` WS event types.

6. **`mojo/apps/assistant/models/conversation.py`** — Ensure `metadata` JSONField exists on Conversation (it should from two-tier tools). Plan lives at `metadata["plan"]`.

7. **Docs**: Update both doc tracks with planning tools, progress block schema, WS events.

8. **Tests**:
   - `test_create_plan` — creates plan, stores in metadata, returns step IDs
   - `test_update_plan_step` — updates step status and summary
   - `test_update_plan_invalid_step` — returns error for nonexistent step ID
   - `test_plan_ws_events` — verify WS events published on create/update
   - `test_progress_block_parsed` — progress block type accepted by `_parse_blocks`
   - `test_no_plan_for_simple_query` — verify the LLM doesn't plan for single-tool requests (integration test, optional)

## Resolution

**Status**: resolved
**Date**: 2026-04-06

### What Was Built
create_plan and update_plan meta-tools (core=True), plan storage in conversation.metadata, progress block type, WS events for plan lifecycle, system prompt planning guidance.

### Files Changed
- `mojo/apps/assistant/services/tools/planning.py` — New file with create_plan and update_plan tools
- `mojo/apps/assistant/services/tools/__init__.py` — Import planning module
- `mojo/apps/assistant/services/agent.py` — _handle_plan_tool(), META_TOOLS set, progress block validation, system prompt updates
- `mojo/apps/assistant/__init__.py` — Added planning domain description
- `docs/django_developer/assistant/README.md` — Planning tools, progress block, WS events
- `docs/web_developer/assistant/README.md` — Plan tracker rendering, WS event handling
- `CHANGELOG.md` — v1.1.14 entry

### Tests
- `tests/test_assistant/17_test_planning.py` — 9 tests covering tool registration, plan creation, step updates, metadata storage, WS events, progress block parsing
- Run: `bin/run_tests --agent -t test_assistant.17_test_planning`

### Security Review
No concerns — planning tools are read-only metadata operations gated by view_admin permission.

### Follow-up
None
