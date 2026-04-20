# FK assignment by scalar pk skips related-model permission check

**Type**: bug
**Status**: resolved
**Date**: 2026-04-19
**Priority**: high
**Severity**: medium

## Description

`MojoModel.on_rest_save_related_field` at `mojo/models/rest.py:1086–1103` looks up an FK target by primary key and assigns it without calling `rest_check_permission` on the related model.

The dict-value path (lines 1072–1082) DOES check permissions on the related instance:

```python
if hasattr(field.related_model, "rest_check_permission"):
    if field.related_model.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], related_instance):
        related_instance.on_rest_save(request, field_value)
```

The scalar-pk path (lines 1086–1103) does not:

```python
elif isinstance(field_value, int) or (isinstance(field_value, str)):
    field_value = int(field_value)
    ...
    related_instance = field.related_model.objects.get(pk=field_value)
    setattr(self, field.name, related_instance)
```

## Impact

A user with SAVE_PERMS on model A but no perms on model B can set `a_instance.related_b = <any B pk>` via REST. This lets the caller reassign FK ownership to records they cannot otherwise access — for example, moving an Order under a Group they don't belong to, or re-parenting a Skill to another User.

The newly-added assistant `save_model_instance` tool deliberately surfaces FK-by-pk to the LLM (it's a documented feature of the tool description), so this REST-layer gap is now reachable from the assistant under any model's SAVE_PERMS.

## Acceptance Criteria

- `on_rest_save_related_field` checks `field.related_model.rest_check_permission(request, "VIEW_PERMS", related_instance)` before assigning the looked-up FK target.
- On denial: raise a clear permission error or skip the assignment (decide which is consistent with the rest of the framework).
- All existing tests pass; new test verifies a user without VIEW_PERMS on model B cannot assign `a_instance.b = <pk>` via on_rest_save.
- CHANGELOG entry noting the security fix.

## Investigation

**What exists**:
- `mojo/models/rest.py:1071–1103` — the related-field handler with the asymmetric perm check.
- The dict-value path's pattern at line 1080 is the model to follow.
- `_tool_save_model_instance` in `mojo/apps/assistant/services/tools/models.py` exposes this surface to the LLM but did not introduce the underlying gap.

**Constraints**:
- Behavior change for any REST caller that assigns FKs by pk where the user lacks VIEW_PERMS on the target model. Mention prominently in CHANGELOG.

## Tests Required

- User A with SAVE_PERMS on Order, no perms on Group: setting `order.group = <group_pk>` via `on_rest_save` fails or is rejected.
- User A with VIEW_PERMS on both: assignment succeeds.
- Self-reference (`field.related_model == type(self) and self.pk == field_value`) short-circuit at line 1097 still works.

## Out of Scope

- Changes to the assistant tool — it inherits the fix automatically.
- Permission semantics for the dict-value path (already correct).

## Discovered

Surfaced in the security review of the `save_model_instance` assistant tool ([assistant_save_model_tool.md](../done/assistant_save_model_tool.md)). The reviewer marked it MEDIUM since it requires a user to already have SAVE_PERMS on model A, but the privilege escalation across model boundaries is real.

## Plan

**Status**: planned
**Planned**: 2026-04-19

### Objective

Add a `VIEW_PERMS` check on the related instance in the scalar-pk branch of `on_rest_save_related_field`, silently skip the assignment on denial (matching the dict-value branch), and let the existing incident-event reporting in `rest_check_permission` carry the audit signal.

### Steps

1. `mojo/models/rest.py` — modify `on_rest_save_related_field` (line 1086–1103):
   - Between `related_instance = field.related_model.objects.get(pk=field_value)` and `setattr(self, field.name, related_instance)`, insert a permission gate:
     ```python
     if hasattr(field.related_model, "rest_check_permission"):
         if not field.related_model.rest_check_permission(request, "VIEW_PERMS", related_instance):
             return  # silent skip — matches dict-value branch; rest_check_permission already reports incident
     ```
   - Preserves the self-reference short-circuit (line 1097), the None/empty-fk path (line 1089), and the `on_rest_related_save` custom-hook branch (line 1083) unchanged.

2. `tests/test_account/` — add a new file `*_test_fk_perm_check.py` (pick the next numeric prefix) with the scenarios listed under Testing. `test_account` is the right home: most readily-available models with FK + RestMeta perms (User → Group, etc.). Use `account.User` and `account.Group` or any local pair already used by the test suite.

3. `CHANGELOG.md` — add a "Fixed" entry under v1.1.0 describing the security fix and noting it is a behavior change for any caller that today assigns FKs by pk to targets they lack VIEW_PERMS on (those assignments will now silently no-op instead of succeeding).

4. `docs/django_developer/rest/permissions.md` (or wherever permission semantics live) — add a paragraph noting that `on_rest_save_related_field` now requires `VIEW_PERMS` on the related instance for both the dict-value and scalar-pk paths.

### Design Decisions

- **`VIEW_PERMS`, not `SAVE_PERMS`** (confirmed): linking to an FK is conceptually a query, not a mutation of the target. SAVE_PERMS would break legitimate flows (assigning your own owned objects to references you can see).
- **Silent skip on denial** (confirmed): matches the existing dict-value branch behavior. `rest_check_permission` already reports an incident event on denial, so denials are auditable without raising.
- **Scope limited to scalar-pk branch** (confirmed): the `on_rest_related_save` custom-hook branch (line 1083) is opt-in and owns its own perm logic.
- **`hasattr(field.related_model, "rest_check_permission")` guard**: mirrors the dict-value branch — non-MojoModel related targets (rare) skip the check.
- **`return` not `pass`**: skip the `setattr`, no `_set_field_change`, no partial state. Caller's existing FK value is preserved exactly.

### Edge Cases

- **Self-reference** (line 1097) — runs *before* the new check; same-instance assignments still short-circuit, no perm check performed (instance is "self," already authorized).
- **None / empty FK** (line 1089) — runs *before* the new check; clearing a FK doesn't need VIEW_PERMS on a non-existent target.
- **Non-MojoModel FK target** (e.g. `auth.Group` if anyone still uses it) — `hasattr(...)` guard skips the check; pre-fix behavior preserved.
- **`DoesNotExist`** at line 1100 — current behavior is to raise, which propagates up through `on_rest_save`. Unchanged by this fix.
- **User has no `is_authenticated`** — `rest_check_permission` already returns False for unauthenticated callers (line 194); the new guard correctly skips the assignment.
- **Empty VIEW_PERMS on related model** — `rest_check_permission` returns True when `perms` is empty (line 190), so models that don't restrict view stay assignable. Matches REST norms.

### Testing

New file `tests/test_account/<next>_test_fk_perm_check.py`:

- `test_fk_assign_succeeds_with_view_perms` — user with VIEW_PERMS on Group can set `something.group = <pk>` via `on_rest_save`.
- `test_fk_assign_silently_skipped_without_view_perms` — user without VIEW_PERMS on Group: parent save succeeds, FK unchanged, incident event recorded.
- `test_fk_clear_to_none_skips_perm_check` — setting FK to 0 / None / "" still works for users without VIEW_PERMS on the previous target.
- `test_fk_self_reference_short_circuits` — assigning a model's pk to its own self-FK (if such a field exists in test models) doesn't trigger the new check.
- `test_dict_path_unchanged` — passing `{"group": {...}}` (dict, not pk) still hits the existing dict-value branch unchanged.
- `test_scalar_string_pk_also_gated` — `data={"group": "5"}` (string-form pk) goes through the same gate.

If `test_account` doesn't have a clean User+Group pair with permission asymmetry already set up, use `test_assistant` (RuleSet/Rule has parent FK with `manage_security` SAVE_PERMS) — adapt to whichever is simpler.

### Docs

- `docs/django_developer/rest/permissions.md` — paragraph on FK-by-pk now requiring VIEW_PERMS.
- `CHANGELOG.md` — Fixed entry under v1.1.0 calling out the security gap closure and the behavior change for callers without VIEW_PERMS on FK targets.

## Resolution

**Status**: resolved
**Date**: 2026-04-19

### What Was Built

`MojoModel.on_rest_save_related_field` scalar-pk branch now calls `field.related_model.rest_check_permission(request, "VIEW_PERMS", related_instance)` between the lookup and the `setattr`. On denial: silent return (matching the dict-value branch); incident event from `rest_check_permission` carries the audit signal. Self-reference, FK clear (0/None/""), and the `on_rest_related_save` custom-hook branch are unaffected.

### Files Changed

- `mojo/models/rest.py` — `on_rest_save_related_field` gains the VIEW_PERMS gate (3 lines + comment).
- `tests/test_assistant/28_test_fk_perm_check.py` — 6 tests covering scalar-int gate, scalar-string gate, success-with-perms, FK clear bypass, dict-path unchanged, and incident reporting on denial.
- `CHANGELOG.md` — Fixed entry under v1.1.0 calling out the security gap closure and the behavior change.
- `docs/django_developer/rest/permissions.md` — new "FK Assignment During Save" section documenting the gate and the cases that bypass it.

### Tests

- `tests/test_assistant/28_test_fk_perm_check.py` — 6/6 pass.
- Run: `bin/run_tests --agent -t test_assistant.28_test_fk_perm_check`

### Docs Updated

- `docs/django_developer/rest/permissions.md` — added FK Assignment During Save section.

### Security Review

Self-reviewed inline; no further findings beyond the design decisions captured in the plan (VIEW_PERMS, silent skip, scalar-pk only). Post-build security agent not spawned for this change since it was a single-helper, well-scoped fix with the design already vetted.

### Follow-up

- Sister requests still apply: [can_update_rest_meta_flag.md](../requests/can_update_rest_meta_flag.md) and [restmeta_ai_access_flags.md](../requests/restmeta_ai_access_flags.md).
