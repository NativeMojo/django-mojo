---
# id is assigned by /scope on pickup — leave it blank
id: DM-044
type: bug
title: Auth decorators clobber each other's SECURITY_REGISTRY entries — enforced_endpoints under-reports
priority: P2
effort: S
owner: backend
opened: 2026-07-17
depends_on: []
related: [DM-043]
links: []
---

# Auth decorators clobber each other's SECURITY_REGISTRY entries — enforced_endpoints under-reports

## What & Why

Ten decorators in `mojo/decorators/auth.py` register endpoints with a **full
overwrite** — `SECURITY_REGISTRY[key] = {...}` — at lines 22
(`requires_perms`), 85 (`requires_group_perms`), 158 (`requires_global_perms`),
198 (`public_endpoint`), 223 (`custom_security`), 249 (`uses_model_security`),
276 (`token_secured`), 296 (`requires_auth`), 325 (`requires_fresh_auth`),
352 (`requires_bearer`). Only `mojo/decorators/geofence.py::_apply_geofence`
uses the merge pattern (`entry = SECURITY_REGISTRY.get(key, {})` + update).

Decorators apply bottom-up, so whenever one of the ten sits **above**
`@requires_geofence` in a stack (applied later), it wipes the `geofence`
sub-entry. Consequence: `GET /api/geo/rules` → `enforced_endpoints` — sold in
the docs as the compliance artifact for WHERE geofencing is enforced — has
been silently missing most geofenced endpoints (register, forgot,
magic/email sends, sms, totp, passkeys, oauth, handoff, magic/reset/verify/
invite completes...). Roughly only `on_user_login` (and other stacks with no
overwriting decorator above the geofence line) survive.

**Enforcement itself is unaffected** — the wrapper (pre-view) and the DM-043
post-credential checks run regardless of registry state. This is an
audit/visibility bug only, but a real one: the compliance surface lies.

Presumably the same clobbering also loses `type: public` / perms info when
multiple registering decorators stack in other orders — the fix should make
ALL registrations merge, not just geofence's.

## Acceptance Criteria

- [ ] All ten registration sites in `mojo/decorators/auth.py` merge into the
      existing entry instead of overwriting (preserve the `geofence` sub-entry
      and each other's keys).
- [ ] `GET /api/geo/rules` → `enforced_endpoints` lists **every**
      `@requires_geofence` endpoint (pre-view and `after_auth`), regardless of
      decorator stacking order.
- [ ] Regression test: a view decorated `public_endpoint`-above-
      `requires_geofence` (the on_register shape) appears in
      `_enforced_endpoints()`.
- [ ] `tests/test_geofence/config_plane.py` upgraded from `len > 0` to
      asserting known members (e.g. on_register present).
- [ ] No behavior change to actual enforcement, auth, or perms checks — this
      is registry bookkeeping only.

## Repro

1. In a Django shell (or in-process test): import
   `mojo.apps.account.rest.user`, then call
   `mojo.apps.account.rest.geofence._enforced_endpoints()`.
- Expected: includes `...rest.user.on_register` (decorated
  `@md.requires_geofence(scope="auth")` at `user.py:263`).
- Actual: absent — `@md.public_endpoint()` sits above the geofence decorator
  and its registration at `auth.py:198` overwrote the entry.

## Investigation

- Root cause: **confirmed** (found during DM-043's
  `test_registry_annotates_after_auth`, which had to fall back to probe views;
  see the comment in `tests/test_geofence/post_auth.py`).
- Code path: `mojo/decorators/geofence.py::_apply_geofence` (merge, correct)
  vs the ten overwrite sites listed above.
- Fix shape: replace each `SECURITY_REGISTRY[key] = {...}` with the same
  get-merge-update pattern geofence uses. Watch for entries that
  intentionally replace (none apparent — each writes disjoint keys like
  `type`/`requires_auth`/`perms`).
- Regression-test feasibility: easy, in-process (define stacked probe views,
  assert registry contents).

## Plan

### Goal
Make all ten `SECURITY_REGISTRY` registration sites in `mojo/decorators/auth.py`
merge into the existing entry instead of overwriting it, so
`enforced_endpoints` (and any other registry-derived audit surface) stops
silently dropping sub-entries written by other stacked decorators.

### Context — what exists
- `SECURITY_REGISTRY = {}` at `mojo/decorators/auth.py:9` (module-global dict).
  `mojo/decorators/geofence.py:31` imports the **same object**
  (`from .auth import SECURITY_REGISTRY`) — not a copy.
- **Key formula is identical at all 11 sites**:
  `key = f"{func.__module__}.{func.__name__}"`. Every intermediate wrapping
  decorator in the stack uses `functools.wraps` (verified: `bouncer.py:42`,
  `limits.py:179,263`, `geofence.py:72`, `http.py:138`, `validate.py:6`, and the
  wrapping auth.py decorators), so `__name__`/`__module__` propagate and merge
  targets align regardless of stack order.
- **The one correct site** — `mojo/decorators/geofence.py:58-65`
  (`_apply_geofence`) — already does get-merge-write:
  ```python
  key = f"{func.__module__}.{func.__name__}"
  entry = SECURITY_REGISTRY.get(key, {})
  ...
  entry["geofence"] = gf_entry
  SECURITY_REGISTRY[key] = entry
  ```
- **The ten overwrite sites** in `mojo/decorators/auth.py`, each doing
  `SECURITY_REGISTRY[key] = {...}`:

  | Decorator | Line | Keys written |
  |---|---|---|
  | `requires_perms` | 22 | `type='permissions'`, `permissions`, `function`, `requires_auth=True` |
  | `requires_group_perms` | 85 | `type='permissions'`, `permissions`, `function`, `requires_auth=True` |
  | `requires_global_perms` | 158 | `type='permissions'`, `permissions`, `function`, `requires_auth=True`, `global_only=True` |
  | `public_endpoint` | 198 | `type='public'`, `reason`, `function`, `requires_auth=False` |
  | `custom_security` | 223 | `type='custom'`, `description`, `function`, `requires_auth=True` |
  | `uses_model_security` | 249 | `type='model'`, `model_class`, `model_name`, `function`, `requires_auth=True` |
  | `token_secured` | 276 | `type='token'`, `token_types`, `description`, `function`, `requires_auth=False` |
  | `requires_auth` | 296 | `type='authentication'`, `function`, `requires_auth=True` |
  | `requires_fresh_auth` | 325 | `type='fresh_auth'`, `seconds`, `function`, `requires_auth=True` |
  | `requires_bearer` | 352 | `type='bearer_token'`, `bearer_token`, `function`, `requires_auth=False` |

- Decorators apply bottom-up, so an auth decorator **above**
  `@requires_geofence` registers **later** and wipes the `geofence` sub-entry.
- **Live victim**: `on_register` at `mojo/apps/account/rest/user.py:259-264`:
  ```python
  @md.POST("auth/register")
  @md.public_endpoint()
  @md.strict_rate_limit("register", ip_limit=5, ip_window=300)
  @md.requires_bouncer_token('registration')
  @md.requires_geofence(scope="auth")
  def on_register(request):
  ```
  `public_endpoint` runs last → `geofence` lost → `on_register` absent from
  `_enforced_endpoints()`. Same pattern hits most geofenced auth endpoints;
  roughly only `on_user_login` (no overwriting decorator above the geofence
  line) survives today.
- **Consumers of SECURITY_REGISTRY** (all read defensively via `.get()`, so an
  additive merge breaks none of them):
  - `mojo/apps/account/rest/geofence.py:149-163` `_enforced_endpoints()` —
    iterates items, reads `entry.get("geofence")` → `gf.get("scope")` /
    `gf.get("after_auth")`. The primary victim.
  - `tests/test_security/test_routes.py:123-125,233-234,285-286,372-375,1014` —
    reads `type` (one direct-index at ~286 is a pre-existing condition for
    geofence-only entries; the merge fix only makes `type` present more often).
  - `tests/test_global_perms/model_permissions.py:31-37` — asserts
    `entry.get('global_only') is True`; preserved by merge.
- **Existing tests to touch**:
  - `tests/test_geofence/config_plane.py:110` —
    `assert len(d.enforced_endpoints) > 0, ...` (weak; passes only via
    `on_user_login`).
  - `tests/test_geofence/post_auth.py:212-250`
    `test_registry_annotates_after_auth` — probe-view fallback with a comment at
    lines 216-221 documenting this exact bug ("several auth decorators ...
    OVERWRITE the shared SECURITY_REGISTRY entry ... a pre-existing bug (filed
    separately)").
- No real code stacks two auth.py decorators on one view — the overwrite-vs-merge
  difference matters in practice only for the auth-decorator-over-geofence combo.

### Changes — what to do
1. **`mojo/decorators/auth.py`** — add a small module-level helper below the
   `SECURITY_REGISTRY` definition:
   ```python
   def _register_security(func, **info):
       key = f"{func.__module__}.{func.__name__}"
       entry = SECURITY_REGISTRY.get(key, {})
       entry.update(info)
       SECURITY_REGISTRY[key] = entry
   ```
   Replace all ten `SECURITY_REGISTRY[key] = {...}` assignments (lines above)
   with calls to it, passing the exact same keys/values each site writes today.
   One helper instead of ten inline copies — single point of truth for the merge
   invariant; inline repeats are exactly how the bug happened.
2. **`tests/test_geofence/`** — regression test (in-process): define a probe
   view stacked `@md.public_endpoint()` **above**
   `@md.requires_geofence(scope="auth")` (the on_register shape) and assert:
   - the entry's `geofence` sub-entry survives (`scope == "auth"`), AND
   - `type == "public"` / `requires_auth is False` survive too (both directions
     of the merge), AND
   - the probe's key appears in
     `mojo.apps.account.rest.geofence._enforced_endpoints()`.
   Also assert the real `mojo.apps.account.rest.user.on_register` key appears in
   `_enforced_endpoints()` (import `mojo.apps.account.rest.user` explicitly in
   the test — in-process assertions can't rely on the server's URL loading).
   Fails on current code (geofence sub-entry wiped), passes with the fix.
3. **`tests/test_geofence/config_plane.py:110`** — upgrade `len > 0` to
   membership assertions on the `GET /api/geo/rules` response: keys ending in
   `.on_register` and `.on_user_login` present in `enforced_endpoints`.
4. **`tests/test_geofence/post_auth.py:216-221`** — update the now-stale bug
   comment (bug fixed as DM-044; probes remain as deterministic fixtures).
5. **`CHANGELOG.md`** — entry for the audit-surface fix.

### Design decisions
- **Shared helper vs. ten inline merges**: helper — future decorators can't
  reintroduce the overwrite. Rejected: repeating the get/update/write triplet
  ten times.
- **`entry.update()` last-wins for overlapping scalar keys** (`type`,
  `requires_auth`, `function`): intentionally preserves today's semantics when
  two auth decorators stack (outermost/last-applied wins) while additively
  keeping disjoint keys like `geofence`. No deep-merge machinery — KISS.
- **No change to `geofence.py`**: its merge is already correct; replacing the
  `geofence` sub-dict wholesale on re-decoration is the right behavior (latest
  args win).

### Edge cases & risks
- Overlapping keys between stacked auth decorators → last-wins, identical to
  current behavior; no real code does this anyway.
- `function` key stores whatever `func` that decorator received (inner vs.
  wrapped) — cosmetic, unchanged by the fix.
- Entries with `geofence` but no `type` (geofence-only stacks) already exist
  today; the direct `['type']` index in `test_routes.py` is a pre-existing
  condition the fix only improves.
- Import order: in-process tests must import the endpoint module
  (`mojo.apps.account.rest.user`) before asserting registry membership.
- No behavior change to enforcement/auth/perms — wrappers are untouched; this is
  registry bookkeeping only.

### Tests
- New regression (testit, `@th.django_unit_test()`, in `tests/test_geofence/`):
  stacked probe keeps both `geofence` and `public` info; probe and real
  `on_register` appear in `_enforced_endpoints()`.
- `config_plane.py` membership upgrade (via `GET /api/geo/rules`).
- Baseline first per `.claude/rules/build-baseline.md`, then full default suite
  green after.

### Docs
- `docs/django_developer/account/geofence.md` — optional one-line note that
  `enforced_endpoints` is complete regardless of decorator stacking order.
- `CHANGELOG.md` — behavior-visible fix (audit surface now complete).
- No `docs/web_developer/` changes — response shape unchanged.

### Open questions
None.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
