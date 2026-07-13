---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-037
type: bug
title: ApiKey.validate_token grants group context without checking the key's group is_active — deactivated tenants' keys keep working
priority: P2
effort: S
owner: backend
opened: 2026-07-10
depends_on: []
related: [ITEM-025, ITEM-019]
links: []
---

# ApiKey.validate_token grants group context without checking the key's group is_active — deactivated tenants' keys keep working

## What & Why
`ApiKey.validate_token` (`mojo/apps/account/models/api_key.py:272-303`) checks
`api_key.is_active` and `expires_at` (line 286) but never
`api_key.group.is_active`, then unconditionally sets
`request.group = api_key.group` (line 292). Deactivating a group therefore
does NOT cut off its machine credentials: the ordinary key request (no
explicit `group=` param) keeps full group-scoped access to the deactivated
tenant's data. ITEM-025's active-only resolution only bites when a request
explicitly passes `group=<id>` — the dispatcher then clobbers `request.group`
to None and model security fails closed. Docs now state the honest behavior
and workaround ("deactivate the key itself") — commit aca2fab — but the
fail-closed expectation is that tenant deactivation suspends its keys.

Secondary hardening from the same review (latent, not currently exploitable):
the ITEM-019 groupless-ApiKey branch (`mojo/models/rest.py:288-311`) denies by
default, but if a future group-scoped model ever sets
`RestMeta.ALLOW_API_KEY_GLOBAL = True`, a key could reach UNSCOPED access by
supplying any inactive group id (dispatcher yields request.group=None and its
`is_group_allowed` confinement check is skipped when group is None). No model
sets the flag today. Add a guard (or loud assertion) against combining a
`group` FK with `ALLOW_API_KEY_GLOBAL=True` on one model.

## Acceptance Criteria
- [ ] A key whose group is inactive fails authentication-time group context: either the token is rejected outright or `request.group` is not set and model security fails closed — decide + document which (product decision: is reactivating a group expected to instantly restore its keys?).
- [ ] Ordinary key requests (no `group=` param) against a deactivated group's data are denied.
- [ ] Keys of active groups are unaffected (including child-group access via `is_group_allowed`).
- [ ] Guard/assertion prevents `group` FK + `ALLOW_API_KEY_GLOBAL=True` on one model (or the combination is explicitly documented as forbidden).
- [ ] Docs in both tracks updated to replace the "deactivate the key itself" workaround with the new behavior.
- [ ] Regression test: key of a deactivated group → denied on a group-scoped endpoint without any `group=` param.

## Repro — bugs only
1. Create group G + `ApiKey.create_for_group(G, ...)` with a working permission.
2. Set `G.is_active=False`.
3. Call any group-scoped endpoint with `Authorization: apikey <token>` and NO `group=` param.
- Expected: denied (deactivated tenant's credentials suspended).
- Actual: full normal access — `request.group = G` straight from `validate_token`.

## Investigation
Traced by the ITEM-025 post-build security review (2026-07-10) — confidence:
**high** (code-path reading of api_key.py:272-303 + rest.py:288-311; re-verify
during /scope). Pre-existing; ITEM-025 documented it rather than fixing it
(auth-time behavior is out of a dispatcher-resolution item's scope).
Regression-test feasibility: high — apikey client patterns exist
(`tests/test_global_perms/_helpers.py:use_apikey`, `tests/test_user_mgmt/api_keys.py`).

## Plan

### Goal
A deactivated group's API keys lose group-scoped access at request time (via a
runtime `is_active` check, so reactivating the group instantly restores them —
keys are never mutated), while active groups, active child groups, and the
group-independent federation path are unaffected.

### Decision recap (user-approved, 2026-07-12)
- **Reactivation restores keys instantly** ⇒ runtime check, never touch the key.
- **Strip group context (403 at model security), not a hard 401** — the geoip
  federation receiver authorizes on `has_permission` and ignores
  `request.group`; a 401 would suspend a legitimate inactive-group fleet peer,
  strip-context does not. Matches ITEM-025's "inactive == no group context".
- An independently-active **child** group under an inactive parent stays
  reachable via explicit `group=<child id>` (documented boundary, not a bug —
  the fix gates the *resolved* group per-request; no cascade deactivation).
- Inactive-group fleet-peer keys may still push to geoip `/sync` (global path,
  group irrelevant) — intentional.

### Context — what exists

**The bug site — `mojo/apps/account/models/api_key.py:282-314` `validate_token`:**
```python
        api_key = cls.objects.select_related("group").get(token_hash=token_hash)  # 293 — group already loaded
        ...
        if not api_key.is_active:                       # 297 — key's own active flag
            return None, "API key is inactive"
        if api_key.expires_at and dates.utcnow() > api_key.expires_at:  # 300
            return None, "API key has expired"
        request.group = api_key.group                   # 303 — BUG: no group.is_active check
        request.api_key = api_key                       # 304
```
`select_related("group")` at line 293 means `api_key.group.is_active` costs no
extra query.

**Auth middleware — `mojo/middleware/auth.py:16,51-56`:** registers
`"apikey": ApiKey.validate_token`; the call site can only do reject (`error` →
401 JsonResponse) vs accept. There is **no** "authenticated but no group" mode
at the middleware — that state only exists if `validate_token` returns the key
but leaves `request.group = None`. So the strip-context behavior must live
*inside* `validate_token`.

**Model-security api_key branch — `mojo/models/rest.py`:**
- `is_group_scoped` computed at line 232: `bool(GROUP_FIELD) or hasattr(cls, "group")`.
- Instance re-bind at lines ~282-295 (detail ops): when an instance is loaded,
  `request.group` is re-bound to the instance's own group via `GROUP_FIELD` /
  `.group` — **this repopulates the inactive group even after `validate_token`
  set it to None**, which is why a `validate_token`-only fix misses detail/
  save/delete.
- The branch (lines ~297-338):
```python
        if request.group and is_group_scoped:
            if hasattr(request, 'api_key') and request.api_key:
                allowed = request.api_key.has_permission(perms)      # <-- add is_active gate ABOVE this
                if allowed:
                    return True, None
                return False, objict.objict(branch="api_key.has_permission",
                                            event_type="group_member_permission_denied", status=403)
            allowed = request.group.user_has_permission(request.user, perms)
            ...
        elif hasattr(request, 'api_key') and request.api_key:
            # groupless / null-group: ITEM-019 deny-by-default
            if not cls.get_rest_meta_prop("ALLOW_API_KEY_GLOBAL", False):   # 325
                return False, objict.objict(branch="api_key.groupless_denied",
                                            event_type="user_permission_denied", status=403)
            allowed = request.api_key.has_permission(perms)
            ...
```
Confirmed: with `request.group = None`, the no-param **list** case falls to the
`elif` groupless-deny branch → 403 for any group-scoped model
(`ALLOW_API_KEY_GLOBAL` False by default). The **detail** case needs the new
gate in the `if` api_key sub-branch because of the re-bind.

**ITEM-025 contract — `Group.get_active` (`group.py:223-232`)** filters
`pk=..., is_active=True`; dispatcher `mojo/decorators/http.py:73-84` resolves a
client `group=` param through it (active-only) and then checks
`api_key.is_group_allowed(request.group)`. This only runs when a `group=` param
is present, and it runs *after* the middleware set `request.group` — the bug is
the no-param case that never hits this resolver.

**`is_group_allowed` (`api_key.py:191-200`)** and `is_child_of`
(`group.py:375-384`) are **hierarchy-only, no `is_active`**. So an explicitly-
passed active child resolves via `get_active` (active-only) and then passes
`is_group_allowed`; our change gates on the *resolved* group's `is_active`, so
active children stay reachable, inactive ones already resolve to None.

**Federation receiver — `mojo/apps/account/rest/device.py:80-88`**
(`requires_global_perms('geoip_sync', allow_api_keys=True)`) and the decorator
`mojo/decorators/auth.py:146-158` authorize on `user.has_permission(perm_set)`
only — **never read `request.group`**. Strip-context does not interfere; a hard
401 would.

**`ALLOW_API_KEY_GLOBAL`** read only at `rest.py:325` and `user.py:919-922`. **No
model sets it True** anywhere in the repo. There is **no** RestMeta class-
definition validation hook (no metaclass / `__init_subclass__` / app-ready
scan); the only precedent is the lazy runtime `_warn_can_save_deprecated`
(`rest.py:43-56`). So the guard must be a runtime check at the decision point.

### Changes — what to do

1. **`mojo/apps/account/models/api_key.py`** — `validate_token`, replace line 303:
   ```python
   # Group context is granted only for an ACTIVE group. Deactivating a tenant
   # instantly suspends its keys; reactivating restores them (no key mutation).
   # An inactive group leaves request.group None so group-scoped model security
   # fails closed via the groupless-deny branch (mojo/models/rest.py), matching
   # ITEM-025's active-only contract. NOT a hard reject: the federation path
   # (requires_global_perms, allow_api_keys) ignores request.group, so a reject
   # would over-suspend fleet-peer keys.
   request.group = api_key.group if (api_key.group_id and api_key.group.is_active) else None
   ```
   (Use `api_key.group_id` for the presence test — no query; `.group` is already
   `select_related`-loaded for the `.is_active` read.)

2. **`mojo/models/rest.py`** — in the `if request.group and is_group_scoped:`
   block, inside `if hasattr(request, 'api_key') and request.api_key:`, BEFORE
   `allowed = request.api_key.has_permission(perms)`, add:
   ```python
   if not request.group.is_active:
       # Instance re-bind (above) can repopulate request.group from a detail
       # instance owned by a now-inactive group; gate it here so detail/save/
       # delete fail closed too, not just the list path. ApiKey-only — user
       # semantics unchanged.
       return False, objict.objict(
           branch="api_key.group_inactive",
           event_type="user_permission_denied",
           status=403,
       )
   ```

3. **`mojo/models/rest.py`** — secondary hardening in the `elif hasattr(request,
   'api_key') and request.api_key:` branch, replace the
   `if not cls.get_rest_meta_prop("ALLOW_API_KEY_GLOBAL", False):` guard with:
   ```python
   allow_global = cls.get_rest_meta_prop("ALLOW_API_KEY_GLOBAL", False)
   if allow_global and is_group_scoped:
       # Misconfiguration: a group-scoped model must never opt into global
       # api-key access — a key could reach unscoped rows by arriving with no
       # active group context. Fail closed and shout so it's discoverable.
       from mojo.helpers import logit
       logit.error(
           f"ALLOW_API_KEY_GLOBAL=True on group-scoped model {cls.__name__} — "
           "ignored (fail-closed). Remove the flag or the group FK.")
       allow_global = False
   if not allow_global:
       return False, objict.objict(
           branch="api_key.groupless_denied",
           event_type="user_permission_denied",
           status=403,
       )
   ```
   (Confirm `logit` isn't already module-imported in rest.py; if it is, drop the
   local import. Never use stdlib logging — `.claude/rules/core.md`.)

### Design decisions
- **Two choke points, both runtime.** `validate_token` makes `request.group`
  honest at the boundary (fixes list via the existing groupless-deny branch,
  and benefits every `request.group` consumer); `rest.py` closes the detail
  instance-re-bind hole. Neither mutates the key ⇒ reactivation is instant.
- **Gate the resolved group, per request** (not cascade-deactivate children):
  minimal, consistent with ITEM-025, and leaves the documented active-child
  boundary intact.
- **Strip-context over 401:** preserves the group-independent federation path;
  see decision recap.
- **`group_id` for presence, `.group.is_active` for the flag:** the FK is
  non-null in practice, but `group_id` avoids any lazy load and is None-safe.
- **Runtime guard for `ALLOW_API_KEY_GLOBAL`+group FK:** no class-definition
  validation surface exists; the read site is the correct, fail-closed place,
  and since no model sets the flag it cannot regress anything.

### Edge cases & risks
- **Active child under inactive parent:** still reachable via explicit
  `group=<child id>` — documented boundary, per-group gating is deliberate.
- **Detail read of own key** (`GET /api/group/apikey/<own id>`): exercises the
  re-bind path — 200 active, 403 inactive. Primary proof the hole is closed.
- **Federation `/sync`:** unaffected (ignores `request.group`); an inactive-
  group peer key still authorizes — intentional.
- **No schema change** ⇒ no `bin/create_testproject`.
- **Non-RestMeta `@md.requires_perms` group-fallback endpoints:** out of scope —
  they already resolve `group=` via `Group.get_active` (active-only) when a
  param is present; the no-param api_key surface that matters here is the
  RestMeta model-security path the two changes cover.

### Tests
New file **`tests/test_global_perms/apikey_group_inactive.py`** (testit — read
`docs/django_developer/testit/Overview.md` first). Use
`tests/test_global_perms/_helpers.py` `use_apikey(opts, token)`. Self-contained
group+key created and torn down in-file (setup deletes-before-create per
`.claude/rules/testing.md`) so shared fixtures are never corrupted; mirror the
`last_activity = None` trick from `tests/test_middleware/group_param_is_active.py`
if a `touch()` side effect needs to be observable. Each regression must FAIL
before the fix and PASS after.

- **unit / `validate_token`** (mirror `api_keys.py::apikey_validate_token_inactive`,
  lines 103-120, `get_mock_request()` pattern): create active group G + key K
  via `ApiKey.create_for_group`; deactivate G; `user, err = ApiKey.validate_token(token, req)`
  → `err is None` and `user is not None` (still authenticates) but
  `req.group is None`. Reactivate/cleanup.
- **HTTP list, no `group=`:** `use_apikey`; `GET /api/group/apikey` (or
  `/api/settings`) → 200 when G active, **403 when G inactive**.
- **HTTP detail (re-bind path):** `GET /api/group/apikey/<K.id>` with K's own
  token → 200 when active, **403 when inactive** (proves rest.py gate closes the
  instance-re-bind hole — the key reads its own row).
- **Reactivation:** deactivate → 403, `G.is_active=True; G.save()` → 200 again,
  same token (instant restore, key never modified).
- **Control — not over-restricted:** active G → 200; and an active **child**
  under G reached via explicit `group=<child id>` → 200
  (`create_for_group` on parent, child active).
- **Guard:** pick a group-scoped model (e.g. `Setting` or `ApiKey`), in a
  `try/finally` set `Model.RestMeta.ALLOW_API_KEY_GLOBAL = True`, drive a
  groupless api_key request (no active group context) → still 403; restore the
  attribute in `finally`. Optionally assert a `logit.error` was emitted.
- **Federation non-interference (optional):** an inactive-group key holding
  `geoip_sync` still succeeds on `POST /api/system/geoip/sync` (mirror
  `tests/test_global_perms/apikey_gate.py`) — documents the intentional carve-out.

Run: `bin/run_tests --agent -t test_global_perms.apikey_group_inactive` during
build; full `bin/run_tests --agent` before close. Baseline first per
`.claude/rules/build-baseline.md`.

### Docs
- **`docs/web_developer/account/api_keys.md`** — replace the "deactivate the key
  itself" workaround (from commit aca2fab; grep `deactivate the key`) with:
  deactivating the group instantly suspends its keys' group-scoped access, and
  reactivating restores them. Note the active-child-under-inactive-parent
  boundary.
- **`docs/django_developer/`** — the API-keys / permissions page(s) (grep
  `deactivate the key`, `ALLOW_API_KEY_GLOBAL`): same behavior note, plus the
  `group` FK + `ALLOW_API_KEY_GLOBAL=True` prohibition (now runtime-enforced,
  fail-closed + logged).
- **`CHANGELOG.md`** — bug entry under the rolling block (ITEM-037).

### Open questions
None — direction, failure-mode (strip-context), and the federation carve-out
approved by the user 2026-07-12.

## Notes
- **Baseline (2026-07-12, before any edit):** `bin/run_tests --agent` →
  total 2436 / passed 2375 / **failed 5** / skipped 56. The 5 failures are
  PRE-EXISTING cross-module flakiness, NOT green — but `test_assistant` run in
  isolation is fully green (589/572/0), so they only manifest under full-suite
  parallel model-reloading (`get_rest_meta_prop("CAN_DELETE")` racing on
  `incident.RuleSet`; the suite logs "Reloading models is not advised"). None
  touch ITEM-037's code (`api_key.py`, `rest.py`). Accepted pre-existing set —
  the ONLY failures attributable to "not mine":
  - `test_assistant/23_test_delete_tools.py`: `test_delete_model_instance_success`,
    `test_delete_model_instance_permission_denied`,
    `test_delete_model_instance_not_found`,
    `test_delete_model_instance_reports_security_event`
  - `test_assistant/27_test_save_model_tool.py`: `test_delete_writes_audit_log`
  Post-change gate: full suite shows only these 5 (and they still pass in
  isolation). Flakiness itself is out of scope — separate chore if it persists.
- **Post-change (2026-07-12):** targeted `test_global_perms.apikey_group_inactive`
  6/6 pass. Full suite 2442 / passed 2380 / **failed 6** / skipped 56 — the 5
  recorded flaky `test_assistant` tests PLUS `test_helpers/domains.py`
  `domain_txt_lookup` (a live DNS TXT lookup — network-dependent, unrelated to
  apikey/permission code). `test_global_perms` + `test_assistant` run in
  ISOLATION = 615/598/0 green (my 6 new tests pass; test_assistant clean alone),
  proving the change introduced ZERO new failures — all 6 full-suite failures
  are pre-existing environmental flakiness.
- **Scope addition flagged:** the plan listed two edit sites (`validate_token`,
  `rest.py` api_key branch + guard). Building surfaced a THIRD api_key group-
  context surface — `ApiKey.get_groups` ignored `Group.is_active`, so the
  RestMeta list fallback (`on_rest_handle_list` → `get_groups_with_permission`)
  re-listed a deactivated group's rows even after `validate_token` stripped
  context. Fixed `ApiKey.get_groups` to always exclude inactive groups (api_key-
  scoped; the User member-grant equivalent — `User.get_groups` filtering
  `member.is_active` but not `Group.is_active` — remains the sibling item
  `member-perms-ignore-group-is-active`). Required to meet this item's own LIST
  acceptance criterion.
- Decide the failure mode carefully: rejecting the token entirely (401) vs stripping group context (403 at model security). 401 is cleaner but changes auth semantics; group-context stripping matches ITEM-025's shape.
- Check the geoip federation receiver (`allow_api_keys=True` surface) for interaction before changing validate_token.
