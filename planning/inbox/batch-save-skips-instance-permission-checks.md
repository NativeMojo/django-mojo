---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: REST batch save skips instance-level permission checks (per-row tenant not re-verified)
priority: P3
effort: S
owner:
opened: 2026-07-10
depends_on: []
related: [ITEM-027, ITEM-019]
links: []
---

# REST batch save skips instance-level permission checks

## What & Why
`on_rest_handle_batch` gates once at the **class level** (with `instance=None`),
then loops rows through `update_from_dict` / `create_from_dict`, each of which
calls `self.on_rest_save(...)` with **no per-instance permission check**. So the
batch path skips everything `_evaluate_permission` does with an `instance`:
`check_view_permission` / `check_edit_permission`, the `"owner"` match, and the
per-row group/`GROUP_FIELD` tenant check. A caller who clears the class-level
`SAVE_PERMS` gate for *one* group can then update rows belonging to *other*
tenants in the same batch — the single-instance save path (`on_rest_handle_save`)
re-checks each of these per row; batch does not.

**Latent, not live today:** no framework model sets `RestMeta.CAN_BATCH = True`
(grep is clean), so `on_rest_handle_batch` is currently unreachable for every
shipped model — in particular `Group` (no `CAN_BATCH`), so ITEM-027's
`check_edit_permission` tightening is not undermined. The risk arms the moment
any group-scoped model (here or in a downstream project like Maestro) enables
batch. Fail-closed hygiene says the per-instance gate should hold regardless.

## Acceptance Criteria
- [ ] Each row in a batch create/update is subject to the same instance-level
      permission evaluation as the single-instance path (owner match,
      group/`GROUP_FIELD` tenant membership, `check_view/edit_permission`).
- [ ] A batch that mixes the caller's tenant with a foreign tenant's rows is
      denied (or drops the foreign rows) — never a cross-tenant write.
- [ ] Regression test on a `CAN_BATCH=True` group-scoped test model: a
      member/key scoped to group A cannot update group B's row via batch.
- [ ] Single-instance save/create behavior unchanged; suite green.

## Repro — bugs only
1. On a group-scoped model with `RestMeta.CAN_BATCH = True`, as a user holding
   `SAVE_PERMS` at the GroupMember level for group A only.
2. POST a batch payload containing a row whose id belongs to group B.
- Expected: the group-B row is rejected (per-row tenant check), like the
  single-instance `POST /api/<model>/<B-row-id>` would be.
- Actual: the row is written — batch never runs the per-instance check.

## Investigation
- Surfaced by the ITEM-027 security review (commit 4137760) while tracing every
  caller of `_evaluate_permission`. Confidence: **high** (code read).
- Code path: `MojoModel.on_rest_handle_batch` (mojo/models/rest.py ~:626-656)
  gates once via `rest_check_permission_or_raise(request,
  ["SAVE_PERMS","VIEW_PERMS"])` with **no instance**, then `update_from_dict` /
  `create_from_dict` (~:550-562) call `self.on_rest_save(...)` directly with no
  further gate. Contrast `on_rest_handle_save` (rest.py:462), which passes the
  `instance` so `_evaluate_permission` runs the owner/group/hook checks per row.
- Fix direction (to scope): re-check each row through
  `rest_check_permission(request, ["SAVE_PERMS","VIEW_PERMS"], instance)` inside
  the batch loop (drop-with-audit or fail-the-batch on denial — decide during
  scope), mirroring the FK-attach per-instance gate. Keep create-row handling
  (no instance yet) consistent with `on_rest_handle_create`.
- Regression-test feasibility: needs a `CAN_BATCH=True` group-scoped fixture
  model (none exists in the test project today) — the main scoping cost.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Do NOT enable `CAN_BATCH` on any existing group-scoped model until this is
  fixed — that would arm the latent gap.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
