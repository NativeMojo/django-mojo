# Assistant Context References

**Type**: request
**Status**: open
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
