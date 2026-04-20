# FK assignment by scalar pk skips related-model permission check

**Type**: bug
**Status**: open
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
