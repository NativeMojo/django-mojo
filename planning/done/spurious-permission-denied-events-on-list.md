## Spurious user_permission_denied incidents from list endpoints that return 200

**Type**: bug
**Status**: resolved
**Date**: 2026-04-25
**Severity**: medium

## Description
`GET /api/group` (and any list endpoint with custom `on_rest_handle_list` fallbacks) records a `user_permission_denied` security incident for every authenticated user who lacks `VIEW_PERMS`, even when the request ultimately returns HTTP 200 with an empty or scoped list. The user sees no error, but the security log fills up with false-positive permission-denied events.

Reported pattern: many `user_permission_denied` events all hitting the same endpoint `GET /api/group?start=0&size=1000`. Affected users are merchant-level / location accounts who legitimately don't have `view_groups` / `manage_groups` but DO get a successful response (filtered to just their memberships, or empty).

## Context
- These are not real permission denials — the API responds 200, the UI works.
- The noise drowns out genuine permission-denied events and triggered creation of a bundling rule (#74) to deduplicate.
- Any model that overrides `on_rest_handle_list` to provide a softer fallback (Group does, base class does too when `MOJO_REST_LIST_PERM_DENY=False`) has this problem.

## Acceptance Criteria
- An authenticated user calling a list endpoint that returns 200 (empty or filtered) MUST NOT generate a `user_permission_denied` (or `group_member_permission_denied`, `view_permission_denied`) incident.
- A request that actually responds 403 MUST still generate the incident.
- Other event types (`unauthenticated`, real edit/save denials) remain unchanged.
- Regression test: authenticated user with no perms calling `GET /api/group` returns 200 + empty list AND no incident is recorded.

## Investigation
**Likely root cause**: `MojoModel.rest_check_permission` (mojo/models/rest.py:204-303) calls `class_report_incident(...)` inline at every False-return branch. The list-handling code paths at `on_rest_handle_list` (mojo/models/rest.py:373-411) and `Group.on_rest_handle_list` (mojo/apps/account/models/group.py:582-607) call `rest_check_permission` first, and only fall through to a graceful empty/filtered list when it returns False. The incident is fired before the caller has a chance to recover, so a 200 response still leaves a denial event behind.

**Confidence**: confirmed (code analysis)

**Code path**:
- mojo/apps/account/rest/group.py:6-10 — endpoint
- mojo/models/rest.py:467 — `on_rest_handle_get` dispatches list
- mojo/models/rest.py:373-411 — base `on_rest_handle_list`
- mojo/apps/account/models/group.py:582-607 — Group's overridden list, returns empty list at line 603 for authenticated users with no groups
- mojo/models/rest.py:289-303 — fires `user_permission_denied` before returning False
- mojo/models/rest.py:230-244 — fires `view_permission_denied`
- mojo/models/rest.py:270-286 — fires `group_member_permission_denied`

**Regression test**: [tests/test_account/test_group_list_no_perms.py](tests/test_account/test_group_list_no_perms.py) — authenticated user with no `view_groups` perm and no memberships hits `GET /api/group`, asserts 200 + empty list, then asserts no `user_permission_denied` / `view_permission_denied` / `group_member_permission_denied` Event was recorded for that user. Currently FAILS with one spurious `user_permission_denied` event (branch `user.has_permission`, path `/api/group`) — exactly the production noise. Will pass once the inline reporting is removed.

**Related files**:
- `mojo/models/rest.py` — `rest_check_permission` is where the redundant reporting happens (lines 211, 234, 249, 275, 293).
- `mojo/decorators/http.py` — `dispatch_error_handler` (lines 110-122, 95-109) already reports `api_denied` / `mojo_rest_error` with full request context whenever a handler raises `PermissionError` or `MojoException`. This is the single, correct reporting site.
- `mojo/apps/account/models/group.py` — caller that swallows the False return into a 200 list.
- Other apps that override `on_rest_handle_list` — same pattern, all silently emitting false positives.

## Notes for design

The dispatcher at `mojo/decorators/http.py:95-122` already catches `MojoException` (the base of `PermissionDeniedException`) and `PermissionError`, and emits a full incident with path, IP, user, headers via `class_report_incident_for_user`. That is the correct single emission point. The inline `class_report_incident` calls in `rest_check_permission` are:

- **Redundant** when the False return eventually surfaces as a raised exception (request logged twice).
- **Wrong** when the False return is recovered into a 200 (Group list, base `MOJO_REST_LIST_PERM_DENY=False`, owner fallback at lines 391-407) — no denial happened, yet an event fires.

### Proposed fix — enrich the exception, not the predicate

1. **Enhance `PermissionDeniedException`** (`mojo/errors.py`) with structured metadata:
   ```python
   class PermissionDeniedException(MojoException):
       def __init__(self, reason='Permission Denied', code=403, status=403, *,
                    branch=None, perms=None, permission_keys=None,
                    model_name=None, instance=None,
                    event_type="user_permission_denied"):
           super().__init__(reason, code, status)
           self.branch = branch
           self.perms = perms
           self.permission_keys = permission_keys
           self.model_name = model_name
           self.instance = instance
           self.event_type = event_type
   ```

2. **Dispatcher merges exception metadata into the incident.** In `mojo/decorators/http.py` `MojoException` branch, when the caught exception is a `PermissionDeniedException`, use `err.event_type` as the `event_type` and pass `branch`, `perms`, `permission_keys`, `model_name`, `instance` as context kwargs. Non-permission `MojoException`s keep `mojo_rest_error`.

3. **`rest_check_permission` becomes a pure boolean predicate.** Delete all five `class_report_incident` calls (lines 211, 234, 249, 275, 293). No side effects.

4. **403 call sites raise instead of returning a JsonResponse.** Replace:
   - `mojo/models/rest.py:410` (`return cls.rest_error_response(request, 403, ...)`) with `raise PermissionDeniedException(branch="list_perm_deny", model_name=cls.__name__, event_type="user_permission_denied")`
   - The `unauthenticated` branch (line 211) becomes `raise PermissionDeniedException(reason="Unauthenticated", status=401, event_type="unauthenticated", ...)` raised from the calling handler when the predicate returns False under that condition — or a separate `raise_if_unauthenticated` helper.
   - Edit/save denial paths in `on_rest_handle_save` etc. raise `PermissionDeniedException` with the right `branch` / `event_type`.

5. **Callers that intend to recover** (Group's list fallback, base list fallback for `MOJO_REST_LIST_PERM_DENY=False`, owner fallback) keep consuming the False return — and now correctly produce no event.

### Why this shape over a flag-based suppression

- Single responsibility: predicate predicates, dispatcher reports.
- Preserves the diagnostic detail today's events carry (`branch`, `perms`, `model_name`) — bundling rules and queries that key on those fields keep working.
- No new `report_incident=False` argument scattered through callers.
- Closes the `rest_error_response`-returns-403 hole (currently bypasses the dispatcher).

### Audit before coding

Grep `mojo/models/rest.py` for every `class_report_incident*` call outside `rest_check_permission` and classify each as (a) real audit event to keep, or (b) another denial-path to convert. Confirm `cls.rest_error_response` callers with status 403 elsewhere in the codebase, and convert them.

## Plan

**Status**: planned
**Planned**: 2026-04-25

### Objective
Make `rest_check_permission` a pure boolean predicate with no side effects, and route every 401/403 from the framework through the dispatcher's exception handler so denial events are emitted exactly once at the layer that knows the actual HTTP outcome.

### Design Decisions

- **Single emission site = the dispatcher.** `mojo/decorators/http.py:95-122` already catches `MojoException` for every REST handler; we extend that one path to emit denial events with full metadata. No more inline reporting in the predicate.
- **`rest_check_permission` stays a pure boolean predicate.** Same name, same signature, same return type — backwards compatible for the assistant tools and FK-attach callers that already interpret the bool. Internal `class_report_incident` calls are deleted.
- **New `rest_check_permission_or_raise(request, permission_keys, instance=None) -> True`** raises `PermissionDeniedException` populated with `branch`, `perms`, `permission_keys`, `model_name`, `instance`, `event_type`, `status`. Used by every `on_rest_handle_*` framework method.
- **HTTP semantics: 401 vs 403.** Unauthenticated → 401 with `event_type="unauthenticated"`. Authenticated-but-forbidden → 403 with the matching event_type from the failed branch.
- **Every 401/403 emits an event.** Including CAN_*=False rejections (verb disabled model-wide) — those use `event_type="feature_disabled"` so they're filterable but they absolutely fire.
- **FK silent-skip keeps its audit trail explicitly.** `on_rest_save_related_field` calls the predicate; on False it emits a `fk_attach_denied` event in-place, then skips the assignment. Documented contract preserved.
- **`MOJO_APP_STATUS_200_ON_ERROR` flag is honored uniformly.** Move the flag handling into the dispatcher's `MojoException` serialization so raise-based 403s and `rest_error_response`-based errors produce identical wire behavior.
- **`PermissionDeniedException` carries metadata.** Six new optional kwargs (`branch`, `perms`, `permission_keys`, `model_name`, `instance`, `event_type`) stored as attributes. Defaults preserve current call sites that pass only a reason.

### Event categories produced

| Category | Status | Trigger |
|---|---|---|
| `unauthenticated` | 401 | Unauth request hit a perm-gated endpoint |
| `user_permission_denied` | 403 | User lacks system-level perms (predicate `user.has_permission` branch) |
| `view_permission_denied` | 403 | `instance.check_view_permission` rejected |
| `edit_permission_denied` | 403 | `instance.check_edit_permission` rejected |
| `group_member_permission_denied` | 403 | Group-scoped perm check failed |
| `feature_disabled` | 403 | `CAN_UPDATE/CAN_DELETE/CAN_CREATE/CAN_BATCH = False` |
| `fk_attach_denied` | n/a (no HTTP error) | FK save silently skipped due to lack of VIEW_PERMS on related instance |

All carry: `branch`, `perms`, `permission_keys`, `model_name`, `instance`, `request_path`, plus the request context the dispatcher's reporter already adds.

### Steps

1. **`mojo/errors.py`** — add metadata kwargs to `PermissionDeniedException`:
   ```
   branch, perms, permission_keys, model_name, instance, event_type
   ```
   Stored on the instance. Defaults preserve existing call sites (single-arg `PermissionDeniedException("reason")` keeps working). Default `event_type="user_permission_denied"`, default `status=403`.

2. **`mojo/decorators/http.py`** (lines 95-122) — specialize the `MojoException` handler:
   - If exception is a `PermissionDeniedException`: build incident kwargs from `err.event_type`, `err.branch`, `err.perms`, `err.permission_keys`, `err.model_name`, `err.instance`. Use level 4 for 403s, level 3 for `unauthenticated`/`feature_disabled`.
   - Otherwise: keep existing `mojo_rest_error` path.
   - Apply `MOJO_APP_STATUS_200_ON_ERROR` to the JsonResponse status code uniformly (port the conditional from `mojo/models/rest.py:119-120`).
   - Drop the `PermissionError` branch's `level=4` to match (or leave; it's a separate path used by app-level code, not the framework).

3. **`mojo/models/rest.py`** — `rest_check_permission` (lines 193-303):
   - Delete five `class_report_incident` calls (lines 211, 234, 249, 275, 293).
   - Keep all branch logic, including the `request.group` side-effect at line 268 (document in docstring).
   - Update docstring: "Pure predicate; returns True/False with no event emission."

4. **`mojo/models/rest.py`** — add `rest_check_permission_or_raise` next to the predicate:
   - Calls predicate. On True → return True. On False → raise `PermissionDeniedException` with metadata reconstructed from the same branch logic the predicate evaluated. Implementation: re-run the discriminator (or have the predicate return a small `objict` with branch info that the wrapper raises from). Cleanest path: factor the branch logic into an internal `_evaluate_permission(...)` returning `(allowed: bool, denial_meta: objict|None)`; predicate ignores meta; raiser raises with it. No duplicated branch logic.

5. **`mojo/models/rest.py`** — convert all framework 403 sites to `rest_check_permission_or_raise` + `raise PermissionDeniedException`:
   - Line 317-319 `on_rest_handle_get` → `cls.rest_check_permission_or_raise(...); return instance.on_rest_get(request)`.
   - Line 346-351 `on_rest_handle_save` → CAN_UPDATE=False → `raise PermissionDeniedException(reason=f"UPDATE not allowed: {cls.__name__}", event_type="feature_disabled")`. Then `rest_check_permission_or_raise(...)`.
   - Line 365-370 `on_rest_handle_delete` → CAN_DELETE=False → `feature_disabled` raise. Then `rest_check_permission_or_raise(...)`.
   - Line 384-410 `on_rest_handle_list` → predicate returns False → fall through to recovery branches as today (no raise). Final `MOJO_REST_LIST_PERM_DENY=True` branch (line 410) → `raise PermissionDeniedException(branch="list_perm_deny", model_name=cls.__name__, event_type="user_permission_denied")`. The `MOJO_REST_LIST_PERM_DENY=False` branch returns 200 empty list silently.
   - Line 447-453 `on_rest_handle_create` → CAN_CREATE=False → `feature_disabled`. Then `rest_check_permission_or_raise(...)`.
   - Line 487-491 `on_rest_handle_batch` → CAN_BATCH=False → `feature_disabled`. Then `rest_check_permission_or_raise(...)`.

6. **`mojo/models/rest.py`** — `on_rest_save_related_field` (lines 1108-1156): keep predicate calls; after each False return, before the silent skip, emit:
   ```
   cls.class_report_incident_for_user(
       details=f"FK attach denied: {field.name} on {self.__class__.__name__}",
       event_type="fk_attach_denied", level=2, request=request,
       field_name=field.name, related_model=field.related_model.__name__,
       related_id=getattr(related_instance, "id", None),
   )
   ```
   Update the inline comment block at lines 1138-1148 to point at `fk_attach_denied` rather than "the existing incident-event reporting in rest_check_permission."

7. **`mojo/apps/account/models/group.py`** — `Group.on_rest_handle_list` (lines 582-607): replace line 606 `return cls.rest_error_response(request, 403, ...)` with `raise PermissionDeniedException(branch="list_perm_deny_group", model_name=cls.__name__, event_type="user_permission_denied")`. The predicate-False path that recovers into the combined-ids list at line 600 stays silent (the bug fix).

8. **`mojo/apps/account/rest/group.py`** (line 39) — convert `return Group.rest_error_response(request, 403, error="GET permission denied: Group")` to `raise PermissionDeniedException(reason="GET permission denied: Group", branch="group_member_endpoint", model_name="Group", event_type="user_permission_denied")` for consistency.

9. **No changes** to: assistant tools (already do their own reporting), chat/rooms.py and other app-level `rest_error_response` 4xx callers (those are 404/400, not 403), `mojo/apps/account/utils/tokens.py:156` (legitimate audit event, not a denial path).

### Edge Cases

- **`MOJO_APP_STATUS_200_ON_ERROR=True`**: today `rest_error_response` rewrites status to 200 with `status:false` body; the dispatcher's `MojoException` handler does NOT honor this flag (returns `status=err.status`). Step 2 ports the flag check into the dispatcher so client-visible behavior stays identical when callers switch from `rest_error_response` to `raise PermissionDeniedException`.
- **`request.group` side-effect at line 268** (`request.group = getattr(instance, "group", None)`): predicate keeps writing this. Documented in the new docstring. No event coupling.
- **Bundle rule #74** (production rule keying on `category=user_permission_denied` + `source_ip`): keeps working — same category, same metadata fields, just zero false positives.
- **Double-emission today in assistant tools** (`rest_check_permission` inline + `_report_security_event`): after fix, only the assistant's report fires — strict improvement.
- **`request.bearer` missing** on some requests: `incident.report_event` already guards with `getattr` checks; no change needed.
- **Unauthenticated wire status 403 → 401**: clients currently receive 403 for auth-required endpoints when middleware lets the request through. After fix they receive 401. Search for clients that branch on 403-vs-401 — none expected (auth middleware already returns 401 for missing bearer in the normal case; this only fires on the rare path where the predicate sees an unauth user).
- **`PermissionDeniedException` raised from non-REST contexts** (e.g. internal services): dispatcher won't see those; existing behavior unchanged. The exception just carries more attributes that go unused.
- **Multiple permission checks in one handler** (e.g. `on_rest_handle_save` checks SAVE_PERMS+VIEW_PERMS in one call): `rest_check_permission_or_raise` raises with the first failing branch's metadata. Single event per request, never two.
- **CAN_*=False emits `feature_disabled`**: queries/dashboards filtering by `category="user_permission_denied"` won't include these. Acceptable — it's a different kind of denial. Operators can OR the categories if they want a unified view.

### Testing

- `tests/test_account/test_group_list_no_perms.py` (already exists, currently fails) → passes after fix. Add three more tests in this file:
  - **Authed user with memberships** → 200 with their groups + 0 events.
  - **Anonymous (no auth header)** → 401 + exactly 1 `unauthenticated` event with `path=/api/group`.
  - **Authed user with `view_groups` perm** → 200 with all groups + 0 events.
- `tests/test_models/permission_events.py` (new):
  - Authed user, no perms, `GET /api/<protected-model>/<id>` → 403 + 1 `view_permission_denied` event with `branch=instance.check_view_permission`, `model_name=<Model>`, `instance` populated.
  - Authed user, no perms, `POST /api/<protected-model>` → 403 + 1 `user_permission_denied` event with `branch=user.has_permission`.
  - Authed user, no perms, `DELETE /api/<protected-model>/<id>` → 403 + 1 denial event.
  - Group-scoped check failure → 403 + 1 `group_member_permission_denied` event.
- `tests/test_models/feature_disabled_events.py` (new):
  - Model with `CAN_DELETE=False`, superuser hits DELETE → 403 + 1 `feature_disabled` event with `reason="DELETE not allowed: <Model>"`.
  - Same for CAN_UPDATE / CAN_CREATE / CAN_BATCH.
- `tests/test_models/fk_attach_audit.py` (new):
  - User with SAVE_PERMS on parent model but no VIEW_PERMS on FK target → save succeeds with field unchanged + exactly 1 `fk_attach_denied` event with `field_name`, `related_model`, `related_id`.
  - `NO_FK_VIEW_CHECK_FIELDS` opt-out: same setup with field listed → no event, no skip.
- `tests/test_models/status_200_on_error.py` (new, uses `th.server_settings(MOJO_APP_STATUS_200_ON_ERROR=True)` so it should be a serial module):
  - 403 path returns wire status 200 with `status:false` body.
  - 401 path returns wire status 200 with `status:false` body.
  - Event still fires.

All tests use `Event.objects.filter(uid=opts.user_id, category=...)` to assert event presence/absence and metadata fields.

### Docs

- `docs/django_developer/core/mojo_model.md:222` — update `rest_check_permission` section to describe it as a pure predicate; add a `rest_check_permission_or_raise` subsection covering raise semantics, the metadata carried on `PermissionDeniedException`, and where the dispatcher emits the event.
- `docs/django_developer/rest/permissions.md:258` — update the FK-attach paragraph: incident is now `fk_attach_denied`, emitted by the FK handler (`on_rest_save_related_field`), not by the predicate. Mention the metadata fields (`field_name`, `related_model`, `related_id`).
- `docs/django_developer/incident/` (or wherever event categories live) — add the seven category names and what they mean.
- `docs/web_developer/` — note in the relevant errors/responses page that 401 is now returned for unauthenticated requests at perm-gated endpoints (was 403 in the rare middleware-bypass case).
- `CHANGELOG.md` — entry: "Permission events now emit only when the request actually responds 401/403. Recovery paths (Group list fallback, owner/group-filtered list, `MOJO_REST_LIST_PERM_DENY=False`) no longer log false-positive incidents. New event categories: `feature_disabled` (CAN_*=False rejections), `fk_attach_denied` (silent FK-attach skips). Unauthenticated requests at perm-gated handlers now return HTTP 401 instead of 403."

## Resolution

**Status**: resolved
**Date**: 2026-04-25

### What Was Built

Refactored permission-denial event emission so the REST dispatcher in `mojo/decorators/http.py` is the single source of truth. `MojoModel.rest_check_permission` is now a pure boolean predicate; new `rest_check_permission_or_raise` raises `PermissionDeniedException` with structured metadata (`branch`, `perms`, `permission_keys`, `model_name`, `instance`, `event_type`, `status`). All framework 403 sites raise instead of returning a JsonResponse. The dispatcher catches the exception, emits exactly one categorized incident, and serializes the response (honoring `MOJO_APP_STATUS_200_ON_ERROR`). Recovery paths that recover into 200 (Group list fallback, owner/group-filtered, `MOJO_REST_LIST_PERM_DENY=False`) now correctly emit zero events. Unauthenticated requests at perm-gated handlers return 401 (was 403). FK-attach silent-skip preserves its audit trail explicitly via `_report_fk_attach_denied` → `event_type="fk_attach_denied"`. CAN_*=False rejections raise with `event_type="feature_disabled"` and a distinguishable `branch`.

### Files Changed

- `mojo/errors.py` — `PermissionDeniedException` now carries metadata kwargs.
- `mojo/decorators/http.py` — specialized `MojoException` handler emits `PermissionDeniedException` events with metadata; `MOJO_APP_STATUS_200_ON_ERROR` honored uniformly.
- `mojo/models/rest.py` — `_evaluate_permission` factored helper; `rest_check_permission` pure predicate; new `rest_check_permission_or_raise`; framework 403 sites raise; FK-attach emits `fk_attach_denied` explicitly.
- `mojo/apps/account/models/group.py` — `Group.on_rest_handle_list` raises (401 for unauth, 403 for forbidden) instead of returning JsonResponse.
- `mojo/apps/account/rest/group.py` — `group/<pk>/member` endpoint raises on missing group.

### Tests

- `tests/test_account/test_group_list_no_perms.py` — 4 tests covering noperm/no-membership empty list, noperm/with-membership scoped list, with-perm full list, anonymous → 401 + `unauthenticated` event.
- `tests/test_models/permission_events.py` — 4 tests: GET-instance `view_permission_denied`, POST `user_permission_denied`, DELETE denial, recovery path no event.
- `tests/test_models/feature_disabled_events.py` — 4 tests: CAN_UPDATE/CAN_DELETE/CAN_CREATE/CAN_BATCH=False each raise with `feature_disabled`.
- `tests/test_models/fk_attach_audit.py` — 2 tests: FK silent-skip emits `fk_attach_denied` with `field_name`/`related_model`/`related_id`; `NO_FK_VIEW_CHECK_FIELDS` opt-out emits none.
- `tests/test_models/status_200_on_error.py` — 3 tests: default flag → 403 wire / flag-on → 200 wire for both 403 and 401, body always carries real code.
- `tests/test_models/can_update_gate.py` — updated 4 tests that asserted on JsonResponse to expect raised `PermissionDeniedException`.
- `tests/test_assistant/28_test_fk_perm_check.py` — updated event-category filter from `__contains="permission_denied"` to `="fk_attach_denied"`.
- Run: `bin/run_tests --agent -t test_models -t test_account` → 72/72 pass. Full suite: only pre-existing flaky `ws_manager_online_status` (Redis state leak between parallel modules) — unrelated.

### Docs Updated

- `docs/django_developer/core/mojo_model.md` — predicate vs raiser split, exception metadata table, full event-category table.
- `docs/django_developer/rest/permissions.md` — FK-attach paragraph rewritten + CAN_*=False `feature_disabled` note.
- `docs/django_developer/logging/incidents.md` — full event-category table including `feature_disabled` and `fk_attach_denied`.
- `docs/web_developer/core/request_response.md` — 401 vs 403 client-side guidance.
- `CHANGELOG.md` — `## Unreleased` section with Fixed + Changed entries.

### Security Review

No auth bypasses, audit gaps, or data leaks. Two INFO notes (both pre-existing, neither introduced by this commit):
- Unauthenticated request with `request.group` populated would categorize as `group_member_permission_denied`/403 instead of `unauthenticated`/401 — misleading category, no security consequence.
- Operators currently filtering on `category="user_permission_denied"` lose visibility into CAN_*=False rejections; those now flow under `feature_disabled` (strictly better than prior state of no event at all).

### Follow-up

None.
