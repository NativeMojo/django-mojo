---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-036
type: bug
title: ApiKey.set_permissions silently discards non-dict `permissions` payloads instead of parsing or rejecting them
priority: P2
effort: S
owner: backend
opened: 2026-07-12
depends_on: []
related: [ITEM-035]
links: []
---

# ApiKey.set_permissions silently discards non-dict `permissions` payloads instead of parsing or rejecting them

## What & Why
`ApiKey.set_permissions` (`mojo/apps/account/models/api_key.py:166-176`) is the
custom REST setter invoked by `on_rest_save_field` whenever a `permissions` key
is present in a save payload. It opens with `if not isinstance(value, dict):
return` (api_key.py:171) — any non-dict value (most commonly a JSON-encoded
*string*, e.g. `'{"manage_group": true}'`) is silently discarded: the method
returns without raising, without logging, and without touching
`self.permissions`. The caller gets a normal 200 response with the field
unchanged — a silent data-loss shape, not a validation error.

This was discovered via the web-mojo admin GroupView API Key dialog, which
currently POSTs `permissions` as a raw JSON string (a separate, web-mojo-side
bug — see Notes). But the silent-drop itself is a backend robustness gap
independent of that bug: any caller — a differently-behaved future UI, a
direct API integration, a script — that sends a JSON string instead of an
already-parsed object hits the same silent no-op. The model already has a
helper that knows how to parse a string into a dict,
`_get_permissions_dict` (api_key.py:103-113) — `set_permissions` just never
calls it.

Contrast with the sibling fields `metadata`/`limits` (api_key.py:45-46, no
custom setter): they fall through to the generic JSONField merge path
(`on_rest_update_jsonfield`, `mojo/models/rest.py:1547`), which
`json.loads`s string values (rest.py:1560-1564) before merging. `permissions`
is the outlier — its custom setter pre-empts that generic path entirely
(`on_rest_save_field`, rest.py:1349-1360: a `set_<field>` method, if present,
is called instead of the generic path, and returns without falling through).

## Acceptance Criteria
- [x] Decide the intended contract: **rejected** — DECIDED during /scope
      (2026-07-12, user): `permissions` must be a real JSON object. Every
      non-dict value (JSON-encoded strings included) is rejected with a 400
      validation error. No lenient string parsing. See `## Plan`.
- [ ] Implement the chosen behavior in `set_permissions`.
- [ ] A dict `permissions` payload continues to work exactly as today (no
      regression to the passing tests below).
- [ ] Regression test: save with `permissions` as a JSON string — asserts
      either successful parse-and-persist, or an explicit error response (per
      the decision above) — NOT today's silent 200-with-no-change.

## Repro — bugs only
1. Create or fetch an `ApiKey` you're authorized to edit (holding
   `manage_group`/`manage_groups`).
2. `POST /api/group/apikey/<pk>` with body
   `{"permissions": "{\"manage_group\": true}"}` (a JSON string, not an
   object).
- Expected: either the permission is parsed and persisted, or the request
  fails with a clear validation error.
- Actual: 200 response; `permissions` on the record is unchanged —
  `set_permissions` returned early at api_key.py:171 with no error surfaced
  anywhere.

## Investigation
Confidence: **confirmed** (direct code reading + existing test evidence).
- Root cause: `mojo/apps/account/models/api_key.py:166-176` (`set_permissions`),
  early-return guard at line 171.
- Invocation path: `mojo/models/rest.py:1349-1360` (`on_rest_save_field` calls
  `set_<field>` before the generic JSONField path; because `set_permissions`
  exists, the generic path — rest.py:1368-1369 → `on_rest_update_jsonfield`,
  rest.py:1547 — is never reached for this field).
- A real dict DOES persist correctly today, confirming the setter itself
  works when given the right shape:
  `tests/test_user_mgmt/api_keys.py:180-183` (`apikey_rest_create` posts a
  dict, persists) and `tests/test_global_perms/apikey_groupless.py:230-236`
  (posts a dict, asserts persistence).
- Sibling fields `metadata`/`limits` (api_key.py:45-46, no custom setter) go
  through `on_rest_update_jsonfield`'s string-parsing branch
  (rest.py:1560-1564) and DO accept JSON strings — confirming `permissions`
  is the outlier, not the norm, within this same model.
- Regression-test feasibility: high — existing apikey REST test fixtures
  (`tests/test_user_mgmt/api_keys.py`,
  `tests/test_global_perms/apikey_groupless.py`) already cover create/save;
  adding a string-payload case is a small addition to either file.

## Plan

### Goal
`ApiKey.set_permissions` rejects any non-dict `permissions` payload with an
explicit 400 validation error instead of today's silent no-op.

### Context — what exists
- **The bug**: `mojo/apps/account/models/api_key.py:166-182` —
  `set_permissions(self, value)` is the REST setter for the `permissions`
  field. Its first two lines are:
  ```python
  if not isinstance(value, dict):
      return
  ```
  Any non-dict payload (JSON-encoded string, list, number, `null`) silently
  returns — 200 response, field unchanged, nothing logged. Below the guard,
  the method iterates `value.items()`, gates each perm through
  `can_change_permission` (raising `merrors.PermissionDeniedException` on
  failure), and merges into `self.permissions` (truthy → set, falsy → pop).
  The gated loop is correct and must not change.
- **Invocation path**: `mojo/models/rest.py:1349-1360` (`on_rest_save_field`)
  — because `set_permissions` exists, it is called instead of the generic
  JSONField path (`on_rest_update_jsonfield`, rest.py:1547) and returns
  without falling through. So this setter is the *only* write path for
  `permissions` via REST; fixing the guard fixes the whole surface.
  (`create_for_group` assigns `self.permissions` directly and never calls the
  setter — untouched.)
- **Error class**: `mojo/errors.py:26` — `ValueException(reason, code=400,
  status=400)`. Already imported in api_key.py:8 as
  `from mojo import errors as merrors` (see the
  `merrors.PermissionDeniedException()` raise at api_key.py:176). Raising it
  from a setter surfaces as a 400 JSON error response.
- **Existing passing tests that must not regress** (dict payloads work today):
  `tests/test_user_mgmt/api_keys.py:174-192` (`apikey_rest_create` POSTs a
  dict `permissions`, asserts 200 + persistence) and
  `tests/test_global_perms/apikey_groupless.py:230-236` (same, group admin).
- **Not reused, deliberately**: `_get_permissions_dict` (api_key.py:103-113)
  reads `self.permissions` (not a passed value) and swallows bad input into
  `{}` — that tolerant read-side behavior is exactly what we're NOT doing on
  the write side. Leave it alone.

### Changes — what to do
1. **`mojo/apps/account/models/api_key.py`** — in `set_permissions`, replace
   the silent early return:
   ```python
   if not isinstance(value, dict):
       return
   ```
   with a loud rejection:
   ```python
   if not isinstance(value, dict):
       raise merrors.ValueException("permissions must be a JSON object")
   ```
   (defaults give code=400, status=400). Update the setter's docstring to
   state the contract: `permissions` must be a JSON object; any other shape —
   including a JSON-encoded string — is rejected with 400. Nothing else in
   the method changes.
2. **`tests/test_user_mgmt/api_keys.py`** — add one regression test (see
   Tests below).
3. **Docs + CHANGELOG** — see Docs below.

### Design decisions
- **Reject ALL non-dicts, including valid JSON-object strings** (user
  decision, 2026-07-12): keep the contract tight — one accepted shape, a
  real object. Rejected alternative: leniently `json.loads` string payloads
  for symmetry with `metadata`/`limits` (rest.py:1560-1564). For a
  security-sensitive field, one unambiguous input shape beats convenience;
  callers sending strings get an immediate, actionable 400 instead of
  guess-the-parser semantics.
- **`ValueException` with defaults** — the framework's standard 400
  validation error; no new error class, no custom codes. KISS.
- **No change to the gated loop / `can_change_permission`** — the security
  behavior for dict payloads is correct today and covered by existing tests.

### Edge cases & risks
- **`"permissions": null`** — was a silent no-op, becomes a 400. No caller
  can depend on null *doing* anything (it never did); surfacing it is the
  point. Flagged as a deliberate behavior change.
- **`"permissions": {}`** — still valid: `isinstance({}, dict)` passes, the
  loop body never runs, no-op. Unchanged.
- **web-mojo admin GroupView dialog** (currently POSTs a JSON string, its own
  bug — see Notes) will start receiving 400s instead of fake 200s once a
  django-mojo release with this lands and maestro picks it up. That is
  strictly better (surfaces the bug) but worth remembering when the web-mojo
  redesign item is scoped.
- **Payload absent** — `set_permissions` is only invoked when the
  `permissions` key is present in the save payload (on_rest_save_field is
  per-key); omitting the field remains a no-op. Unchanged.

### Tests
Add to `tests/test_user_mgmt/api_keys.py` (testit; follow the style of
`apikey_rest_create` at :174 — `@th.unit_test(...)`, `def test_xxx(opts):`,
`opts.client`, descriptive assert messages; place it after `apikey_rest_get`
so `opts.rest_key_id` from the create test is available):
- **`apikey_rest_permissions_rejects_non_dict`** (the regression — must fail
  on today's code, pass after the fix). Login as admin, then against
  `POST /api/group/apikey/{opts.rest_key_id}` (with `group` param, matching
  sibling tests):
  1. JSON-string payload `{"permissions": "{\"manage_group\": true}"}` →
     assert `resp.status_code == 400` (today: 200).
  2. List payload `{"permissions": ["manage_group"]}` → assert 400.
  3. Re-GET the key and assert `permissions` on the record is unchanged from
     what `apikey_rest_create` set (`view_data`/`manage_group` still there,
     nothing added) — proves rejection didn't partially apply.
  4. Control: dict payload `{"permissions": {"view_data": False}}` → 200 and
     the perm is removed — proves the happy path still works end-to-end.
- Existing dict-payload tests (`apikey_rest_create`,
  `tests/test_global_perms/apikey_groupless.py`) must still pass untouched.
- Run: `bin/run_tests --agent -t test_user_mgmt.api_keys` (and the baseline /
  full-suite comparison per `.claude/rules/build-baseline.md`).

### Docs
- `docs/web_developer/account/api_keys.md:42` — the create-fields table row
  for `permissions`: state it must be a JSON **object**; any other shape
  (including a JSON-encoded string) is rejected with 400. One sentence.
- `docs/django_developer/account/api_keys.md:47` — the "Who can assign a
  key's permissions" paragraph: append one sentence that `set_permissions`
  accepts only a dict and raises `ValueException` (400) for anything else.
- `CHANGELOG.md` — entry under the current unreleased block: non-dict
  `permissions` payloads on API key save now return 400 instead of being
  silently ignored.

### Open questions
None.

## Notes
- Build baseline (2026-07-12, `bin/run_tests --agent`): 2426 total / 2370
  passed / 0 failed / 56 skipped — all green. No pre-existing failures.
- Companion finding, same investigation thread, different root cause and
  filed separately (mechanical fix, different acceptance criteria):
  `permission-gate-fallback-missing-base-groups-users-perm.md` — covers
  `can_change_permission`'s fallback list missing `"groups"` on this same
  model (plus 4 others found by the same audit).
- Also related (other repo, unscoped, no ID yet): a web-mojo item redesigning
  the GroupView API Key permissions editor to send per-permission boolean
  saves (mirroring the Group Member permission editor) instead of a
  whole-object JSON string. If that ships, this backend gap stops being hit
  by the primary UI, but remains a real gap for any other caller — this item
  stands on its own regardless of that redesign landing.
- Backfill `related:` on both sides once all three items are scoped and have
  IDs (cross-repo refs use the `org/other-repo#ITEM-007` form per this
  repo's `_template.md`).

## Resolution
- closed: 2026-07-12
- branch: main
- files changed: mojo/apps/account/models/api_key.py,tests/test_user_mgmt/api_keys.py,docs/django_developer/account/api_keys.md,docs/web_developer/account/api_keys.md,CHANGELOG.md
- tests added: tests/test_user_mgmt/api_keys.py::apikey_rest_permissions_rejects_non_dict (string payload → 400, list payload → 400, record unchanged after rejection, dict add+remove control)
