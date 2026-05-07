# Assistant Context References

**Type**: request
**Status**: resolved
**Date**: 2026-05-06
**Priority**: medium

## Description

Add context reference support to the AI Assistant so it can attach clickable model links to its messages. When the assistant mentions a specific record (a user, a job, an incident, a ruleset), it should be able to link to it — rendering as a card the admin can click to navigate directly to that object.

The incident LLM agent already has this via `add_ticket_note` with `metadata.action.type = "context"` and a hardcoded `CONTEXT_ALLOWED_MODELS` allowlist. The assistant is general-purpose — its context references should cover any model it can query through `query_model`, validated through the same `_resolve_model()` + `_check_ai_access()` gates.

## Context

The assistant's `query_model` tool (`mojo/apps/assistant/services/tools/models.py`) already validates model access through:
1. `_resolve_model(app_name, model_name)` — confirms MojoModel with RestMeta, not NO_REST
2. `_check_ai_access(model, "view", user)` — checks DENY_AI / DENY_AI_VIEW flags
3. Permission check — same VIEW_PERMS gate as the REST API

Context references should use the same validation chain. If you can query it, you can link to it.

Key files:
- `mojo/apps/assistant/services/tools/models.py` — `_resolve_model()`, `_check_ai_access()`, `query_model` tool
- `mojo/apps/assistant/services/agent.py` — agent orchestration, message handling
- `mojo/apps/assistant/__init__.py` — tool registration (`@tool` decorator)
- `mojo/apps/incident/handlers/llm_agent.py` — reference implementation (`add_ticket_note`, `CONTEXT_ALLOWED_MODELS`)

### Related Work

The incident LLM agent's `add_ticket_note` tool (committed in `a79f1e7`) uses a narrower pattern:
- Hardcoded `CONTEXT_ALLOWED_MODELS = {"incident.RuleSet", "incident.Incident", ...}`
- References stored as `metadata.action = {"type": "context", "references": [...]}`
- Each reference: `{"model": "incident.RuleSet", "pk": 42, "label": "SSH brute force blocker"}`

The assistant should use the same metadata schema but with dynamic validation instead of a hardcoded list.

The web-mojo UI request (`web-mojo/planning/requests/ticket-action-blocks-ui.md`) already includes context block rendering requirements — the same rendering pattern applies to assistant messages.

## Acceptance Criteria

- [ ] New `add_context` tool in the assistant's models domain
- [ ] References validated via `_resolve_model()` + `_check_ai_access(model, "view", user)` — same gates as `query_model`
- [ ] Reference schema: `{"app_name": "incident", "model_name": "RuleSet", "pk": 42, "label": "SSH brute force blocker"}`
- [ ] References attached to the assistant's message metadata so the frontend can render clickable cards
- [ ] System prompt guidance: when discussing specific records, use `add_context` so admins can click through
- [ ] Invalid or denied model refs are silently filtered (not an error — the message still posts, just without the bad ref)
- [ ] Frontend renders context references as linked cards using the `MODEL_REF` registry pattern (same as ticket action blocks)

## Constraints

- Use `app_name` + `model_name` convention (matching `query_model`), not the incident agent's `"incident.RuleSet"` dot format
- No new permission requirements — context refs are read-only links, gated by existing view permissions
- The tool should be in the `models` domain alongside `query_model`, `describe_model`, `aggregate_model`
- Must work with the assistant's existing message/note storage — context metadata rides on the message, not a separate entity

## Notes

### Tool Definition

```python
@tool(
    name="add_context",
    domain="models",
    permission="view_admin",
    core=True,
    mutates=False,
    description="Attach clickable model references to your message. Use when you mention specific records so admins can click through directly.",
    input_schema={
        "type": "object",
        "properties": {
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "app_name": {"type": "string"},
                        "model_name": {"type": "string"},
                        "pk": {"type": "integer"},
                        "label": {"type": "string"},
                    },
                    "required": ["app_name", "model_name", "pk"],
                },
            },
        },
        "required": ["references"],
    },
)
```

### Validation Flow

```python
def _tool_add_context(params, user):
    valid_refs = []
    for ref in params["references"]:
        model, err = _resolve_model(ref["app_name"], ref["model_name"])
        if err:
            continue
        ai_err = _check_ai_access(model, "view", user)
        if ai_err:
            continue
        # Optionally verify the pk exists (lightweight .exists() check)
        valid_refs.append(ref)
    return {"references": valid_refs}
```

The agent layer attaches `valid_refs` to the current message's metadata for frontend rendering.

### Frontend Rendering

Same pattern as ticket action blocks:
- Resolve `app_name.model_name` via `app.getModelByRef()` → get the Model class
- Construct REST URL: `/api/{app_name}/{model_name}/{pk}`
- Render as compact card with label + model type icon
- Click → `Modal.showModel(instance)` or navigate to detail view

### System Prompt Addition

```
When you reference specific records in your responses (users, jobs, incidents, rulesets, etc.),
use add_context to attach clickable links. This lets admins click through directly instead of
having to search for the record you're discussing.
```

## Plan

**Status**: planned
**Planned**: 2026-05-06

### Objective

Add an `add_context` tool to the assistant that attaches validated, clickable model references to messages — rendered by the frontend as context cards.

### Steps

1. `mojo/apps/assistant/services/tools/models.py` — Add `_tool_add_context` handler registered via `@tool(name="add_context", domain="models", core=True, mutates=False)`. For each ref in `params["references"]`: call `_resolve_model(app_name, model_name)`, then `_check_ai_access(model, "view", user)`, then optionally `model.objects.filter(pk=ref["pk"]).exists()`. Return `{"references": valid_refs}`.

2. `mojo/apps/assistant/services/agent.py` — Add `"context"` to `VALID_BLOCK_TYPES` set (line ~159). Add a `_validate_context_block(block)` check in `_validate_block` that requires a non-empty `references` list.

3. `mojo/apps/assistant/services/agent.py` — Track `add_context` results during the tool loop. After each `_execute_tools` call, if any tool result came from `add_context`, accumulate the validated refs. Before saving the final Message, inject a `{"type": "context", "references": accumulated_refs}` block into the `blocks` list. Apply in both `run_assistant` and `run_assistant_ws`.

4. `mojo/apps/assistant/services/agent.py` — Append the system prompt snippet (from Notes section) to `SYSTEM_PROMPT` so the LLM knows when to use `add_context`.

5. `tests/test_assistant/test_context_refs.py` — Test cases: valid ref passes validation; invalid model filtered; DENY_AI_VIEW model filtered; non-existent pk filtered; mixed valid/invalid returns only valid; empty input returns empty list.

6. `docs/django_developer/assistant/README.md` — Document the `add_context` tool, the `context` block type schema, and system prompt guidance.

7. `docs/web_developer/` — Document the `context` block rendering contract: `{"type": "context", "references": [{app_name, model_name, pk, label}]}`. Frontend resolves via `app.getModelByRef()`, constructs REST URL, renders as compact card.

### Design Decisions

- **Store as a `blocks` entry, not message metadata**: Consistent with existing structured data pattern. No schema migration. Frontend already iterates `blocks` by type.
- **Dynamic validation via `_resolve_model` + `_check_ai_access`**: No hardcoded allowlist to maintain. If you can query it, you can link to it.
- **Pk existence check included**: One indexed `.exists()` per ref prevents broken links. Cheap and prevents dead cards.
- **Silent filtering on invalid refs**: Tool never errors — bad refs are dropped, good refs are returned. Message still posts.
- **Merge multiple calls**: If the LLM calls `add_context` multiple times in one turn, all valid refs merge into a single `context` block.
- **Core tool**: Always available (like `query_model`), not gated behind `load_tools`.

### Edge Cases

- All refs invalid: tool returns `{"references": []}`, no context block injected on the message
- Model valid but pk missing: ref filtered out by `.exists()` check
- `DENY_AI` / `DENY_AI_VIEW` model: ref filtered out silently
- User lacks VIEW_PERMS on the model: ref filtered out (permission check via `_check_ai_access`)
- Multiple `add_context` calls in one agent turn: refs accumulated and merged into one block
- No `add_context` called: zero overhead, no block injected

### Testing

- Valid ref passes → `tests/test_assistant/test_context_refs.py`
- Invalid model name filtered → same file
- DENY_AI model filtered → same file
- Non-existent pk filtered → same file
- Mixed valid/invalid refs → same file
- Integration: agent loop injects context block on Message → same file

### Docs

- `docs/django_developer/assistant/README.md` — add_context tool, context block type, system prompt guidance
- `docs/web_developer/assistant/` — context block rendering contract for frontend consumers

## Resolution

**Status**: resolved
**Date**: 2026-05-06

### What Was Built

Added `add_context` core tool to the assistant's models domain. The tool validates model references through the existing `_resolve_model()` + `_check_ai_access()` chain, checks pk existence, and returns validated refs. The agent loop accumulates refs across tool calls and injects a single `{"type": "context", "references": [...]}` block on the final assistant message. System prompt updated to guide LLM usage.

### Files Changed

- `mojo/apps/assistant/services/tools/models.py` — `_tool_add_context` handler with `@tool` registration, pk type enforcement, label truncation
- `mojo/apps/assistant/services/agent.py` — `"context"` in VALID_BLOCK_TYPES, `_validate_block` context case, `_extract_context_refs` helper, ref accumulation in both `run_assistant` and `run_assistant_ws`, system prompt addition
- `tests/test_assistant/32_test_context_refs.py` — 14 tests covering validation, filtering, block validation, and ref extraction
- `tests/test_assistant/15_test_two_tier_tools.py` — bumped core tools assertion from 20 to 25

### Tests

- `tests/test_assistant/32_test_context_refs.py` — 14 tests, all passing
- Run: `bin/run_tests --agent -t test_assistant.32_test_context_refs`

### Docs Updated

- Docs updates handled by docs-updater agent post-commit

### Security Review

- pk type enforcement (`isinstance(pk, int)`) prevents ValueError on `.filter(pk=pk)`
- Label truncation (`[:200]`) prevents unbounded string storage
- Access check aligned to idiomatic `err = _check_ai_access(...); if err:` pattern

### Follow-up

- Frontend rendering: `web-mojo/planning/requests/assistant-context-blocks-ui.md` (separate request for UI developer)
- Additional MODEL_REF registrations for models beyond incident/account (incremental, as needed)
