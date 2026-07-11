---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-032
type: bug
title: REST batch save skips instance-level permission checks (per-row tenant not re-verified)
priority: P3
effort: S
owner: backend
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

### Goal
Every row in a batch create/update passes the same per-instance permission
evaluation as the single-instance save path; denied rows are dropped with an
audit incident and a per-row error entry — never a cross-tenant write.

### Context — what exists
All in `mojo/models/rest.py` unless noted.

- **The buggy handler — `on_rest_handle_batch` (rest.py:616-670).** Reached from
  `on_handle_list_or_create` (rest.py:596-614): POST/PUT/PATCH with a
  `batched` list in `request.DATA` and `CAN_BATCH=True` (line 611). It re-checks
  `CAN_BATCH` defensively (line 628, raises `feature_disabled`/`can_batch_false`),
  gates **once at class level** with no instance:
  ```python
  636  cls.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])
  ```
  then loops:
  ```python
  644  for idx, item in enumerate(batched):
  645      try:
  646          if not isinstance(item, dict):
  647              raise ValueError("Batch item must be an object")
  648          pk = item.get("id") or item.get("pk")
  649          if pk:
  650              instance = cls.objects.filter(pk=pk).first()
  651              if instance:
  652                  instance.update_from_dict(item)
  653              else:
  654                  instance = cls.create_from_dict(item, request=request)
  655          else:
  656              instance = cls.create_from_dict(item, request=request)
  657          results.append(instance)
  658      except Exception as e:
  659          errors.append({"index": idx, "error": str(e)})
  ```
  Response: `{"items": <serialized results>, "count": len(results)}` plus
  `"errors": [{"index": idx, "error": str}]` when any (lines 661-668, via
  `return_rest_response`). Per-row exceptions are demoted to `errors` entries —
  the loop never raises (relevant: a raised `PermissionDeniedException` inside
  the loop would be swallowed into a 200-with-errors, so the fix must NOT rely
  on raising).
- **`update_from_dict` (rest.py:550-552)** and **`create_from_dict`
  (rest.py:554-562)**: general framework helpers, no permission checks, fall
  back to `SYSTEM_REQUEST` (superuser-like, rest.py:32-40) when no active
  request. Used by internal/system flows — do NOT add gates here.
- **The correct template — `on_rest_handle_save` (rest.py:429-463)** passes the
  instance: `cls.rest_check_permission_or_raise(request, ["SAVE_PERMS",
  "VIEW_PERMS"], instance)` (line 462). **`on_rest_handle_create`
  (rest.py:573-594)** gates with `["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"]`,
  no instance (line 592).
- **`rest_check_permission(cls, request, permission_keys, instance=None)`
  (rest.py:354-380)**: pure boolean, emits NO event — docstring says it's for
  callers that recover gracefully (e.g. silently skip an FK attach). The
  `_or_raise` variant (rest.py:382-412) raises `me.PermissionDeniedException`
  and the dispatcher (`mojo/decorators/http.py:_emit_permission_denied_event`)
  emits the incident.
- **`_evaluate_permission` (rest.py:201-352)** with an instance runs: write
  classification (`SAVE_PERMS` in keys → write, per ITEM-027, rest.py:254-258),
  `check_view_permission`/`check_edit_permission` hooks, `"owner"` match via
  `OWNER_FIELD`, and — critically — **binds `request.group` to the row's true
  tenant** (rest.py:286-295: from `GROUP_FIELD` path or `instance.group`) before
  the member-permission check (`request.group.user_has_permission(...)`,
  rest.py:297-314). This binding is a side effect that persists on the request.
- **The precedent to mirror — FK-attach per-instance gate**,
  `on_rest_save_related_field` (rest.py:1410-1474): boolean
  `rest_check_permission(...)` per related instance; on denial calls
  `_report_fk_attach_denied` (rest.py:1386-1408) which emits
  `class_report_incident_for_user(..., event_type="fk_attach_denied", level=2,
  branch=..., ...)` wrapped in `try/except: pass` ("Audit reporting must never
  block a save flow"), then skips. Drop-with-audit, no raise.
- **Incident helpers**: `class_report_incident_for_user` (rest.py:1666-1693),
  `class_report_incident` (rest.py:1695-1734).
- **ITEM-027 consistency** (planning/done/, commit 4137760): passing
  `["SAVE_PERMS","VIEW_PERMS"]` with the instance makes `_evaluate_permission`
  classify the row as a write and route to `check_edit_permission`/owner/group
  exactly like the single-instance path. Use those exact keys.
- **Fixture model for the test — `chat.Room`
  (mojo/apps/chat/models/room.py:22-30)**: direct `group` FK,
  `GROUP_FIELD = "group"`, `OWNER_FIELD = "user"`,
  `SAVE_PERMS = ["manage_chat", "comms", "owner"]`,
  `CREATE_PERMS = ["authenticated"]`. No `CAN_BATCH` (no shipped model sets it).
- **Test constraints**: RestMeta mutation does NOT cross into the testit server
  process — batch on a runtime-mutated model must be tested **in-process** by
  calling `cls.on_rest_handle_batch(req)` with a hand-built request. Template:
  `tests/test_models/feature_disabled_events.py:155-179`
  (`test_can_batch_false_raises_feature_disabled` — builds an `objict` request,
  mutates `RestMeta.CAN_BATCH` at runtime, calls the handler directly).
  Group/member fixture pattern: `tests/test_account/test_group_save_perms.py`
  (ITEM-027's regression — `Group.objects.create`, `grp.add_member(user)`,
  `gm.add_permission(...)`, try/finally cleanup, descriptive assert messages).

### Changes — what to do
1. **`mojo/models/rest.py` — `on_rest_handle_batch`** (the only behavior change):
   - Before the loop: `original_group = getattr(request, "group", None)`.
   - At the top of each iteration: `request.group = original_group` — resets the
     tenant binding a previous row's `_evaluate_permission` left behind, so it
     can't leak into this row's check or save. (For an allowed row, the check
     re-binds `request.group` to the row's tenant before the save — same
     semantics as the single-instance path.)
   - **Update branch** (instance found, before `instance.update_from_dict(item)`):
     ```python
     if not cls.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], instance):
         cls._report_batch_row_denied(request, instance, idx, branch="batch_update")
         errors.append({"index": idx, "error": "permission denied"})
         continue
     ```
   - **Create branches** (both the pk-not-found fallthrough at line 654 and the
     no-pk branch at line 656), before `create_from_dict`:
     ```python
     if not cls.rest_check_permission(request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"]):
         cls._report_batch_row_denied(request, None, idx, branch="batch_create")
         errors.append({"index": idx, "error": "permission denied"})
         continue
     ```
     (Mirrors `on_rest_handle_create:592`; closes the sibling gap where batch
     ignores a stricter `CREATE_PERMS`. Gate once per row — restructure so the
     two create call sites share one check rather than duplicating it.)
   - Use the generic error string `"permission denied"` — do not include model
     details that would leak more than the single-instance path does.
2. **`mojo/models/rest.py` — new classmethod `_report_batch_row_denied(cls,
   request, instance, index, branch)`**, placed next to and mirroring
   `_report_fk_attach_denied` (rest.py:1386-1408): calls
   `cls.class_report_incident_for_user(request, <details>,
   event_type="batch_row_denied", level=2, branch=branch, index=index,
   model_name=..., instance_id=getattr(instance, "pk", None))`, entire body
   wrapped in `try/except: pass`. Boolean check + explicit incident is
   deliberate: `rest_check_permission` is event-free by design, and raising
   inside the loop would be swallowed by the `except Exception` at rest.py:658.
3. **Tests — new file `tests/test_models/batch_row_permissions.py`** (see Tests
   below).
4. **Docs** (see Docs below) + `CHANGELOG.md` security/hardening entry.

### Design decisions
- **Drop-with-audit, not fail-the-whole-batch** (user-approved): rows are
  written sequentially with no transaction, so raising mid-loop cannot undo
  earlier writes — failing the batch wouldn't be atomic anyway; the batch
  response already has per-row `errors` semantics; and it mirrors the FK-attach
  gate precedent exactly. Denied rows: `errors` entry + `batch_row_denied`
  incident (level 2), row skipped, rest of the batch proceeds.
- **Gate lives in the handler, not in `update_from_dict`/`create_from_dict`** —
  those are general helpers used by internal/system flows (`SYSTEM_REQUEST`
  fallback); gating there would break non-REST callers.
- **Exact permission keys copied from the single paths** (`["SAVE_PERMS",
  "VIEW_PERMS"]` + instance for updates; `["CREATE_PERMS", "SAVE_PERMS",
  "VIEW_PERMS"]` for creates) so ITEM-027's write classification and hook
  routing behave identically in batch.
- **Not in scope**: enforcing `CAN_UPDATE`/`CAN_CREATE` flags per row in batch.
  `CAN_UPDATE`'s default/`CAN_SAVE`-alias semantics make that a behavior change
  beyond this bug; `CAN_BATCH=True` is the developer's explicit opt-in to batch
  create+update. If wanted, file separately.

### Edge cases & risks
- **`request.group` leakage between rows**: `_evaluate_permission` binds
  `request.group` to each row's tenant; without the per-iteration reset, a
  denied foreign row's group could poison a subsequent create (e.g. group
  auto-assignment from `request.group`). Handled by snapshot/restore above.
- **Raised `PermissionDeniedException` demotion**: the loop's `except Exception`
  turns any raise into a row error (200-with-errors). The fix intentionally
  never raises for permission denial — boolean check + `continue`.
- **pk-present-but-not-found falls through to create** (existing behavior,
  rest.py:653-654): now gated by the create-row check; unchanged otherwise. No
  new enumeration surface — the denied-row error string is generic.
- **Denied rows must not appear in `results`** (`continue` before
  `results.append`); `count` keeps meaning "successful rows".
- **System/internal flows unaffected**: `SYSTEM_REQUEST.has_permission` returns
  True and the gate is only in the REST batch handler.
- **Audit must never block the batch**: incident helper wrapped in
  `try/except: pass`, same as `_report_fk_attach_denied`.

### Tests
New file `tests/test_models/batch_row_permissions.py`, testit style
(`from testit import helpers as th`, `@th.django_unit_test()`, `def
test_xxx(opts):`, imports inside the function, descriptive assert messages).
**In-process** handler calls (no `opts.client`): build the request as
`feature_disabled_events.py:155-179` does (`objict` with a real `User`,
`DATA` as an objict supporting `.get`), set `Room.RestMeta.CAN_BATCH = True`
inside try/finally that deletes the attribute after.

Fixtures (setup deletes any leftovers first — long-lived DB):
- Groups A and B; `user_a` = member of A only, with **member-level**
  `manage_chat` on A (`gm.add_permission(...)`); `user_b` = member of B, owner
  of B's room.
- `room_a` (group=A, user=user_a), `room_b` (group=B, user=user_b).

Cases:
1. **Cross-tenant update denied (the regression)**: as `user_a`, batch
   `[{"id": room_b.pk, "name": "pwned"}]` → response `errors` has an entry for
   index 0, `room_b.refresh_from_db()` name unchanged, `count == 0`. Fails on
   current code (row gets written), passes with the fix.
2. **Mixed batch**: `[{"id": room_a.pk, "name": "ok"}, {"id": room_b.pk,
   "name": "pwned"}]` → room_a updated, room_b unchanged, exactly one error
   entry (index 1), `count == 1`.
3. **Create row still works**: batch with a no-pk row (CREATE_PERMS is
   `["authenticated"]`) → row created, no errors. Guards against the new create
   gate over-blocking.
4. Confirm `tests/test_models/feature_disabled_events.py`
   (`test_can_batch_false_raises_feature_disabled`) still green.

Run: `bin/run_tests --agent -t test_models.batch_row_permissions`; baseline per
`.claude/rules/build-baseline.md` before any edit.

### Docs
- `docs/django_developer/core/mojo_model.md` — Batch Operations section
  (~lines 528-537): document per-row permission enforcement, denied rows →
  `errors` entries, `batch_row_denied` incident; also the `CAN_BATCH` flag row
  (~line 60) if it describes permissions.
- `docs/django_developer/rest/permissions.md` — add the batch path to the
  permission-flow section (~lines 12-18); note the per-row gate beside the
  FK-attach gate note (~lines 302-306).
- `docs/web_developer/` — grep for a batch endpoint page; if one documents the
  batch response, add the per-row `errors`/permission-denied behavior.
- `CHANGELOG.md` — Unreleased security note: batch save now enforces per-row
  instance permissions (owner/group/hooks), denied rows dropped with audit.

### Open questions
None — drop-with-audit approved by user 2026-07-10.

## Notes
- Do NOT enable `CAN_BATCH` on any existing group-scoped model until this is
  fixed — that would arm the latent gap.
- Baseline (2026-07-10, pre-edit, `bin/run_tests --agent`): status=passed,
  total=2414, passed=2358, failed=0, skipped=56 (+ test_incident/test_security
  opt-in modules skipped). All green — no pre-existing failures.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
