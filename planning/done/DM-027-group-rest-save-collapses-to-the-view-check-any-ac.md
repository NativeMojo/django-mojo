---
# id is assigned by /scope on pickup — leave it blank
id: DM-027
type: bug
title: Group REST save collapses to the view check — any active member can update Group fields
priority: P1
effort: S
owner:
opened: 2026-07-10
depends_on: []
related: [DM-016, DM-019, DM-021]
links: []
---

# Group REST save collapses to the view check — any active member can update Group fields

## What & Why
`POST /api/account/group/<pk>` (the generic RestMeta update path) is effectively
gated by `Group.check_view_permission`, whose final fallthrough admits **any
active member** (`request.DATA.set("graph", "basic"); return True` — clearly a
read-with-downgraded-graph affordance). `Group.RestMeta.SAVE_PERMS =
["manage_groups", "manage_group", "groups"]` is dead config on this path: a
plain member with **zero** permissions can rename the group and change `kind`,
`auth_domain` (drives group-by-hostname auth resolution — hijack surface),
top-level `metadata`, and reach `POST_SAVE_ACTIONS` at member level
(`on_action_realtime_message` has no internal perm check — any member can
publish to `group:<id>:*` realtime topics via a REST save). A second door:
nested FK dict-save (`{"group": {"name": "x"}}` on any group-FK model the
caller can save) writes the Group through the same collapsed check
(rest.py:1409 → `related_instance.on_rest_save`).

Writes must require SAVE_PERMS (globally, or at GroupMember level, or a
confined ApiKey holding the perm). Reads keep the member basic-graph downgrade.

## Acceptance Criteria
- [ ] Plain active member (no member perms) `POST /api/account/group/<pk>
      {"name": "x"}` → 403, name unchanged in DB.
- [ ] Member holding member-level `manage_group` → 200, name changed.
- [ ] Plain member `GET /api/account/group/<pk>` → 200 with the **basic** graph
      downgrade still applied (read behavior unchanged).
- [ ] Member attach-by-pk of their group (`group=<pk>` on rows they create)
      keeps working — rest.py:1453 passes bare `"VIEW_PERMS"` and must stay
      view-classified.
- [ ] ApiKey behavior preserved: confined key holding the perm can save its
      group; zero-perm or unconfined key cannot (DM-019 bar).
- [ ] `User.check_edit_permission` keeps gating VIEW-by-pk (its documented
      behavior) — no routing change for models with only an edit hook.
- [ ] Regression test covering the above; suite green; docs + CHANGELOG note the
      behavior change (member-level `realtime_message` action now needs
      manage_group; Maestro accepts plain-member rename → 403).

## Repro — bugs only
1. Create group G; add user U as an active `GroupMember` with `permissions = {}`.
2. As U: `POST /api/account/group/<G.pk>` body `{"name": "hacked"}`.
- Expected: 403 `permission_denied` (U holds none of SAVE_PERMS at any level);
  G.name unchanged.
- Actual: 200; G.name is now "hacked" (response rendered with the basic graph —
  the giveaway that the save was authorized by the *view* fallthrough).

## Investigation
- **Root cause — confirmed (by code read; call-site map exhaustively grepped).**
  `MojoModel._evaluate_permission` (mojo/models/rest.py:246-248) computes
  `is_view = "VIEW_PERMS" in permission_keys`. Every caller includes
  `VIEW_PERMS` (it is the fallback perms list), so `is_view` is **always
  True** — the flag is vestigial. Consequence: an instance with
  `check_view_permission` has the **view hook decide all operations including
  writes** (rest.py:249-258); `check_edit_permission` (rest.py:260) is
  reachable only for models with no view hook.
- Only `Group` defines `check_view_permission`
  (mojo/apps/account/models/group.py:575-597); only `User` defines
  `check_edit_permission` (mojo/apps/account/models/user.py:910-923, which
  deliberately gates VIEW too — keep that).
- On save the perms list is `SAVE_PERMS` (`get_rest_meta_prop` first-defined-
  wins, rest.py:89-94) but the member fallthrough ignores `perms` entirely.
  Even the explicit member branch (group.py:592) passes on `view_group` — a
  read perm authorizing a write.
- **Not intended** — evidence: (1) the ApiKey branch in the same method
  (group.py:576-585) was hardened *because* save flows through it (comment:
  "this override gates SAVE as well as VIEW... would let a zero-permission key
  write its own group's auth_config/geofence") — the member fallthrough was
  never revisited for save; (2) `GroupMember` has no view hook, so its save is
  correctly gated by SAVE_PERMS via the group branch — inconsistent; (3)
  `Group.SAVE_PERMS` is otherwise dead config; (4) Group's write actions
  (`on_action_disable`/`reactivate`, group.py:611-640) demand global
  `manage_groups`.
- **Call-site map** (the fix's blast radius):
  - Pure views pass the bare string `"VIEW_PERMS"`: GET detail rest.py:416,
    list gates rest.py:490 + group.py:790, attach-by-pk rest.py:1453-1455,
    assistant read tools (models.py:551, 1078, 1330). All keep view semantics.
  - Genuine writes pass write-led lists: save rest.py:452, delete rest.py:475,
    create rest.py:582/626 (instance=None — hook block skipped, unaffected),
    **nested FK dict-save rest.py:1409** (calls `related_instance.on_rest_save`
    — a real write; must tighten with the rest), assistant tools
    (models.py:703, 843, 869).
  - Therefore the operation can be classified **from the keys list** (write
    keys present ⇒ write) — no new kwarg; no call-site edits needed.
- **Fix direction:** (1) `_evaluate_permission`: `is_view` = bare
  `"VIEW_PERMS"` string, or a list containing none of
  CREATE/SAVE/DELETE_PERMS. View ops keep view-hook-first (edit-hook fallback
  for models with only an edit hook — User unchanged). Write ops skip the view
  hook: edit hook if defined, else fall through to the generic
  owner/group/flat branches (for group-scoped models that is the same
  member-SAVE_PERMS gate GroupMember already uses — strictly tightening).
  (2) Add `Group.check_edit_permission`: ApiKey ⇒ `is_group_allowed(self) AND
  has_permission(perms)` (same bar as the view hook's key branch); else global
  `request.user.has_permission(perms)`; else
  `get_member_for_user(check_parents=True)` and `ms.has_permission(perms)`.
  (3) Update the now-stale "gates SAVE as well as VIEW" comment in
  `check_view_permission`.
- Post-fix allowed-set for Group save is a strict subset of pre-fix (any
  member ∪ global holders → manage_group-level members ∪ global holders ∪
  confined keys with perm) — no loosening anywhere; direction is fail-closed.
- **Regression-test feasibility: high.** HTTP-level via testit client: plain
  member 403 + unchanged, manage_group member 200 + changed, plain member GET
  basic-graph downgrade intact. Member perms live on the GroupMember row.
- **Downstream:** Maestro (/Users/ians/Projects/mojo/maestro/api) deliberately
  accepts any-member workspace rename today; owner decided 2026-07-10 the
  tightening is acceptable — workspace admins hold member-level `manage_group`
  and keep working; plain-member rename starts returning 403. Also flag:
  member-level `realtime_message` REST action tightens to manage_group.

## Plan

### Goal
Make `POST`/`DELETE` on a Group instance (and its POST_SAVE_ACTIONS) require
`SAVE_PERMS` — globally, at the GroupMember level, or via a confined ApiKey that
holds the perm — instead of collapsing to the any-member view check, while GET
keeps the member basic-graph downgrade.

### Context — what exists
- **Route:** `POST /api/group/<pk>` → `Group.on_rest_request` →
  `on_rest_handle_save` → `rest_check_permission_or_raise(request,
  ["SAVE_PERMS","VIEW_PERMS"], instance)` (rest.py:452). This `:452` gate is the
  **only** permission check in front of the `on_action_*` dispatch loop
  (rest.py:1303-1307), so tightening save tightens Group's actions
  (`realtime_message`, `disable`, `reactivate`) too. (account rest mounts at the
  bare `api` prefix — `mojo/apps/account/rest/__init__.py` `APP_NAME=""` — so the
  path is `/api/group/<pk>`, not `/api/account/...`.)
- **The bug — `_evaluate_permission` (mojo/models/rest.py:245-285):** computes
  `is_view = "VIEW_PERMS" in permission_keys`. VIEW_PERMS is in *every* caller's
  keys, so `is_view` is **always True**; the flag is vestigial. When the instance
  defines `check_view_permission`, that hook then decides *all* operations
  including writes. Only `Group` defines `check_view_permission`
  (group.py:575-597), whose final fallthrough is
  `request.DATA.set("graph","basic"); return True` for **any active member** — a
  read affordance authorizing writes. `check_edit_permission` (rest.py:260) is
  only defined by `User` (user.py:910-923, which deliberately gates VIEW-by-pk
  too since User has no view hook).
- **The reusable helper:** `Group.user_has_permission(user, perms,
  check_user=True)` (group.py:213-221) already does exactly the write gate:
  global `user.has_permission(perms)` → True; non-`User` identity → False; else
  `get_member_for_user(user, check_parents=True).has_permission(perms)`. The
  write hook delegates to it. `GroupMember.has_permission` (member.py:108-129) is
  **any-of** over a list, reading the `permissions` JSON dict.
- **`perms` resolution:** `get_rest_meta_prop(["SAVE_PERMS","VIEW_PERMS"])`
  returns first-defined = `SAVE_PERMS = ["manage_groups","manage_group","groups"]`
  (group.py:50). Group defines no `CREATE_PERMS`/`DELETE_PERMS` and no
  `CAN_DELETE` (delete is disabled by default), so every write path resolves
  `perms` to SAVE_PERMS.
- **Call-site map (the blast radius):**
  - *View ops pass a bare `"VIEW_PERMS"` string:* GET detail (rest.py:416), list
    gates (rest.py:490, group.py:790), **FK attach-by-pk**
    (rest.py:1453-1455), assistant read tools (models.py:551,1078,1330).
  - *Write ops pass a write-led list:* save (rest.py:452), delete (rest.py:475),
    create (rest.py:582/626 — `instance=None`, hook block skipped),
    **FK dict-save (rest.py:1409** — `["SAVE_PERMS","VIEW_PERMS"]`, then calls
    `related_instance.on_rest_save` — a genuine inline write), assistant
    save/delete tools (models.py:703,843,869; the save tool re-checks with the
    same keys+instance so it tightens in lockstep — no bypass).
- **The one at-risk test:** `tests/test_geofence/strict_posture.py::
  test_group_strict_requires_global_perm` (strict_posture.py:250-303) — a member
  with member-level `manage_group` POSTs `/api/group/<pk>` metadata and expects
  200. Under the fix it stays 200 because `check_edit_permission` grants
  member-level `manage_group` (SAVE_PERMS any-of). Its `geofence_strict` 403
  comes from a *different* field-level gate (`on_rest_pre_save`) — unchanged.
- **ApiKey coverage:** `tests/test_global_perms/apikey_groupless.py::
  test_apikey_cannot_write_own_group_without_perm` (zero-perm confined key POST
  Group → 403). Stays green: the new hook's ApiKey branch requires
  `has_permission(perms)`.

### Changes — what to do
1. `mojo/models/rest.py` — in `_evaluate_permission`, replace the vestigial
   `is_view` computation (rest.py:246-249) with a **write** classification and
   restructure the hook selection (rest.py:245-268):
   - Compute `is_write`: `permission_keys` (str or list) contains any of
     `("CREATE_PERMS","SAVE_PERMS","DELETE_PERMS")`.
   - **View op** (`not is_write`) AND `hasattr(instance, "check_view_permission")`
     → call the view hook (return allow, or `view_permission_denied` 403).
   - Then, unchanged: `if hasattr(instance, "check_edit_permission")` → call the
     edit hook (return allow, or `edit_permission_denied` 403). This keeps
     User's VIEW-by-pk gated (no view hook → falls here) AND makes write ops use
     the edit hook when present.
   - Leave the owner-match and `request.group` binding (rest.py:270-285,
     GROUP_FIELD-aware — from the in-tree uncommitted work) exactly as-is.
   - Net: a write op on an instance with only a view hook (Group, until step 2's
     hook exists) skips the view hook; with the edit hook added it routes there.
2. `mojo/apps/account/models/group.py` — add `check_edit_permission(self, perms,
   request)` (next to `check_view_permission`, ~group.py:598):
   ```python
   def check_edit_permission(self, perms, request):
       # WRITE gate for POST/PUT/DELETE on /api/group/<pk> (and its
       # POST_SAVE_ACTIONS). Unlike check_view_permission there is NO
       # any-member fallthrough and no graph downgrade — a write demands an
       # actual SAVE_PERMS grant. ApiKey: confined to its own group tree AND
       # holds the perm (same bar as the key branch in check_view_permission /
       # _evaluate_permission). Users: global grant OR member-level grant, via
       # Group.user_has_permission (global-or-member, parent-aware).
       api_key = getattr(request, "api_key", None)
       if api_key is not None:
           return api_key.is_group_allowed(self) and api_key.has_permission(perms)
       return self.user_has_permission(request.user, perms)
   ```
3. `mojo/apps/account/models/group.py` — fix the now-stale comment in
   `check_view_permission` (group.py:576-585): it claims the override "gates SAVE
   as well as VIEW". After the fix, saves route to `check_edit_permission`; the
   ApiKey branch here now only backstops the FK **attach-by-pk** view check and
   GET. Reword to reflect that (still confine keys on the view path).
4. Docs + CHANGELOG (see below).

### Design decisions
- **Classify by the keys list, not a new call-site kwarg.** The original bug
  writeup worried that inferring write-ness from keys would break member FK
  attach (`group=<pk>`). Recon disproves it: attach-by-pk (rest.py:1453) passes a
  **bare `"VIEW_PERMS"` string** → classified view → member attach still works;
  only the FK **dict-save** (rest.py:1409) passes the write list, and that path
  actually rewrites the Group's fields (`{"group":{"name":...}}`) — a second door
  to the *same* hole, which we *want* closed. Keys-based classification closes
  the dict-save door and leaves attach open automatically, with zero call-site
  edits. A kwarg would be more code and would have to special-case 1409 back
  open (wrong).
- **Delegate the user path to `Group.user_has_permission`** rather than
  re-implementing global-or-member — it's the same tested helper `GroupMember`
  saves already use, so Group writes and GroupMember writes gate identically
  (fixes the inconsistency the bug noted).
- **Hookless models are unaffected.** For any model without a view/edit hook, a
  write op already fell through to the owner/group/flat branches with
  `perms=SAVE_PERMS`; the restructure doesn't change that (it only stops running
  a *nonexistent* view hook). Only Group (view hook) and User (edit hook) models
  change routing, and only Group changes *behavior*.
- **Strictly fail-closed.** Post-fix allowed-set for Group writes = global
  SAVE_PERMS holders ∪ members holding SAVE_PERMS ∪ confined keys with the perm —
  a strict subset of the pre-fix set (which included *every* active member). No
  path is loosened.

### Edge cases & risks
- **FK dict-save `{"group":{...}}` (rest.py:1409) now tightens** — a member
  without manage_group can no longer inline-edit a Group via another model's
  save; the attach is silently skipped with an `fk_attach_denied` incident. This
  is the intended closing of the second door; no test/internal caller relies on
  it (recon confirmed).
- **POST_SAVE_ACTIONS tighten with save** — `on_action_realtime_message` (no
  internal perm check) is no longer reachable by a plain member; now needs
  SAVE_PERMS. `disable`/`reactivate` already require global `manage_groups`
  additively (unchanged). Flag in CHANGELOG.
- **Write-denial event type** flips from `view_permission_denied` to
  `edit_permission_denied` for Group. No test asserts the write-denial event on
  Group; the only Group event test (`test_models/permission_events.py`) is a
  GET (view) and still emits `view_permission_denied`.
- **User unchanged:** view op → no view hook → edit hook (its documented
  VIEW-by-pk gate); write op → edit hook. Same as today.
- **ApiKey:** zero-perm/unconfined denied (DM-019 bar preserved); confined key
  with perm allowed.
- **Downstream Maestro:** plain-member workspace rename starts 403ing (accepted,
  owner ruling 2026-07-10); workspace admins hold member-level manage_group and
  keep working.

### Tests
Add `tests/test_account/test_group_save_perms.py` (testit; `@th.django_unit_test`;
follow the strict_posture.py:250-303 template — `Group.objects.create`,
`grp.add_member(user)`, `member.add_permission(...)`, `clear_rate_limits(ip=IP,
key="login")`, `opts.client.login`, try/finally cleanup, delete-before-create,
every assert carries a message with `opts.client.last_response.body`):
- **plain member (no member perms) POST `/api/group/<pk>` `{"name":"hacked"}` →
  403**, and `grp.refresh_from_db()` shows name unchanged. (the core regression)
- **member with member-level `manage_group` POST `{"name":"renamed-ok"}` →
  200**, name changed in DB.
- **plain member GET `/api/group/<pk>` → 200** and the response is the **basic**
  graph downgrade — assert `"auth_domain"` and `"metadata"` are absent from
  `resp.response.data` (both are default-graph-only; basic graph =
  id/uuid/name/created/modified/last_activity/is_active/kind per group.py:71-85).
- (optional, closes a coverage gap) confined ApiKey holding `manage_group` POST
  `{"name":...}` → 200; leave the zero-perm-denied case to the existing
  apikey_groupless.py test.
Regression check: `strict_posture.py::test_group_strict_requires_global_perm` and
`apikey_groupless.py::test_apikey_cannot_write_own_group_without_perm` must stay
green. Baseline the suite green first (build-baseline rule), then full-suite after.

### Docs
- `docs/django_developer/account/group.md` (~:204-220) — the hook note block:
  add that `check_view_permission` governs **reads** (member basic-graph
  downgrade) and the new `check_edit_permission` governs **writes** (member needs
  a SAVE_PERMS grant; no downgrade); fix the SAVE_PERMS list to include
  `"groups"`.
- `docs/django_developer/core/mojo_model.md` (~:318-329) — Permission Flow:
  reads consult `check_view_permission`, writes consult `check_edit_permission`;
  classification is by write-shaped keys (CREATE/SAVE/DELETE_PERMS).
- `docs/django_developer/core/permissions.md` — VIEW/SAVE section + Group row in
  the model-perms table.
- `docs/web_developer/account/group.md` — `POST /api/group/<pk>` now requires a
  member-level (or global) `manage_group`/`groups` grant, not mere membership;
  member `realtime_message` action likewise.
- `CHANGELOG.md` (Unreleased, `**security**`) — behavior change: Group writes +
  POST_SAVE_ACTIONS require SAVE_PERMS; plain-member rename now 403; FK dict-save
  `{"group":{...}}` inline edit tightens; Maestro-accepted.
  (`/build` spawns docs-updater over the diff; this list is its checklist.)

### Open questions
- none.

## Notes
- **Baseline (build-baseline rule), default suite `bin/run_tests --agent`:**
  2715 total / 2334 passed / **0 failed** / 381 skipped (test_incident 243 +
  test_security 82 skipped = opt-in `--full` modules). `var/test_failures.json`
  failures: 0. Green — every post-change failure is mine.
- Session-start tree state: the working tree carried **uncommitted, unrelated**
  GROUP_FIELD tenant-scoping work (mojo/models/rest.py, two docs, CHANGELOG.md,
  tests/test_global_perms/apikey_groupless.py, untracked
  tests/test_fileman/9_test_rendition_group_field.py). Because my fix also edits
  rest.py + CHANGELOG.md, I committed that complete unit FIRST as its own commit
  (`563e430`) after confirming the baseline green — so the DM-027 commit stays
  clean. Left untouched: a stray `pyproject.toml` 1.2.44→1.2.45 bump + `uv.lock`
  churn (a separate version unit; CHANGELOG not re-headered → not a completed
  release; not mine to commit).

## Build outcome
- **Implemented** in commit `4137760`: keys-based write/read classification in
  `MojoModel._evaluate_permission` (write = keys contain CREATE/SAVE/DELETE_PERMS
  → skip `check_view_permission`, use `check_edit_permission`); new
  `Group.check_edit_permission` (global OR member-level SAVE_PERMS via
  `user_has_permission`; ApiKey `is_group_allowed(self) AND has_permission`).
  Chose keys-based classification over the item's mooted kwarg — recon proved FK
  attach-by-pk (rest.py:1453) passes a bare `"VIEW_PERMS"` string (stays a read,
  member attach unaffected) while only FK dict-save (rest.py:1409, a real inline
  Group write) carries the write list and correctly tightens. Zero call-site
  edits.
- **Post-build agents (all green):**
  - test-runner: full default suite 2719 total / 2338 passed / **0 failed** / 381
    skipped (baseline 2334 + 4 new; +1 more added post-review = suite now 2339
    passed once re-run). Every at-risk module green.
  - security-review: **"Sound. Fails closed. No new bypass, no over-tightening."**
    Traced every caller; confirmed classification airtight, `check_edit_permission`
    denies plain member / zero-perm key / cross-tenant / cross-group; POST_SAVE_ACTIONS
    tightened; User + hookless models byte-for-byte unchanged. Surfaced (a) a
    pre-existing latent gap — `on_rest_handle_batch` skips per-instance checks,
    unreachable today (no model sets `CAN_BATCH`) → filed
    `planning/inbox/batch-save-skips-instance-permission-checks.md`; (b) a coverage
    nicety → added the cross-tenant-key-write-denied test.
  - docs-updater: both tracks in sync; my 4 doc edits correct; cross-link anchor
    resolves; no other stale docs; no README/index change needed.

## Resolution
- closed: 2026-07-10
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/README.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/geofence.md,docs/django_developer/account/geoip.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/helpers/settings.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/testit/Overview.md,docs/web_developer/account/README.md,docs/web_developer/account/admin_portal.md,docs/web_developer/account/geofence.md,docs/web_developer/account/geoip.md,docs/web_developer/account/group.md,docs/web_developer/account/login_events.md,docs/web_developer/core/authentication.md,docs/web_developer/core/request_response.md,docs/web_developer/security/README.md,memory.md,mojo/__init__.py,mojo/apps/account/models/group.py,mojo/apps/account/models/setting.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/services/geofence/engine.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/migrations/0031_alter_ipset_source.py,mojo/apps/incident/models/ipset.py,mojo/decorators/auth.py,mojo/decorators/http.py,mojo/helpers/geoip/detection.py,mojo/helpers/geoip/threat_intel.py,mojo/helpers/request_parser.py,mojo/helpers/settings/helper.py,mojo/models/rest.py,mojo/rest/info.py,planning/.next_id,planning/confirmed/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/geofence-hardening.md,planning/inbox/geofence-settings-write-validation-gap.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/member-perms-ignore-group-is-active.md,pyproject.toml,tests/test_account/test_group_save_perms.py,tests/test_fileman/9_test_rendition_group_field.py,tests/test_geofence/_helpers.py,tests/test_geofence/evidence_plane.py,tests/test_geofence/member_visibility.py,tests/test_geofence/settings_validation.py,tests/test_geofence/strict_posture.py,tests/test_geofence/threat_cache.py,tests/test_global_perms/apikey_groupless.py,tests/test_helpers/settings_coercion.py,tests/test_middleware/group_param_is_active.py,tests/test_middleware/request_data_merge.py,uv.lock
- tests added: tests/test_account/test_group_save_perms.py — 5 cases: plain member save→403 (unchanged), plain member GET→200 basic-graph downgrade, manage_group member save→200, confined ApiKey+perm save→200, ApiKey confined to group A cannot write group B→403
