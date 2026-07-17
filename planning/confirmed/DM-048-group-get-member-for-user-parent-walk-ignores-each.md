---
# id is assigned by /scope on pickup — leave it blank
id: DM-048
type: bug
title: Deactivating a parent group must disable its entire subtree — dynamic effective-activeness (is_effectively_active) routed through every group gate
priority: P2
effort: M
owner: backend
opened: 2026-07-16
depends_on: []
related: [DM-039, DM-037, DM-025, DM-045]
links: []
---

# Deactivating a parent group must disable its entire subtree

## What & Why
Originally filed as: `Group.get_member_for_user(user, check_parents=True)`
(`mojo/apps/account/models/group.py:264-309`) walks up to 8 parent levels and, at
each level, filters the **membership row's** `is_active` but never checks the
**parent group's** `is_active` — so an active membership in a deactivated parent
still authorizes against an active child. **Verified by /scope 2026-07-17**: the
walk never consults `self.is_active` or `current.is_active`, and all 12 production
call sites pass `check_parents=True` (perm checks, realtime topics, assistant
tiers, member/api-key `can_change_permission`).

**Rescoped by user ruling (2026-07-17)** to the stricter, general contract: *if a
parent is disabled, all its children are disabled.* A group is **effectively
active** only if it AND every ancestor is active. Enforced **dynamically** (no
flag cascade — cascading writes create a reactivation one-way door, the same trap
that parked the api-key descendant item). Deactivating a parent instantly darkens
the subtree (memberships, API keys, group resolution); reactivating it instantly
restores individually-active children.

This extends DM-025 ("inactive == nonexistent"), DM-037 (deactivation instantly
suspends API keys — now for whole subtrees), and composes with DM-045's landed
structural gates. It **deliberately overturns** the documented per-group
carve-outs in `ApiKey.is_group_allowed` / `ApiKey.get_groups` ("an active child
under an inactive parent stays reachable") and the test asserting them.

## Acceptance Criteria
- [ ] New single owner of the contract: `Group.is_effectively_active(max_depth=8)`
      — False on the first inactive ancestor (or self), True on a clean chain.
- [ ] `Group.get_active`, the `get_member_for_user` walk, `validate_token`,
      `is_group_allowed`, `get_groups`, the model-security gates, the decorator
      gate, dispatcher `group_uuid`, registration, geofence, OAuth state, and the
      auth-domain cache read all route through effective activeness.
- [ ] A membership in a deactivated ancestor no longer authorizes anywhere; a
      **direct** membership in an active child of a deactivated ancestor no longer
      authorizes either (subtree rule).
- [ ] Fully-active-chain inheritance unchanged; `is_active=False` admin path in
      `get_member_for_user` unchanged.
- [ ] Reactivating the parent restores child access with no one-way door.
- [ ] Regression tests covering the contract (see Plan → Tests).

## Repro — bugs only
1. Active child group C under deactivated parent P; user U is an active member of P
   only. U calls `GET /api/group/<C.pk>/member`.
- Expected: denied — wire-identical to nonexistent/inactive, zero touch writes.
- Actual (verified): 200 with U's P-membership record; C and the membership
  `touch()`ed.

## Plan

### Goal
A group is *effectively active* only if it and every ancestor are active —
deactivating a parent instantly disables its whole subtree (memberships, API
keys, group resolution), dynamically, with no flag cascade and no reactivation
one-way door.

### Context — what exists
- `Group.get_active(pk)` — `mojo/apps/account/models/group.py:223-232` — own-flag
  only (`cls.objects.filter(pk=pk, is_active=True).first()`); DM-025's single
  owner of "inactive == nonexistent" (silent `None`, no oracle, no touch).
  Callers: dispatcher `?group=` (`mojo/decorators/http.py:86`), perm fallbacks
  (`mojo/decorators/auth.py:77,129`), member endpoint
  (`mojo/apps/account/rest/group.py:109`).
- `Group.get_member_for_user(user, check_parents=False, is_active=True,
  max_depth=8)` — `group.py:264-309`. Direct check then parent walk; the
  `is_active` param filters **membership rows only**
  (`current.members.filter(user=user, is_active=True)`); `self.is_active` /
  `current.is_active` never consulted. Each `current` Group object is already
  loaded in-memory during the walk. 12 production call sites, all
  `check_parents=True`: `user_has_permission` (`group.py:218`),
  `check_view_permission` (`group.py:589`), member endpoint
  (`rest/group.py:112`), `ApiKey.can_change_permission` (`api_key.py:161`),
  `GroupMember.can_change_permission` (`member.py:90`), `User.can_access_topic`
  (`user.py:1500` — resolves the group with **no** is_active filter at
  `user.py:1497`), assistant app ×6 (`assistant/handler.py:158`,
  `services/skills.py:57,74,447`, `services/memory.py:156,173` — handler resolves
  via `Group.objects.get(pk=...)`, no is_active).
- DM-037/DM-045 gates, all own-flag today:
  - `ApiKey.validate_token` — `api_key.py:326`:
    `request.group = api_key.group if api_key.group.is_active else None`
  - `ApiKey.is_group_allowed` — `api_key.py:191-206`: `if group is None or not
    group.is_active: return False`, then pk match or `group.is_child_of(self.group)`.
    Carries a comment that an active child under an inactive parent still passes —
    **to be overturned**.
  - `ApiKey.get_groups` — `api_key.py:234,238`: querysets filtered
    `is_active=True` per-group; docstring says active child stays reachable —
    **to be overturned**.
  - Model-security structural gates — `mojo/models/rest.py:281` (pre-hook
    instance-group gate) and `:340` (post-rebind gate), both
    `not <group>.is_active`, both `branch="api_key.group_inactive"` (DM-045).
  - Decorator gate — `mojo/decorators/auth.py:34`
    (`_deny_machine_identity_without_active_group`; defense-in-depth).
- Other own-flag resolutions: dispatcher `group_uuid` branch (`http.py:118`,
  `Group.objects.filter(uuid=group_uuid, is_active=True)`), registration
  (`mojo/apps/account/rest/user.py:314-316`, raises "Group is not active"),
  geofence pre-flight (`rest/geofence.py:75`, inactive → system rules only),
  OAuth `group_from_state` (`rest/oauth.py:95`, inactive → None),
  `Group.resolve_by_auth_domain` (`group.py:784,788`) backed by a 24h Redis cache
  (`group.py:746-768` invalidates only on the group's **own** save — an ancestor
  flip cannot purge it).
- Deactivation surfaces: REST save of `is_active`, `on_action_disable` /
  `on_action_reactivate` (`group.py:625-656`). `on_rest_saved`
  (`group.py:709-731`) only recomputes a global metric and the group's own
  auth-domain cache — **no cascade exists today**; the check must be dynamic.
- No `is_effectively_active`-like helper exists anywhere; no request-scoped
  memoization helper in `mojo/helpers/` (Redis via `mojo/helpers/redis/` is the
  only caching precedent).
- Group `RestMeta` — `group.py:46-122`: `LIST_DEFAULT_FILTERS = {"is_active":
  True}` (own flag, SQL-level). Custom list handler `on_rest_handle_list`
  (`group.py:802-851`); `User.get_groups` (`user.py:349-371`) expands children
  with per-group is_active only.
- DM-039's endpoint (`rest/group.py:102-122`): gates the child via
  `Group.get_active(pk)`, one `PermissionDeniedException` raise site,
  `touch()`es only after membership confirms.

### Changes — what to do
1. `mojo/apps/account/models/group.py` — add:
   ```python
   def is_effectively_active(self, max_depth=8):
       # a group counts as active only if it AND every ancestor is active;
       # depth-capped like get_member_for_user (also guards parent cycles)
       current = self
       depth = 0
       while current is not None and depth <= max_depth:
           if not current.is_active:
               return False
           current = current.parent
           depth += 1
       return True
   ```
   The sole definition of the contract — no site re-implements the walk.
2. `group.py` `get_active` — after own-flag resolution, return `None` unless
   `group.is_effectively_active()`. Fixes dispatcher `?group=`, both perm-fallback
   decorators, and the member endpoint in one move. Same silent-`None` shape (no
   new oracle).
3. `group.py` `get_member_for_user` — when `is_active=True`, gate at the top:
   `if not self.is_effectively_active(): return None` (before the direct-member
   query). One check covers self AND every parent level — if the chain from self
   to root is clean, every walked parent is active by definition — fixing all 12
   call sites including the ones that resolve the group with no is_active filter
   (realtime topics, assistant app) without touching those files.
   `is_active=False` keeps raw behavior (admin/introspection).
4. `mojo/apps/account/models/api_key.py` — `validate_token` (`:326`) and
   `is_group_allowed` (`:196`) → `is_effectively_active()`; `get_groups` →
   post-filter resolved ids through `is_effectively_active()` and return
   `Group.objects.filter(id__in=kept_ids)`. Rewrite the two obsolete
   "active child under an inactive parent stays reachable" comments/docstrings.
5. `mojo/models/rest.py:281,340` and `mojo/decorators/auth.py:34` →
   `is_effectively_active()`. Branch string `api_key.group_inactive` unchanged.
6. One-line swaps to the effective check: `mojo/decorators/http.py:118`
   (`group_uuid` branch — filter by uuid then verify effective),
   `mojo/apps/account/rest/user.py:314` (registration),
   `mojo/apps/account/rest/geofence.py:75`, `mojo/apps/account/rest/oauth.py:95`.
7. `group.py` `resolve_by_auth_domain` — verify `is_effectively_active()` on the
   cache-resolved group at read time (the 24h Redis entry can't see ancestor
   flips; per-read verification closes the hole without new invalidation
   plumbing).
8. Tests + docs per sections below. Regression tests first (bug type — they must
   fail on current main), then the fix.

### Design decisions
- **Dynamic check, no flag cascade** — user-ruled 2026-07-17. Cascading
  `is_active=False` writes down the subtree creates a reactivation one-way door
  (can't distinguish cascade-disabled from deliberately-disabled children — the
  same trap that parked `planning/future/apikey-parent-key-inactive-descendant-
  one-way-door.md`). Dynamic is instant in both directions.
- **One helper, routed everywhere** — `is_effectively_active` is the single
  owner; every gate delegates.
- **Lists stay own-flag in v1** (`LIST_DEFAULT_FILTERS`, `User.get_groups`,
  children listings): SQL can't cheaply walk ancestors. A child of a deactivated
  parent may still *appear* in lists to users who already hold perms, but every
  resolution/authorization gate denies. Disclosure-to-the-already-permitted, not
  an access grant; pruning lists is a possible follow-up item.
- **Depth cap 8**, matching `get_member_for_user(max_depth=8)`; deeper chains are
  already unsupported for membership. Also cycle protection. Cost ≤8 lazy
  `.parent` loads per check, typically 1-2; accepted, optimize later (CTE /
  caching) only if it shows up.
- **Deliberately overturns** `is_group_allowed`/`get_groups` per-group carve-outs
  and `test_apikey_active_child_still_reachable`
  (`tests/test_global_perms/apikey_group_inactive.py:170`) — contract change per
  user ruling, not a regression.
- `get_member_for_user` gates via one top-of-method check rather than a per-level
  `current.is_active` guard — equivalent under the subtree rule and simpler.

### Edge cases & risks
- API keys of every descendant group go dark when an ancestor is deactivated —
  DM-037 extended to subtrees; instant, reversible via reactivation.
- Registration / OAuth / geofence against a child of a deactivated org behave
  exactly as if the group were inactive (generic deny / system-rules-only) — same
  wire shapes, no new existence oracle.
- Chains deeper than the cap: levels past 8 are not verified (and memberships
  past 8 were never honored) — documented limitation, unchanged.
- Existing all-active-chain tests (`tests/test_auth/accounts.py:213-377`,
  `test_group_me_member_oracle.py`, `group_param_is_active.py`) must stay green.
- Per-request cost: a few extra small queries on group-scoped requests; accepted.

### Tests
testit (`docs/django_developer/testit/Overview.md`); hierarchy fixtures modeled on
`tests/test_auth/accounts.py` REST-built Org→Dept→Team; delete-first setup;
`last_activity=None` to defeat the 300s touch throttle (DM-039 pattern). Bug type
→ regressions must fail on current main before the fix.
- `tests/test_account/test_group_me_member_oracle.py` (extend DM-039 suite):
  1. Original repro: U active member of deactivated P only; active child C →
     `GET /api/group/<C.pk>/member` denied, wire-identical to the other deny
     paths, **zero** touch writes on C and the membership.
  2. Inactive middle: U member of active grandparent, middle parent deactivated →
     denied.
  3. Subtree rule: U **direct** active member of active C under deactivated P →
     denied.
  4. Fully active chain still resolves the ancestor membership (inheritance
     unchanged).
  5. Reactivate P → the denied lookups from (1)/(3) immediately succeed (no
     one-way door).
- `tests/test_middleware/group_param_is_active.py` (extend): `?group=<child-of-
  inactive-parent>` → `request.group is None`, no touch on the child.
- `tests/test_global_perms/apikey_group_inactive.py` (extend + flip): key bound
  to an active child of a deactivated parent → `validate_token` strips group /
  detail+list denied with `branch="api_key.group_inactive"`; **flip**
  `test_apikey_active_child_still_reachable` (`:170`) to assert denial;
  reactivation hierarchy case restores access.
- Direct-model: `C.get_member_for_user(u, check_parents=True)` → `None` under an
  inactive ancestor; same call with `is_active=False` still returns the row
  (admin path preserved).

### Docs
- `docs/django_developer/account/group.md` — Membership (`:196-232`) + hierarchy:
  define effective activeness, subtree deactivation, depth cap, dynamic (no
  cascade) semantics.
- API-key doc touched by DM-037: deactivating an ancestor suspends descendant
  groups' keys.
- `docs/web_developer/` — group deactivation semantics note if a group endpoints
  page exists.
- `CHANGELOG.md` — behavior change entry.

### Open questions
None — the three embedded calls (lists own-flag in v1, api-key test flip,
timing side-channel parked to `planning/future/`) were approved 2026-07-17.

## Notes
- Source: DM-039 post-build security-review (2026-07-16); verified + rescoped to
  the subtree contract by /scope with user ruling (2026-07-17).
- The minor DM-039-review timing side-channel (deny paths wire-identical but not
  equal-cost) is parked at
  `planning/future/group-member-deny-timing-side-channel.md`.
