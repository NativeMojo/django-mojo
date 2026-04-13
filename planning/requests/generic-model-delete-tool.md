# Generic Model Delete Tool + Delete Rule from RuleSet

**Type**: request
**Status**: planned
**Date**: 2026-04-13
**Priority**: medium

## Description

Two related additions to the AI assistant tool suite:

1. **`delete_rule`** — A targeted tool to delete individual Rule conditions from a RuleSet (the current `delete_ruleset` tool only deletes entire rulesets, not individual child rules).

2. **`delete_model_instance`** — A generic tool that can delete any MojoModel instance, gated by the same permission chain as `on_rest_handle_delete` in `mojo/models/rest.py`:
   - Model must have `RestMeta.CAN_DELETE = True`
   - User must pass `rest_check_permission(request, ["DELETE_PERMS", "SAVE_PERMS", "VIEW_PERMS"], instance)`
   - Calls `instance.on_rest_delete(request)` (which runs `on_rest_pre_delete()` hook inside an atomic transaction)

## Context

The assistant can currently create rules, add conditions, update rulesets, and delete entire rulesets — but cannot remove a single bad rule condition without nuking the whole ruleset. This is the immediate pain point.

More broadly, delete is one of the few CRUD operations missing from the generic model tools. `describe_model` and `query_model` exist in the `models` domain, but there's no generic delete counterpart. Every model that needs delete currently requires a hand-written tool (skills, rulesets, scheduled tasks, etc.). A generic tool eliminates that per-model boilerplate while respecting the same RestMeta permission gates as the REST layer.

## Acceptance Criteria

- [ ] `delete_rule` tool deletes a single Rule by ID, confirms the parent RuleSet exists, returns the remaining rule count
- [ ] `delete_rule` requires `manage_security` permission and `mutates=True`
- [ ] `delete_model_instance` tool accepts `app_name`, `model_name`, and `pk`
- [ ] `delete_model_instance` rejects models without `CAN_DELETE = True`
- [ ] `delete_model_instance` runs the full `rest_check_permission` chain (DELETE_PERMS > SAVE_PERMS > VIEW_PERMS, owner checks, group checks)
- [ ] `delete_model_instance` calls `on_rest_pre_delete()` and deletes inside `transaction.atomic()`
- [ ] `delete_model_instance` has `mutates=True` so the LLM confirms with the user before executing
- [ ] `delete_model_instance` reports a security event on permission denial (same pattern as `query_model`)
- [ ] `delete_model_instance` rejects sensitive-field lookups in any filter/pk resolution path
- [ ] Existing hand-written delete tools (skills, rulesets, scheduled tasks) can optionally remain as convenience shortcuts

## Investigation

**What exists**:
- `delete_ruleset` tool in `mojo/apps/assistant/services/tools/security/rules.py` — deletes entire rulesets but not individual rules
- `query_model` and `describe_model` in `mojo/apps/assistant/services/tools/models.py` — generic read tools with permission checking, owner/group filtering, and security event reporting
- `_resolve_model()`, `_build_request()`, `_report_security_event()` helpers already in `models.py` — reusable for the generic delete tool
- `on_rest_handle_delete()` in `mojo/models/rest.py` (line ~333) — the REST layer's delete permission chain (CAN_DELETE gate + rest_check_permission + on_rest_delete)
- Rule model (`mojo/apps/incident/models/rule.py`) has `CAN_DELETE = True` and `DELETE_PERMS = ["manage_security", "security"]`

**What changes**:
- `mojo/apps/assistant/services/tools/security/rules.py` — add `delete_rule` tool (small, ~30 lines)
- `mojo/apps/assistant/services/tools/models.py` — add `delete_model_instance` tool (~60 lines), reusing existing helpers

**Constraints**:
- Must not bypass RestMeta permission gates — the tool must be at least as strict as the REST layer
- Must call `on_rest_pre_delete()` hook so models with custom pre-delete logic still work
- Must use `transaction.atomic()` for the delete
- The generic tool needs `view_admin` permission as a baseline (same as `query_model`) plus the model's own DELETE_PERMS
- Should not allow deletion of models marked `NO_REST = True`
- Sensitive field names must not leak through error messages

**Related files**:
- `mojo/apps/assistant/services/tools/security/rules.py`
- `mojo/apps/assistant/services/tools/models.py`
- `mojo/apps/assistant/services/tools/__init__.py` (imports, no change needed — auto-registers)
- `mojo/models/rest.py` (reference for permission chain — no changes)
- `mojo/apps/incident/models/rule.py` (reference for Rule model — no changes)

## Tests Required

- `delete_rule` with valid rule ID — rule deleted, remaining count returned
- `delete_rule` with nonexistent rule ID — error returned
- `delete_rule` without `manage_security` permission — rejected
- `delete_model_instance` on a model with `CAN_DELETE = True` — instance deleted
- `delete_model_instance` on a model with `CAN_DELETE = False` or missing — rejected with 403-style error
- `delete_model_instance` with insufficient permissions — rejected, security event reported
- `delete_model_instance` on a model with `NO_REST = True` — rejected
- `delete_model_instance` with owner-scoped permissions — only owner can delete their own instance
- `delete_model_instance` calling `on_rest_pre_delete()` — hook fires before deletion

## Out of Scope

- Batch/bulk delete (too dangerous for an LLM tool)
- Replacing existing hand-written delete tools — they can coexist as domain-specific shortcuts
- Cascade visibility — the tool reports success/failure but doesn't enumerate what cascaded
- Soft-delete support — if a model overrides `on_rest_delete` for soft-delete, it already works via the hook

## Plan

**Status**: planned
**Planned**: 2026-04-13

### Objective

Add a `delete_rule` tool for removing individual rules from rulesets, and a generic `delete_model_instance` tool that mirrors the REST layer's full delete permission chain.

### Steps

1. `mojo/apps/assistant/services/tools/security/rules.py` — Add `delete_rule` tool. Domain: `security`, permission: `manage_security`, `mutates=True`. Accepts `rule_id`, loads the Rule, confirms parent RuleSet exists, deletes the rule, returns `{ok, rule_id, ruleset_id, remaining_rules}`. ~25 lines, same pattern as existing `delete_ruleset`.

2. `mojo/apps/assistant/services/tools/models.py` — Modify `_build_request()` to accept optional `method` and `path` kwargs (currently hardcodes `"GET"` and `"/assistant/query_model"`). Default values unchanged so `query_model` is unaffected.

3. `mojo/apps/assistant/services/tools/models.py` — Add `delete_model_instance` tool. Domain: `models`, permission: `view_admin`, `mutates=True`. Accepts `app_name`, `model_name`, `pk`. Reuses `_resolve_model()` (checks MojoModel, RestMeta, NO_REST). Gates: `CAN_DELETE == True`, then `model.rest_check_permission(request, ["DELETE_PERMS", "SAVE_PERMS", "VIEW_PERMS"], instance)`. Calls `instance.on_rest_delete(request)` and parses the JsonResponse to return a dict. Reports security event on permission denial via `_report_security_event()`.

4. `docs/django_developer/assistant/README.md` — Add `delete_rule` and `delete_model_instance` to the tools listing.

### Design Decisions

- **Call `on_rest_delete(request)` not raw `delete()`**: Some models override `on_rest_delete` for soft-delete or custom cascade logic. Calling the method ensures identical behavior to the REST layer. Parse the returned JsonResponse into a dict for the tool return.
- **`_build_request` gets `method`/`path` kwargs**: Small non-breaking change — default stays `"GET"` / `"/assistant/query_model"` so existing tools are unchanged. Delete tool passes `method="DELETE"`.
- **`view_admin` as base permission**: Same gate as `query_model`/`describe_model`. The model's own DELETE_PERMS are checked separately via `rest_check_permission`. Only admin-level users can attempt the generic tool; RestMeta provides the fine-grained gate.
- **`delete_rule` stays in `security/rules.py`**: Domain-specific shortcut more ergonomic for the LLM than `delete_model_instance(app_name='incident', model_name='Rule', pk=...)`. Both work; targeted tool is more discoverable in the security domain.
- **No `ALT_PK_FIELD` / UUID support**: Tool takes integer `pk` only. The LLM can `query_model` first to find the pk.

### Edge Cases

- **Model without `CAN_DELETE`**: Returns error before any permission check (same as REST 403).
- **`on_rest_delete` override returns error**: Tool parses JsonResponse status — if 400, returns the error dict.
- **Instance not found**: Uses `model.objects.filter(pk=pk).first()` — returns clean error, no 500.
- **`is_request_user` on synthetic request**: `_build_request` passes the real User object which has `is_request_user`, so owner checks work correctly.
- **Sensitive field in error message**: Only `app_name.model_name` and `pk` referenced in errors — no field values leak.

### Testing

- `delete_rule` happy path + missing rule + missing permission → `tests/test_assistant/`
- `delete_model_instance` happy path (CAN_DELETE=True model) → `tests/test_assistant/`
- `delete_model_instance` rejected (CAN_DELETE=False) → `tests/test_assistant/`
- `delete_model_instance` permission denied → `tests/test_assistant/`
- `delete_model_instance` NO_REST model → `tests/test_assistant/`

### Docs

- `docs/django_developer/assistant/README.md` — add `delete_rule` and `delete_model_instance` to the tools listing
- `docs/web_developer/` — no changes (internal assistant tools, not REST endpoints)
