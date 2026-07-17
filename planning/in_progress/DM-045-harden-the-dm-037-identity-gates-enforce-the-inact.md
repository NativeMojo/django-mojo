---
# id is assigned by /scope on pickup — leave it blank
id: DM-045
type: chore
title: Harden the DM-037 identity gates — enforce the inactive-group invariant above instance hooks, unify the two machine-identity idioms, extract the duplicated decorator gate, drop the dead group_id guard
priority: P3
effort: M
owner: backend
opened: 2026-07-16
depends_on: []
related: [DM-037, DM-019, DM-016]
links: []
---

# Harden the DM-037 identity gates — enforce the inactive-group invariant above instance hooks, unify the two machine-identity idioms, extract the duplicated decorator gate, drop the dead group_id guard

## What & Why
Four hardening/cleanup items from the DM-037 post-close code review
(adversarially verified 2026-07-12; sites unchanged as of 2026-07-16). None
is an exploitable bug today — they are structural weaknesses that make the
next change likelier to reopen the class DM-037 closed.

1. **The inactive-group invariant on detail ops is per-hook convention, not
   structural.** `_evaluate_permission` dispatches to instance hooks
   (`check_view_permission` rest.py:~260, `check_edit_permission` :~270),
   which return BEFORE the `api_key.group_inactive` gate (:~299). In-repo
   `Group` was patched via `is_group_allowed`'s active check, but any future
   model hook that grants via `api_key.has_permission(perms)` directly (the
   obvious pattern to copy — it was Group's own pre-DM-037 shape) silently
   bypasses the gate on detail GET/save/delete: the exact self-reversible-
   suspension class DM-037's follow-up fixed. Fix at the right altitude:
   gate the resolved instance's group for api_key identities BEFORE
   dispatching hooks in `_evaluate_permission` (or, minimally, a contract
   test asserting every group-scoped instance hook's api_key branch routes
   through `is_group_allowed`).
2. **Two machine-identity idioms guard one invariant.** The decorators key
   on `not hasattr(request.user, "is_request_user")`
   (`mojo/decorators/auth.py:42,99`); model security keys on
   `hasattr(request, 'api_key') and request.api_key`
   (`mojo/models/rest.py:~298,~326`). They coincide today (ApiKey is the
   only machine identity and validate_token always sets `request.api_key`),
   but a future non-User bearer identity that doesn't set `request.api_key`
   would be fail-closed at the decorators yet routed to rest.py's USER
   branch, where its self-claimed perms authorize with no group confinement
   and no inactive-group gating — the DM-019/DM-037 protections silently
   would not apply. Align both layers on one predicate (the
   `is_request_user` marker / a shared helper).
3. **The decorator gate is copy-pasted verbatim** (`auth.py:42-46` and
   `:99-103`) — a security gate whose next edit lands in one clone and not
   the other. Extract one module-level helper. The helper is also the right
   home for the `or not group.is_active` half-condition, which is currently
   unreachable (every pre-decorator `request.group` assignment is
   active-only) but is worthwhile defense-in-depth on a security boundary —
   keep it, documented once, instead of implied-load-bearing twice.
4. **Dead guard in validate_token:** `api_key.group_id and` at
   `api_key.py:~324` can never be falsy — the group FK is non-nullable
   (api_key.py:~39, no `null=True`) and the row is always DB-loaded via
   `select_related("group")`. The condition implies a fictional null-group
   key variant a future reader may design around (or "complete" by making
   the FK nullable). Simplify to
   `api_key.group if api_key.group.is_active else None`.

## Acceptance Criteria
- [ ] For api_key identities, an inactive resolved instance-group is denied
      BEFORE instance hooks run (or a contract test enforces the
      is_group_allowed routing convention on every group-scoped hook) — with
      a regression test using a synthetic model/hook that grants via bare
      `api_key.has_permission`.
- [ ] One shared machine-identity predicate used by both `auth.py`
      decorators and `mojo/models/rest.py` branches; behavior for ApiKey
      unchanged (full suite green, DM-037 regression suite green).
- [ ] The decorator gate exists once (helper), with the defense-in-depth
      `is_active` half-condition kept and documented there.
- [ ] `validate_token`'s dead `group_id` guard removed.
- [ ] No behavior change for real Users, active-group keys, or the
      federation path (`requires_global_perms(..., allow_api_keys=True)`).

## Repro — bugs only
n/a (chore — no current misbehavior; item 1's scenario is only reachable
with a hypothetical future hook, which the regression test will simulate).

## Investigation
All four traced with file:line evidence and verdicts (3× CONFIRMED, 1×
CONFIRMED-forward-looking) by the DM-037 post-close review — see
`mojo/models/rest.py:260-278` vs `:297-309` (hook-before-gate ordering),
`auth.py:42-46/99-103` (verbatim clones; second copy's comment already says
"Same ... gate as requires_perms"), `user.py:297` (`is_request_user` is the
canonical User marker — the framework idiom per DM-016), `api_key.py:39`
(non-nullable FK) + `:305` (select_related load). Constraint to respect:
`ApiKey.has_permission` itself must NOT gain group/active awareness — the
federation path (`requires_global_perms`, geoip `/sync`) depends on it
working with no group context (DM-037 owner ruling).

## Plan

### Goal
Make the four DM-037-adjacent identity gates structural (one predicate, one
choke point, one copy) instead of per-site convention — with zero behavior
change for real Users, active-group keys, and the federation path.

### Context — what exists (verified 2026-07-17)

- **`mojo/models/rest.py:202` `_evaluate_permission(cls, request, permission_keys, instance=None)`**
  — the single model-security evaluator. Flow (line refs current as of scoping):
  - `:221-223` empty perms → allow. `:234-240` unauthenticated deny (unless
    `"all"`). `:242-243` `"authenticated" in perms` → allow.
  - `:245-278` **instance hooks run first**: read → `instance.check_view_permission`
    (`:260`), write → `instance.check_edit_permission` (`:270`); each returns
    allow/deny directly.
  - `:286-295` instance group re-bind: `GROUP_FIELD` via
    `cls._resolve_group_from_instance(instance, GROUP_FIELD)` (defined `:1926`),
    else `instance.group` → written into `request.group`.
  - `:297-317` group-scoped ApiKey branch — keys on
    `hasattr(request, 'api_key') and request.api_key`; contains the DM-037
    follow-up gate `branch="api_key.group_inactive"` (`:299-309`), then
    `request.api_key.has_permission(perms)`.
  - `:326-359` groupless ApiKey branch (`api_key.groupless_denied` /
    `ALLOW_API_KEY_GLOBAL`), `:360-373` plain-user fallthrough
    (`user.has_permission`).
  - `is_group_scoped` (`:231-232`) = `bool(GROUP_FIELD) or hasattr(cls, "group")`.
- **`mojo/decorators/auth.py`** — the ApiKey active-group-context gate is
  **copy-pasted verbatim** at `:42-46` (`requires_perms`) and `:99-103`
  (`requires_group_perms`):
  ```python
  if not hasattr(request.user, "is_request_user"):
      group = getattr(request, "group", None)
      if group is None or not group.is_active:
          logger.error(f"{getattr(request.user, 'username', request.user)} has no active group context for {perms}")
          raise mojo.errors.PermissionDeniedException()
  ```
  The `not group.is_active` half is currently unreachable (every pre-decorator
  `request.group` assignment is active-only) but is deliberate defense-in-depth.
  `requires_global_perms` uses the same predicate at `:171`
  (`not allow_api_keys and not hasattr(user, "is_request_user")`).
- **`mojo/apps/account/models/api_key.py:296` `validate_token`** — `:324`:
  `request.group = api_key.group if (api_key.group_id and api_key.group.is_active) else None`.
  The `api_key.group_id and` is dead: the `group` FK is non-nullable (`:38-39`,
  no `null=True`) and the row is loaded via `select_related("group")` (`:306`).
- **`mojo/apps/account/models/group.py`** — `Group` is **NOT group-scoped**
  (no `group` FK, no `GROUP_FIELD`); its inactive protection lives inside its
  own hooks `check_view_permission` (`:575`) / `check_edit_permission` (`:599`),
  both routing the api_key branch through `api_key.is_group_allowed(self)`
  (`api_key.py:191`, active-check included). The new gate therefore does not
  apply to Group and must not change its behavior.
- **`mojo/middleware/auth.py:12,38-45`** — `AUTH_BEARER_HANDLERS` is a
  settings-driven extension point: a deployment can register a custom
  `Authorization:` scheme whose handler returns ANY identity object. This makes
  item 2 concrete, not hypothetical: a custom identity without
  `is_request_user` that doesn't set `request.api_key` currently lands in
  rest.py's USER branch and authorizes on its self-claimed `has_permission`
  with no group confinement.
- **`mojo/helpers/request.py`** — existing request-helpers module; natural home
  for the shared predicate. `mojo/apps/account/models/user.py:297` defines
  `is_request_user` — the canonical "real request User" marker (DM-016 idiom).
- **Existing DM-037 regression suite**: `tests/test_global_perms/apikey_group_inactive.py`
  (8 tests, incl. in-process `_evaluate_permission` tests that build requests as
  `objict(user=key, api_key=key, group=..., DATA=objict())` — copy that style).
  One test asserts `denial2.branch == "api_key.groupless_denied"` (`:316`);
  no test pins the `api_key.group_inactive` branch string, so the early gate may
  reuse it. `tests/test_global_perms/_helpers.py` provides `use_apikey(opts, token)`.

### Changes — what to do

1. **`mojo/helpers/request.py` — add the shared machine-identity predicate** (item 2):
   ```python
   def is_request_user(request):
       # The framework's one predicate for "a real logged-in User is driving
       # this request" (vs a machine identity: ApiKey or a custom
       # AUTH_BEARER_HANDLERS identity). User defines the is_request_user
       # marker (account/models/user.py); machine identities must not.
       user = getattr(request, "user", None)
       return user is not None and hasattr(user, "is_request_user")
   ```

2. **`mojo/decorators/auth.py` — extract the duplicated gate** (item 3):
   - Add one module-level helper, e.g. `_deny_machine_identity_without_active_group(request, perms)`,
     containing the verbatim block from `:42-46`. Move the ITEM-037 comment
     (`:35-41`) onto it, plus one line documenting that the
     `not group.is_active` half is unreachable-today defense-in-depth (kept
     deliberately; see DM-045).
   - Use `is_request_user(request)` (helper from change 1) as its predicate.
   - Replace both inline clones (`requires_perms` `:42-46`,
     `requires_group_perms` `:99-103`) with a call to the helper.
   - In `requires_global_perms` (`:171`) replace
     `not hasattr(user, "is_request_user")` with `not is_request_user(request)`
     (same semantics; unified predicate).

3. **`mojo/models/rest.py` `_evaluate_permission` — machine-identity fail-closed guard** (item 2):
   After the `"authenticated" in perms` short-circuit (`:242-243`) and before
   the `if instance is not None:` block, add:
   ```python
   # An authenticated MACHINE identity (no is_request_user marker) that did
   # not register itself as request.api_key has no branch here — falling
   # through to the User branches would authorize its self-claimed perms with
   # no group confinement or inactive-group gating (DM-019/DM-037). Fail
   # closed. ApiKey always sets request.api_key (validate_token), so this is
   # a no-op today; it exists for custom AUTH_BEARER_HANDLERS identities.
   ```
   Condition: `request.user is authenticated` AND `not is_request_user(request)`
   AND `not getattr(request, 'api_key', None)` → deny with
   `branch="non_user_no_api_key"`, `event_type="user_permission_denied"`,
   `status=403`. (Note `"all" in perms` returns earlier only when combined with
   the checks above it — trace the exact early-returns when placing this; it
   must not fire for unauthenticated/anonymous requests, which lack
   `is_authenticated=True`.)

4. **`mojo/models/rest.py` `_evaluate_permission` — inactive-group gate ABOVE instance hooks** (item 1):
   At the top of the `if instance is not None:` block (before the
   `WRITE_KEYS` classification at `:254`), add: if the model
   `is_group_scoped` and the identity is an api_key
   (`getattr(request, 'api_key', None)`), resolve the instance's owning group —
   `cls._resolve_group_from_instance(instance, GROUP_FIELD)` when `GROUP_FIELD`,
   else `getattr(instance, "group", None)` — and if that group exists and is
   NOT active, deny immediately with the existing taxonomy
   (`branch="api_key.group_inactive"`, `event_type="user_permission_denied"`,
   `status=403`) before any `check_view_permission`/`check_edit_permission`
   dispatch. Do NOT remove the existing `:299-309` gate — it still covers the
   no-instance (list re-bind) path; add a cross-reference comment. Comment must
   also flag the one-way-door interaction (see Design decisions #4).

5. **`mojo/apps/account/models/api_key.py:324` — drop the dead guard** (item 4):
   ```python
   request.group = api_key.group if api_key.group.is_active else None
   ```
   (FK is non-nullable and select_related-loaded; adjust the comment above it
   if it references the old condition.)

6. **`CHANGELOG.md`** — one entry under the current rolling block: internal
   security hardening of the DM-037 identity gates; no API/behavior change.

### Design decisions

1. **Predicate = absence of `is_request_user`, not presence of `request.api_key`.**
   `is_request_user` is the canonical marker (DM-016; `user.py:297`) and is the
   fail-closed direction: an unknown identity is treated as a machine. Keying on
   `request.api_key` would silently trust any identity that forgets to set it —
   the exact hole item 2 closes.
   The helper lives in `mojo/helpers/request.py` (no model imports → no import
   cycles; decorators and models can both import it).
2. **rest.py guard denies rather than reroutes.** A machine identity without
   `request.api_key` cannot be group-confined (we don't know its group), so
   routing it to the api_key branches is impossible and routing to USER branches
   is the vulnerability. Deny + distinct branch string makes a future custom
   identity's integration failure loud and diagnosable.
3. **Item 1 gate reuses `branch="api_key.group_inactive"`.** Same invariant,
   same taxonomy as the existing `:299` gate — dashboards/tests see one denial
   class, and no existing test pins the branch to a specific code site. The
   existing gate stays (covers list-path re-binds where `instance is None`).
4. **One-way-door interaction (flagged, not resolved here).** Inbox item
   `apikey-parent-key-inactive-descendant-one-way-door` wants a PARENT-group
   key to read/reactivate an inactive DESCENDANT group. Item 1's gate cements
   that wall for group-scoped models' rows (Group itself is not group-scoped, so
   Group-row lifecycle is untouched — that item's fix lives in
   `is_group_allowed`/the Group hooks). If that item is later scoped to also
   cover rows owned by inactive descendants, its carve-out belongs in this new
   gate (e.g. allow when `api_key.group` is active and a proper ancestor of the
   instance group). The gate's comment must point there. Do not pre-build the
   carve-out.
5. **Chose the structural gate over the contract-test-only alternative** the
   item offered: the gate is ~10 lines at the correct altitude and protects
   third-party apps' models too, which no in-repo contract test can.
6. **`ApiKey.has_permission` stays group/active-unaware** — DM-037 owner
   ruling; the federation path (`requires_global_perms(..., allow_api_keys=True)`,
   geoip `/sync`) calls it with no group context.
7. **Scope discipline:** only the two authorization layers move to the shared
   predicate (auth.py ×3, rest.py guard). The other `is_request_user` hasattr
   sites (`limits.py:467`, `sms.py:42`, `group.py:216,282`, `rest/user.py:30`,
   `rest.py:283,539`) are read-only marker checks, not the duplicated gate —
   leave them.

### Edge cases & risks

- **Anonymous requests**: `AnonymousUser` lacks `is_request_user` but also
  `is_authenticated` — the rest.py guard requires authenticated, so anonymous
  flows are untouched (`:234-240` handles them first).
- **`"all"` / `"authenticated"` perms**: both return before the new guard —
  public/authenticated-only models keep working for any identity.
- **Federation path**: `requires_global_perms(..., allow_api_keys=True)` skips
  the predicate entirely (`:171` short-circuits on `allow_api_keys`) — unchanged.
- **Group self-access**: Group is not group-scoped → item 1 gate never fires for
  Group rows; existing `is_group_allowed` hook protection (and its DM-037
  regression tests) unchanged.
- **Null instance group** (group-FK model with a null-group row): gate only
  denies when a group resolves AND is inactive; null falls through to the
  existing groupless-deny branch exactly as today.
- **In-process tests that build `objict` requests**: `objict` auto-creates
  missing attrs? No — `getattr(request, 'api_key', None)` on an objict without
  the key returns None (objict returns None for missing attrs), which is the
  correct "no api_key" reading. Existing suite tests pass `api_key=key`
  explicitly.
- **Test that monkeypatches `Setting.RestMeta`** asserts the
  `api_key.groupless_denied` branch — unaffected (its request has `group=None`,
  no instance).

### Tests
Add to `tests/test_global_perms/apikey_group_inactive.py` (same in-process
style; follow `.claude/rules/testing.md` — descriptive assert messages,
cleanup-first setup):

1. **Item 1 regression — naive hook cannot bypass the inactive gate.**
   In-process `_evaluate_permission` test: take a group-scoped model instance
   (an `ApiKey` row itself is group-scoped — `hasattr(cls, "group")`) owned by
   a group, attach a naive instance hook that grants via bare
   `request.api_key.has_permission(perms)` (attach on the instance:
   `inst.check_view_permission = lambda perms, request: request.api_key.has_permission(perms)`
   — instance attribute, so no class pollution; `hasattr(instance, ...)` at
   `:260` finds it). With the group ACTIVE → allowed (control). Deactivate the
   group → must deny with `branch == "api_key.group_inactive"`. This test FAILS
   on current code (hook runs first and grants) and passes with the gate —
   the required regression.
2. **Item 2 guard — unregistered machine identity fails closed.**
   Build a minimal fake identity (`objict(is_authenticated=True,
   has_permission=lambda perms: True, username="custom:1")` — deliberately no
   `is_request_user`, request has NO `api_key`) and call
   `SomeModel._evaluate_permission(req, "VIEW_PERMS")` on (a) a groupless model
   and (b) a group-scoped model with `group=None` — both must deny with
   `branch == "non_user_no_api_key"`. Control: a real User-marker identity
   still reaches `user.has_permission`.
3. **Items 3+4** — covered by the existing DM-037 suite staying green
   (`test_requires_perms_denies_key_without_active_group` exercises both
   decorator clones; `test_validate_token_strips_inactive_group` exercises the
   `:324` line both directions). No new tests needed; do not weaken existing ones.

Run: `bin/run_tests --agent -t test_global_perms.apikey_group_inactive`, then
the default suite per `.claude/rules/build-baseline.md` (baseline BEFORE any
edit).

### Docs
- `CHANGELOG.md`: one internal-hardening entry.
- No `docs/django_developer/` or `docs/web_developer/` changes — no API surface,
  permission, or configuration change. (If the builder finds
  `AUTH_BEARER_HANDLERS` documented in django_developer docs, add one sentence
  there: a custom identity must set `request.api_key` or define the
  `is_request_user` marker, else model security denies it.)

### Open questions
none

## Notes
- **Baseline (2026-07-17, before first edit)**: `bin/run_tests --agent` →
  status=passed, total=2503, passed=2447, failed=0, skipped=56 (opt-in modules
  test_incident/test_security etc. — normal). All-green: every post-change
  failure is ours.
- Two review leftovers deliberately NOT in scope here: test-fixture
  boilerplate in `tests/test_global_perms/apikey_group_inactive.py` (nice-to-
  have contextmanager, fold in only if touching that file anyway) and the
  `event_type` taxonomy nit on the `api_key.group_inactive` denial
  (PLAUSIBLE-only; matches the adjacent groupless-denied branch, so arguably
  deliberate).
- The unthrottled `ALLOW_API_KEY_GLOBAL` logit.error + wrong "group FK"
  message are in the sibling bug item (`apikey-suspension-residual-surfaces.md`),
  not here.
