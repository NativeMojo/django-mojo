# Gate updates with a real `CAN_UPDATE` RestMeta flag

**Type**: request
**Status**: planned
**Date**: 2026-04-19
**Priority**: medium

## Description

Introduce a real `CAN_UPDATE` flag in `mojo/models/rest.py` that gates updates to existing instances, mirroring how `CAN_CREATE` gates creates and `CAN_DELETE` gates deletes. Default: `True` (matches today's behavior).

Today `CAN_SAVE` is referenced in many app RestMetas with the clear intent of blocking updates — including `account.LoginEvent` (`CAN_SAVE = False`) and `shortlink.Click` (`CAN_SAVE = False`) — but **`rest.py` never reads the flag**. Updates to those models are not actually blocked. This is a silent permissions gap.

Rename to `CAN_UPDATE` (or `CAN_MODIFY`) for clarity: "save" is ambiguous (save = create + update?), while "update" matches the verb pair `create / update / delete` that already exists for the other two flags.

## Context

Discovered while scoping the assistant `save_model_instance` tool ([assistant_save_model_tool.md](assistant_save_model_tool.md)). When checking what `CAN_x` gates exist for the assistant to honor, found:

- `CAN_CREATE` — gated in `mojo/models/rest.py:415`, defaults `True`.
- `CAN_DELETE` — gated in `mojo/models/rest.py:333`, defaults `False`.
- `CAN_SAVE` — referenced in 8+ RestMetas, **never read by rest.py**.

Models that today set `CAN_SAVE = False` and are silently still updateable:
- `mojo/apps/account/models/login_event.py:46` — login events should be append-only audit records.
- `mojo/apps/shortlink/models/click.py:12` — click tracking should be append-only.

Anyone setting `CAN_SAVE = False` today believes they have hardened the model. They have not.

## Acceptance Criteria

- New `CAN_UPDATE` flag added to `mojo/models/rest.py`, defaulting to `True`.
- Update path (existing-instance save) checks `CAN_UPDATE` before running the permission chain. On `False`: returns the same shape of error as `CAN_DELETE = False` does today.
- Decision recorded on the `CAN_SAVE` name: deprecate in favor of `CAN_UPDATE`, or keep `CAN_SAVE` as the name. Recommendation: use `CAN_UPDATE` (clearer pair with `CAN_CREATE` / `CAN_DELETE`) and treat `CAN_SAVE` as a deprecated alias for one release for backwards compatibility.
- Existing RestMetas migrated:
  - `mojo/apps/account/models/login_event.py` — `CAN_SAVE = False` → `CAN_UPDATE = False`.
  - `mojo/apps/shortlink/models/click.py` — `CAN_SAVE = False` → `CAN_UPDATE = False`.
  - `mojo/apps/filevault/models/data.py`, `mojo/apps/filevault/models/file.py`, `mojo/apps/fileman/models/manager.py`, `mojo/apps/fileman/models/file.py`, `mojo/apps/fileman/models/rendition.py`, `mojo/apps/shortlink/models/shortlink.py` — `CAN_SAVE = True` → `CAN_UPDATE = True` (or remove since `True` is the default).
- Docs updated:
  - `mojo/apps/filevault/DESIGN.md`, `docs/django_developer/filevault/README.md`, `docs/django_developer/shortlink/README.md`.
  - `docs/django_developer/rest/permissions.md` (or wherever RestMeta flags are documented) gains a `CAN_UPDATE` section.
- `CHANGELOG.md` calls out the silent-gap fix and the rename.

## Investigation

**What exists**:
- `mojo/models/rest.py:333` — `CAN_DELETE` gate (the template to follow).
- `mojo/models/rest.py:415` — `CAN_CREATE` gate.
- `mojo/models/rest.py:317` — current update path; only checks the permission chain.

**What changes**:
- `mojo/models/rest.py` — add the `CAN_UPDATE` check on the update path; optionally read `CAN_SAVE` as a deprecated fallback with a `logit.warning` once per process.
- Eight model files listed in Acceptance Criteria — rename the flag.
- Docs in `docs/django_developer/`, `mojo/apps/filevault/DESIGN.md`.
- After this lands, the assistant `save_model_instance` tool will pick up the new gate automatically since it delegates to `on_rest_save` / `rest_check_permission`.

**Constraints**:
- This is a **behavior-changing fix** for `LoginEvent` and `Click`: anyone today PUTting to `/api/account/loginevent/<pk>` or `/api/shortlink/click/<pk>` will start getting denied. That is the intended fix, not a regression. Mention in `CHANGELOG.md` so deployers know.
- Backwards-compat path for the rename: accept either `CAN_UPDATE` or `CAN_SAVE` for one release, prefer `CAN_UPDATE` if both are set.

**Related files**:
- `mojo/models/rest.py`
- All files matching `grep -r CAN_SAVE mojo/`
- `docs/django_developer/rest/permissions.md`
- `CHANGELOG.md`

## Tests Required

- Update with `CAN_UPDATE = False` returns 403/permission-denied even when the user has `SAVE_PERMS`.
- Update with `CAN_UPDATE = True` (or unset, since default is True) follows the existing `SAVE_PERMS` / `VIEW_PERMS` chain.
- Create with `CAN_UPDATE = False` still works (the flag only gates updates, not creates).
- Delete unaffected — still gated by `CAN_DELETE` only.
- Backwards compat: model with only the old `CAN_SAVE = False` still blocks updates (with a deprecation log line).
- `LoginEvent` and `Click`: PUT to existing instance is denied.

## Out of Scope

- Splitting per-field write gates (already handled by `on_rest_save_field`).
- Changing `CAN_CREATE` or `CAN_DELETE` semantics or defaults.
- Adding the assistant `save_model_instance` tool — tracked separately in [assistant_save_model_tool.md](assistant_save_model_tool.md).

## Plan

**Status**: planned
**Planned**: 2026-04-19

### Objective

Add a real `CAN_UPDATE` RestMeta gate to `on_rest_handle_save`, default `True`, treat `CAN_SAVE` as a deprecated alias for one release, and migrate the 8 known call sites so `LoginEvent` and `Click` are actually append-only.

### Steps

1. `mojo/models/rest.py` — at `on_rest_handle_save` (line 306-319), add a `CAN_UPDATE` check before the permission chain, mirroring the `CAN_DELETE` gate at line 333. Read order:
   - If `CAN_UPDATE` is explicitly set (True or False): use it.
   - Else if `CAN_SAVE` is explicitly set: use it, emit a once-per-process `logit.warning` that `CAN_SAVE` is deprecated for this model.
   - Else: default `True` (matches today's behavior for models that set neither).
   - On `False`: return `rest_error_response(request, 403, error=f"UPDATE not allowed: {cls.__name__}")`.
   - Helper `_deprecated_can_save_warned = set()` at module level; helper fn `_warn_can_save_deprecated(cls_name)` adds to the set and emits the warning only the first time per class name. Module-scope state is fine — it resets on process restart and we only need once-per-lifetime signaling.

2. `mojo/apps/account/models/login_event.py` — `CAN_SAVE = False` → `CAN_UPDATE = False`.

3. `mojo/apps/shortlink/models/click.py` — `CAN_SAVE = False` → `CAN_UPDATE = False`.

4. Remove redundant `CAN_SAVE = True` (default is already True — keeping it is noise):
   - `mojo/apps/filevault/models/data.py`
   - `mojo/apps/filevault/models/file.py`
   - `mojo/apps/fileman/models/manager.py`
   - `mojo/apps/fileman/models/file.py`
   - `mojo/apps/fileman/models/rendition.py`
   - `mojo/apps/shortlink/models/shortlink.py`

5. `tests/test_models/__init__.py` (new package) + `tests/test_models/can_update_gate.py` — tests use `@th.django_unit_test()` with monkey-patched `RestMeta` flags on an existing model (e.g. `incident.RuleSet`) so no migrations are needed. Tests cover:
   - `CAN_UPDATE = True` → update succeeds (with perms).
   - `CAN_UPDATE = False` → update denied with "UPDATE not allowed" 403 even when user has SAVE_PERMS.
   - `CAN_UPDATE = False` does not block create (the flag only gates updates).
   - `CAN_UPDATE = False` does not affect delete (CAN_DELETE remains authoritative).
   - Deprecation fallback: only `CAN_SAVE = False` set → update still blocked, deprecation warning logged exactly once per class.
   - Precedence: `CAN_UPDATE = True` + `CAN_SAVE = False` → update allowed (new flag wins).
   - Default behavior: neither flag set → update follows the SAVE_PERMS chain unchanged.

6. `tests/test_account/test_login_event.py` — add a test that a PUT to an existing `LoginEvent` row is denied by the new gate (real-world assertion beyond the synthetic monkey-patch path).

7. `tests/test_shortlink/` — add a test that a PUT to an existing `ShortLinkClick` row is denied. (If `test_shortlink` exists; else skip — the monkey-patched gate test + explicit model-file update is enough coverage.)

8. `docs/django_developer/rest/permissions.md` — add `CAN_UPDATE` to the RestMeta properties table right after `CAN_DELETE`. Add a short note on the `CAN_SAVE` → `CAN_UPDATE` rename and the one-release deprecation window.

9. `docs/django_developer/filevault/README.md` — replace the `CAN_SAVE = True` example with an updated one (either `CAN_UPDATE = True` or drop the line since True is default).

10. `docs/django_developer/shortlink/README.md` — update the line "Read-only via REST (`CAN_SAVE = CAN_CREATE = False`)" to use `CAN_UPDATE`.

11. `mojo/apps/filevault/DESIGN.md` — update the two example blocks that show `CAN_SAVE = True`.

12. `CHANGELOG.md` — v1.1.0 entry under both Added (new gate) and Changed (breaking behavior fix): explicitly call out that PUT to `account.LoginEvent` and `shortlink.Click` will now be correctly denied — this is the intended fix for a silent permissions gap, not a regression.

### Design Decisions

- **Default `True`, not `False`**: matches today's behavior. Defaulting to `False` would silently break every existing RestMeta that doesn't set the flag — the opposite of what this fix is trying to do.
- **`CAN_UPDATE` name over `CAN_MODIFY`**: pairs cleanly with `CAN_CREATE` and `CAN_DELETE`. "Update" is the standard verb for existing-instance mutation.
- **One-release deprecation of `CAN_SAVE`, not hard removal**: avoids breaking any downstream project that inherited this repo's convention. The warning fires once per class per process — informative, not spammy.
- **Redundant `CAN_SAVE = True` flags removed, not renamed**: the default is `True`; keeping the flag is noise with no effect. Dropping is strictly cleaner than switching to `CAN_UPDATE = True`.
- **Gate runs before permission chain**: fail fast. A blocked model shouldn't even walk the perm tree.
- **`_deprecated_can_save_warned` as module-level set**: process-local is sufficient; we don't need cross-process dedup for a deprecation warning. Resetting on restart means deployers re-see the warning after upgrades, which is desirable.
- **Testing via monkey-patch on `RuleSet.RestMeta`**: same pattern used for `DENY_AI_*` tests in `29_test_ai_access_flags.py`. No test-only models or migrations needed.
- **Assistant `save_model_instance`**: inherits the new gate for free via `on_rest_save` → `on_rest_handle_save`. No changes needed in `mojo/apps/assistant/services/tools/models.py`.

### Edge Cases

- **Both `CAN_UPDATE` and `CAN_SAVE` set to different values**: `CAN_UPDATE` wins by precedence rule. Covered in tests.
- **Model inherits from a MojoModel parent that sets `CAN_SAVE`**: `get_rest_meta_prop` reads from the subclass's `RestMeta`, so parent values don't leak unless the subclass's RestMeta inherits from the parent's RestMeta. Today's pattern is `class RestMeta:` (no explicit parent), so inheritance isn't an issue.
- **`CAN_UPDATE = False` with `POST /resource/<pk>`**: POSTing to an existing pk follows the same `on_rest_handle_save` path — correctly blocked.
- **`update_from_dict` and `create_from_dict`** (rest.py:381, 386): these call `on_rest_save` directly, bypassing `on_rest_handle_save`. `CAN_UPDATE` is a REST-level gate, not an ORM-level one — internal callers like these stay unblocked by design. Matches today's behavior for `CAN_CREATE` and `CAN_DELETE`.
- **Deprecation warning in test runs**: tests that monkey-patch `CAN_SAVE` will emit the warning. That's fine — each class name warns once per process, so test noise stays bounded.
- **`CAN_UPDATE = False` on a model with no `SAVE_PERMS` at all**: gate returns 403 before perm chain, so the message is "UPDATE not allowed" rather than a perm-denied. Consistent with how `CAN_DELETE = False` behaves today.

### Testing

- `tests/test_models/can_update_gate.py` — 7 cases as listed in step 5.
- `tests/test_account/test_login_event.py` — append one case: PUT to existing LoginEvent row is 403.
- Full targeted: `bin/run_tests -t test_models -t test_account.test_login_event`.

### Docs

- `docs/django_developer/rest/permissions.md` — RestMeta properties table.
- `docs/django_developer/filevault/README.md` — example update.
- `docs/django_developer/shortlink/README.md` — line update.
- `mojo/apps/filevault/DESIGN.md` — two example blocks.
- `CHANGELOG.md` — Added + Changed entries, with the LoginEvent/Click behavior-change callout.

## Resolution

**Status**: resolved
**Date**: 2026-04-19
**Commits**: 709e08f (main implementation) + 2121e22 (assistant bypass fix + doc follow-ups)

### What Was Built

Real `CAN_UPDATE` gate enforcing append-only behavior on models that want it, enforced at **two layers**:

1. **REST layer** — `mojo/models/rest.py:on_rest_handle_save` checks `CAN_UPDATE` (default `True`) before running the permission chain. On `False`: returns `403` with `error = "UPDATE not allowed: <ModelName>"`.
2. **Assistant layer** — `mojo/apps/assistant/services/tools/models.py:_tool_save_model_instance` re-enforces the same gate in the update branch. The assistant calls `instance.on_rest_save` directly and would otherwise bypass the REST-layer gate; the security-review agent caught this critical bypass.

`CAN_SAVE` is honored as a deprecated one-release alias. Both layers prefer `CAN_UPDATE` when both are set; a once-per-class `logit.warn` fires for any class still using `CAN_SAVE` alone.

### Files Changed

- `mojo/models/rest.py` — new gate + deprecation helper (`_warn_can_save_deprecated` + `_DEPRECATED_CAN_SAVE_WARNED` set).
- `mojo/apps/assistant/services/tools/models.py` — bypass fix in the update branch.
- `mojo/apps/account/models/login_event.py` — `CAN_SAVE = False` → `CAN_UPDATE = False`.
- `mojo/apps/shortlink/models/click.py` — `CAN_SAVE = False` → `CAN_UPDATE = False`.
- Redundant `CAN_SAVE = True` dropped from: `mojo/apps/fileman/models/{manager,file,rendition}.py`, `mojo/apps/filevault/models/{data,file}.py`, `mojo/apps/shortlink/models/shortlink.py`.
- `tests/test_models/__init__.py` + `tests/test_models/can_update_gate.py` — new package, 13 scenarios.
- `docs/django_developer/rest/permissions.md` — new table row + dedicated "CAN_UPDATE" section with deprecation note.
- `docs/django_developer/core/mojo_model.md` — RestMeta properties table updated (docs-updater).
- `docs/django_developer/assistant/README.md` — corrected stale "no separate flag" claim (docs-updater).
- `docs/django_developer/filevault/README.md`, `docs/django_developer/shortlink/README.md`, `mojo/apps/filevault/DESIGN.md` — example updates.
- `CHANGELOG.md` — Added + Changed entries under v1.1.0.

### Tests

- `tests/test_models/can_update_gate.py` — 13 scenarios: gate on/off/default at REST layer, deprecation fallback, once-per-class dedup, `CAN_UPDATE` wins over `CAN_SAVE`, create/delete independence, real-model flag assertions, assistant bypass regression guards (3 cases).
- Run: `bin/run_tests -t test_models`
- Full suite post-commit (709e08f): 1696 passed, 0 failed. No regressions — the two models that gained blocking behavior (`LoginEvent`, `ShortLinkClick`) had no existing PUT tests.

### Docs Updated

- `docs/django_developer/rest/permissions.md` — CAN_UPDATE documented as a first-class RestMeta flag alongside CAN_CREATE and CAN_DELETE, with the deprecation note for CAN_SAVE.
- `docs/django_developer/core/mojo_model.md`, `docs/django_developer/assistant/README.md` — stale references corrected.
- `docs/django_developer/filevault/README.md`, `docs/django_developer/shortlink/README.md`, `mojo/apps/filevault/DESIGN.md` — example blocks updated.
- `CHANGELOG.md` — records both the new gate (Added) and the breaking-fix for LoginEvent/Click (Changed).

### Security Review

One **CRITICAL** finding, addressed in `2121e22`:

- **CRITICAL (resolved)** — Assistant bypass. The original commit enforced `CAN_UPDATE` only in `on_rest_handle_save` (the REST handler layer), but the assistant's `save_model_instance` tool calls `instance.on_rest_save` directly, skipping the gate entirely. An admin using the LLM could have mutated `CAN_UPDATE=False` models even though a direct REST PUT correctly 403s. The fix adds the same check in the assistant tool's update branch; three regression tests guard the bypass.
- **INFO** — `_DEPRECATED_CAN_SAVE_WARNED` set is bounded by installed model count (fixed at startup). No DoS surface.
- **INFO** — `getattr` semantics on inherited `RestMeta`: `get_rest_meta_prop` returns `None` when neither model nor parent sets the flag, which correctly falls through to `CAN_SAVE` and then the default. Inheritance is safe.

All other focus areas (gate ordering, fallback logic, `update_from_dict`/`create_from_dict` as intentionally-unblocked internal callers, error message distinctness, warning dedup) were correctly implemented.

### Follow-up

- **Other models that might benefit from `CAN_UPDATE = False`** — this request migrated only the two known-broken ones. Candidates worth individual review: `incident.Event` (append-only signals), `logit.Log` (audit), `chat.ChatMessage`-style rows in other apps. File per-model requests as owners identify which need the treatment.
- **Remove the `CAN_SAVE` deprecation alias** — scheduled for the next release after v1.1.0 per the one-release deprecation policy. No file yet; track in release notes.
