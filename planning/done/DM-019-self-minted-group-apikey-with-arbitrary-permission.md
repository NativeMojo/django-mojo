---
# id is assigned by /scope on pickup — leave it blank
id: DM-019
type: bug
title: Self-minted group ApiKey with arbitrary permissions reaches groupless models cross-tenant
priority: P1
effort: M
owner: backend
opened: 2026-07-08
depends_on: []
related: [DM-018, DM-017]
links: []
---

# Self-minted group ApiKey with arbitrary permissions reaches groupless models cross-tenant

## What & Why

Surfaced by DM-018's post-build security review (2026-07-08) as a WARNING +
the root cause of a CRITICAL. DM-018 closed the CRITICAL for the six
`requires_perms` AWS/device endpoints it covered (switched to
`requires_global_perms`), but the **underlying mechanism is broader** and
distinct from the `requires_perms` group-fallback DM-018 audited. It affects
the `@md.uses_model_security(...)` / `on_rest_request` path, so it is filed
separately rather than expanding DM-018 further.

Two independent gaps combine:

1. **`ApiKey.permissions` is unrestricted at assignment.**
   `mojo/apps/account/models/api_key.py:36` is a bare `JSONField` with only a
   `has_permission` getter — there is **no `set_permissions` / `can_change_permission`
   gate** analogous to the one `GroupMember` has (`member.py:84-104`, hardened
   by DM-018). A group admin who can create a group ApiKey via
   `POST /api/group/apikey` (`account/rest/api_key.py`, gated by group-scoped
   `manage_group`/etc. — legitimate, since `ApiKey` has a `group` FK) can set
   **any** permission key on their own key, e.g. `{"manage_users": true,
   "manage_aws": true, "security": true}`. Nothing bounds which keys they may
   self-assign.

2. **The model-security layer trusts a key's self-claimed perms for groupless
   models.** `MojoModel._evaluate_permission` (`mojo/models/rest.py:270-296`):
   the group branch is `if request.group and hasattr(cls, "group")`; for a
   model with **no `group` FK** that is False, so control reaches the
   `elif hasattr(request, 'api_key') and request.api_key:` branch (`:288-289`)
   which returns `request.api_key.has_permission(perms)` — the key's own dict,
   **with no tenant scoping** (there is no group to scope by). `on_rest_list`
   (`:644-653`) only group-filters when the model has a group field, so the
   list is unfiltered across all tenants.

Net: a low-trust group admin self-mints a key with a category/manage perm and
reads (and for some, writes) **every tenant's** rows of any groupless
`uses_model_security` model. Confirmed-reachable examples still open after
DM-018 (these use `uses_model_security` directly, NOT `requires_perms`, so
DM-018 did not touch them):

- **`User`** (`account/rest/user.py`, `uses_model_security(User)`; User has no
  group FK) with a self-minted `users`/`manage_users` key → LIST/GET/UPDATE all
  users platform-wide (cross-tenant PII, and potentially permission edits).
- **`GeoLocatedIP`** (`account/rest/device.py:32`,
  `uses_model_security(GeoLocatedIP)`) with `security`/`manage_users` → global
  threat/geo data + firewall state surface.
- Audit **every** `uses_model_security` model for a missing `group` FK; each is
  a candidate (e.g. incident/metrics/logit/fileman globals).

DM-018 already closed the six AWS/device *function* endpoints
(`aws/rest/{email,templates,messages}.py`, `account/rest/device.py` location)
because those were on `requires_perms` — the switch rejects the key at the
outer gate before `on_rest_request` runs. This item is the **model-layer**
fix so groupless `uses_model_security` models are covered too.

## Acceptance Criteria

- [ ] A group-scoped ApiKey's self-claimed permission does **not** authorize
      access to a groupless `uses_model_security` model (User, GeoLocatedIP,
      …). Decide the mechanism:
      (a) gate `ApiKey.permissions` assignment with an
      `APIKEY_PERMS_PROTECTION` map (parallel to `MEMBER_PERMS_PROTECTION`),
      and/or (b) make the `rest.py:288` api_key branch fail closed for
      groupless models unless the key is explicitly allowed there. Prefer a
      solution that keeps legitimate group-scoped ApiKey CRUD working.
- [ ] Enumerate every `uses_model_security` model lacking a `group` FK and
      confirm each is either intentionally global-admin-only (and now
      key-safe) or genuinely group-scoped.
- [ ] `ApiKey.has_permission` continuing to grant `all`/`authenticated` is
      reviewed (should a key ever satisfy a broad category perm globally?).
- [ ] Regression tests: a group ApiKey with self-minted `manage_users` gets
      403 on `/api/user`; with `security` gets 403 on `/api/system/geoip`;
      legitimate group-scoped ApiKey flows (webhook subscription CRUD,
      apikey/me, geoip/sync via `allow_api_keys`, phonehub sms/send) stay
      green.
- [ ] Minor (same review): `assistant/services/memory.py:146,163`
      `user.is_superuser` raises `AttributeError` (500) if an ApiKey reaches
      it — make it a clean deny.
- [ ] Docs: `docs/django_developer/account/api_keys.md` (the "indistinguishable
      from a normal group-scoped request" claim needs the groupless-model
      caveat / new protection), permissions.md.

## Plan

### Goal
Make a group-scoped `ApiKey` fail **closed** at the model-security layer for
any request that isn't confined to a group (groupless models, null-group
instances) — closing the proven cross-tenant read/write — and add a
permission-assignment gate so a group admin can't self-mint a key with
arbitrary powerful permissions in the first place.

### Context — what exists (recon verified 2026-07-08, v1.2.42)

**The proven exploit** (end-to-end, with a control): a group-A `ApiKey` whose
`permissions` a group admin set to `{"manage_users": true}` →
`GET /api/user` returned **200 with another tenant's real email/username**; an
otherwise-identical key WITHOUT `manage_users` → **403**; targeted
`GET /api/user?email=<victim>` → returns the victim. Two gaps combine:

**Gap 1 — `ApiKey.permissions` has no assignment gate.**
`mojo/apps/account/models/api_key.py:36` — `permissions = models.JSONField(default=dict, blank=True)`.
There is **no** `set_permissions` / `can_change_permission` / `on_rest_pre_save`
on ApiKey (confirmed absent) — unlike `GroupMember` (`member.py:86-106`,
hardened by DM-018). The create/edit endpoint is
`account/rest/api_key.py:6-10` (`on_group_apikey`,
`@md.uses_model_security(ApiKey)`; ApiKey **has** a `group` FK so SAVE_PERMS
`["manage_group","manage_groups","groups"]` are satisfied by a group admin via
the group branch). The `permissions` REST field has no `set_<field>` method, so
`on_rest_save_field` (`mojo/models/rest.py:1268-1288`) routes it to
`on_rest_update_jsonfield` (`:1421`) which merges the dict verbatim — any
non-`sys.` key lands.

`ApiKey.has_permission` (`api_key.py:103-122`) — the only guard is the `sys.`
prefix:
```python
def has_permission(self, perm_key):
    if isinstance(perm_key, (list, set)):
        return any(self.has_permission(p) for p in perm_key)
    if isinstance(perm_key, str) and perm_key.startswith("sys."):
        return False
    if perm_key in ["all", "authenticated", "member"]:
        return True
    return bool(self._get_permissions_dict().get(perm_key, False))
```
Real admin perms (`manage_users`, `security`, `manage_aws`, `jobs`, …) are not
`sys.`-prefixed, so they pass. `ApiKey.group` is a **required non-null FK**
(`api_key.py:29`), so for a key `request.group` is always truthy.

**Gap 2 — the vulnerable choke point.** `MojoModel._evaluate_permission`
(`mojo/models/rest.py:221-310`). The two ApiKey branches, verbatim:
```python
        if request.group and hasattr(cls, "group"):          # :270  BRANCH A (group-scoped model)
            if hasattr(request, 'api_key') and request.api_key:
                allowed = request.api_key.has_permission(perms)     # confined: on_rest_list group-filters
                if allowed: return True, None
                return False, objict.objict(branch="api_key.has_permission",
                    event_type="group_member_permission_denied", status=403)
            allowed = request.group.user_has_permission(request.user, perms)
            ...
        elif hasattr(request, 'api_key') and request.api_key:  # :288  BRANCH B — VULNERABLE
            allowed = request.api_key.has_permission(perms)     # NO scoping, NO downstream filter
            if allowed: return True, None
            return False, objict.objict(branch="api_key.has_permission",
                event_type="user_permission_denied", status=403)
        if request.user is None or not request.user.is_authenticated:  # :297  (non-key users)
            return False, ...
        allowed = request.user.has_permission(perms)           # :303  real User global check
```
For a key, `request.group` is always truthy, so **Branch B is reached exactly
when `hasattr(cls, "group")` is False (a groupless model) OR the instance's
`.group` is None** (a null-group instance of a group-FK model — `:267-268`
rebinds `request.group = instance.group`; e.g. Conversation/Skill are
`SET_NULL`). Branch B grants purely on the key's self-claimed perms. Downstream,
`on_rest_list` (`:644-653`) only filters `queryset.filter(group=request.group)`
when `GROUP_FIELD or hasattr(cls, "group")` — groupless models get **no
filter** → all tenants' rows. `on_rest_handle_list` (`:458`) returns the full
list when `rest_check_permission` passes; otherwise falls to owner-fallback
(`:465`, needs `is_request_user` — a key lacks it) / group-fallback (`:474`,
needs `hasattr(cls,"group")`) then 403 (`MOJO_REST_LIST_PERM_DENY`, default
True, `:20`,`:495`) or empty (`:504`). Branch B also gates CREATE/UPDATE/DELETE
(`:548/:592/:443` → `rest_check_permission_or_raise`), so it is a read AND write
hole.

**Identity idiom:** `request.user` **is** the ApiKey for apikey auth
(`middleware/auth.py:19` `AUTH_BEARER_NAME_MAP={"apikey":"user"}`, `:54-55`);
`request.api_key` is set in `ApiKey.validate_token` (`api_key.py:236-237`).
`User.is_request_user` exists (`user.py:296`); **ApiKey has none** — the
canonical "real request User?" test is `hasattr(x, "is_request_user")` (used at
`rest.py:264/465`, `decorators/auth.py:138`).

**Blast radius — GROUPLESS `uses_model_security` models (Branch B, exposed):**
`User` (`account/rest/user.py:20`, bare `@md.URL`, no `check_view_permission`
→ the proven case), `GeoLocatedIP` (`device.py:34`), `UserAPIKey`
(`user_api_key.py:9`), `UserLoginEvent` (`login_event.py:10`), `Group`
(`group.py:8,14` — overrides check_view_permission but still grants via
`request.user.has_permission`), `Job`/`JobEvent`/`JobLog` (`jobs/rest/jobs.py:16,25,34`),
`ScheduledTask`/`TaskResult` (`scheduled_task.py:7,14`),
`BouncerDevice`/`BouncerSignal`/`BotSignature` (`bouncer_admin.py:11,19,27`,
`uses_model_security(None)`), `FileRendition` (`fileman.py:19`, indirect
`GROUP_FIELD`). Read-only (`SAVE_PERMS=[]`) rows are still readable.
**GROUP-SCOPED (Branch A, confined — leave working):** Conversation, Skill,
ChatRoom, GitHubInstall, WebhookSubscription, ApiKey, Setting, GroupMember,
PublicMessage, FileManager, File.

**The GroupMember protection precedent to mirror (Gap 1 fix), `member.py:86-106`:**
```python
def _member_perms_protection():
    return settings.get("MEMBER_PERMS_PROTECTION", {}, kind="dict") or {}
def can_change_permission(self, perm, value, request):
    if request.user.has_permission(["manage_groups", "manage_users"]):
        return True
    req_member = self.group.get_member_for_user(request.user, check_parents=True)
    if req_member is not None:
        m = _member_perms_protection()
        if perm in m: return req_member.has_permission(m[perm])
        return req_member.has_permission(["manage_group","manage_members","manage_users","manage_groups"])
    return False
def set_permissions(self, value):
    for perm, perm_value in value.items():
        if not self.can_change_permission(perm, perm_value, self.active_request):
            raise merrors.PermissionDeniedException()
        ...add/remove
```
`on_rest_save_field` (`rest.py:1270-1279`) routes a `permissions` REST write
through a `set_permissions` method when one exists. `active_request` resolves
via the `ACTIVE_REQUEST` ContextVar (`middleware/mojo.py:62`).

**Legit key flows that MUST stay green (recon-verified):** geoip/sync
(`device.py:87`, dedicated `requires_global_perms(...,allow_api_keys=True)` —
not model-security), geoip/lookup (`device.py:42`, `requires_auth()` calling
`on_rest_get` **directly** — bypasses `_evaluate_permission`), phonehub
sms/send (group-scoped), webhook_subscription CRUD by a `manage_group` key
(Branch A, group-scoped — `tests/test_account/test_webhook_subscription_rest.py`),
group/apikey/me. **None rely on Branch B for a groupless model**, so the
Branch B fix breaks nothing.

**Minor (from acceptance criteria):** `assistant/services/memory.py:145,161`
`_can_read_tier`/`_can_write_tier` start with `if user.is_superuser:` — an
ApiKey has no `is_superuser` → `AttributeError`/500 if a key reaches it.

### Changes — what to do

**1. `mojo/models/rest.py` — the core fail-closed fix (Gap 2).** In
`_evaluate_permission`, Branch B (`:288-296`), an ApiKey must not authorize an
unconfined request. Replace the unconditional grant with a default-deny gated
by a RestMeta opt-in:
```python
elif hasattr(request, 'api_key') and request.api_key:
    # Reaching this branch means the request is NOT confined to a group
    # (groupless model, or a null-group instance). A group-scoped ApiKey must
    # not get unconfined/cross-tenant access — deny unless the model explicitly
    # opts in (parallel to requires_global_perms' allow_api_keys). Group-scoped
    # models take Branch A above and are unaffected.
    if not cls.get_rest_meta_prop("ALLOW_API_KEY_GLOBAL", False):
        return False, objict.objict(
            branch="api_key.groupless_denied",
            event_type="user_permission_denied",
            status=403,
        )
    allowed = request.api_key.has_permission(perms)
    if allowed:
        return True, None
    return False, objict.objict(
        branch="api_key.has_permission",
        event_type="user_permission_denied",
        status=403,
    )
```
Only Branch B (key identities on unconfined requests) changes; Branch A
(group-scoped) and the real-User fall-through (`:303`) are untouched. No model
sets `ALLOW_API_KEY_GLOBAL` initially (the federation pattern is dedicated
`requires_auth`/`allow_api_keys` endpoints, not model-security) — the flag is a
documented escape hatch only.

**2. `mojo/apps/account/models/api_key.py` — assignment gate (Gap 1).** Mirror
the GroupMember pattern:
- add module helper `_apikey_perms_protection()` →
  `settings.get("APIKEY_PERMS_PROTECTION", {}, kind="dict") or {}`.
- add `can_change_permission(self, perm, value, request)`: global
  `manage_groups`/`manage_users` bypass; else resolve the requester's
  membership in `self.group` (`self.group.get_member_for_user(request.user,
  check_parents=True)`); if the perm is in the protection map, require the
  granter to hold the mapped perm; else require the granter to hold a
  key-management perm (`["manage_group","manage_groups","groups"]`, matching the
  endpoint's SAVE_PERMS). Deny if no membership. (Guard `request`/`request.user`
  None → deny.)
- add `set_permissions(self, value)`: dict only; per key,
  `can_change_permission(perm, val, self.active_request)` or raise
  `merrors.PermissionDeniedException`; then set/remove in `self.permissions`.
  Because it's a `set_<field>` method, REST writes to `permissions` route
  through it automatically (`rest.py:1270`).
- **Important:** `set_permissions` must remain a no-op-safe path for the
  **model constructor** `create_for_group(permissions=...)` (`api_key.py:191`),
  which assigns `permissions` directly (not via the setter) — that stays a
  trusted internal call and is unaffected. Only REST writes route through
  `set_permissions`.
- Default `APIKEY_PERMS_PROTECTION = {}` → no behavior change out of the box
  (operators opt in), exactly like `MEMBER_PERMS_PROTECTION`.

**3. `mojo/apps/assistant/services/memory.py:145,161`** — replace
`if user.is_superuser:` with `if getattr(user, "is_superuser", False):` (clean
deny instead of 500 when the identity is an ApiKey).

**4. Tests — new `tests/test_global_perms/apikey_groupless.py`** (extends the
DM-018 module/style; see Tests).

**5. Docs + CHANGELOG** (see Docs). No schema change → no
`bin/create_testproject`.

### Design decisions
- **Fix at the choke point, fail-closed, no config (primary).** Denying keys in
  Branch B closes the proven exposure for all 14 groupless models at once,
  by default, with zero configuration — unlike a protection-map (Gap 1) which
  protects nothing until configured. This is the security-critical change.
- **Deny unconfined ApiKey access rather than "deny groupless models."** Branch
  B is reached for a key both on a groupless model AND on a null-group instance
  of a group-FK model (Conversation/Skill `SET_NULL`). Gating Branch B itself
  (not `hasattr(cls,"group")`) closes both — a key never gets unconfined access,
  full stop.
- **RestMeta `ALLOW_API_KEY_GLOBAL` opt-in (default False), mirroring the
  decorator's `allow_api_keys`.** Keeps a documented, per-model escape hatch
  without leaving anything open. No model uses it initially. Rejected:
  unconditional deny with no flag (marginally simpler, but no escape hatch and
  less symmetric with the decorator).
- **Also gate ApiKey permission assignment (Gap 1), mirroring GroupMember.**
  Defense-in-depth + consistency: stops a group admin minting a key with
  `manage_users` etc. at the source, and protects any future
  `ALLOW_API_KEY_GLOBAL` model. Default-empty map = no out-of-box behavior
  change (the fail-closed Fix 1 is what actually protects by default). Flagged
  for sign-off — could be split to its own item; recommend keeping (small,
  symmetric, closes the item's titular "arbitrary permissions" root).
- **ApiKeys are group-scoped credentials; platform-global admin uses a real
  user.** After this, a key can no longer reach a global model via REST — the
  supported path for machine access to global data is a dedicated endpoint
  (`allow_api_keys`, like geoip/sync) or a service-account User. Called out in
  CHANGELOG.

### Edge cases & risks
- **Breaking a legit key flow**: recon found none relying on Branch B for a
  groupless model (geoip/lookup bypasses the choke point; federation uses
  dedicated endpoints; all other key flows are group-scoped Branch A). Full
  suite + the named apikey tests must stay green — the regression check.
- **Null-group instances** (Conversation/Skill `SET_NULL`): a key hitting one
  now denies (correct — it's unconfined). No legit flow does this.
- **`create_for_group` / internal assignment** must not route through
  `set_permissions` (it sets `.permissions` directly on the constructed
  instance) — verify it still works (tests in `test_user_mgmt/api_keys.py`).
- **`can_change_permission` for a key created by a global admin**: global
  `manage_users`/`manage_groups` holder bypasses the map (can still mint any
  key) — intended; the map constrains group-scoped admins.
- **A deployment that today uses a group key for global admin** (e.g. user
  management) will break — documented as a deployment note; remediate with a
  service-account user or `ALLOW_API_KEY_GLOBAL` on that model (with the
  understanding it re-opens that model to all keys).
- **`request.group` falsy for a key** cannot happen (`ApiKey.group` non-null),
  so Branch B ⟺ unconfined; no false denials for group-scoped key use.

### Tests (testit — new `tests/test_global_perms/apikey_groupless.py`)
Use `ApiKey.create_for_group(group=…, permissions={…})` + the `use_apikey`
helper (`tests/test_global_perms/_helpers.py:71`).
- **Core regression** `test_apikey_denied_on_groupless_models`: a group-A key
  with a broad perm set (`users, manage_users, security, manage_security,
  view_security, jobs, manage_jobs, view_jobs, files, manage_files,
  view_fileman, view_scheduled_tasks, manage_scheduled_tasks`). Assert **403**
  (skip on 404) on list + a detail for each groupless endpoint: `/api/user`,
  `/api/user/<pk>`, `/api/system/geoip`, `/api/account/login_events` (confirm
  path), `/api/group`, `/api/jobs/job` (confirm path), `/api/jobs/job_event`,
  `/api/jobs/scheduled_task`, `/api/account/bouncer/device`,
  `/api/account/bouncer/bot_signature`, `/api/user/apikey` (UserAPIKey),
  `/api/fileman/rendition`. Include a **victim-in-body** check on `/api/user`
  (a uniquely-named user must NOT appear) so it proves data non-exposure, not
  just a status code.
- **Legit Branch A stays green** `test_apikey_group_scoped_still_works`: a
  group-A key with `manage_group` → `GET /api/group/webhook_subscriptions`
  (its own group) returns 200 (and only its group's rows). (The existing
  `test_webhook_subscription_rest.py` also covers this — must stay green.)
- **Federation unaffected**: `test_geoip_sync_endpoint.py` (allow_api_keys) and
  geoip/lookup stay green (full-suite check).
- **Assignment gate** `test_apikey_perms_protection`: with
  `Setting.set("APIKEY_PERMS_PROTECTION", {"itest_prot": "sys.itest_never"})`
  (test-only key; clean up), a group admin (member `manage_group`, no global)
  creating/editing a key with `{"itest_prot": true}` → denied; an unlisted perm
  still lands; assert the DB-backed map is honored via `kind="dict"` (a 403,
  not a 500). Mirror `invite_protection.py`.
- **memory crash**: a key reaching `_can_read_tier`/`_can_write_tier` returns a
  clean deny, not 500 (unit-level or via the assistant memory endpoint with an
  apikey identity).
- Keep the DM-018 `tests/test_global_perms/apikey_gate.py` green (it already
  asserts keys are denied on the switched `requires_perms` + six delegating
  endpoints).

### Docs
- `docs/django_developer/account/api_keys.md` — correct the claim at `:20`
  ("indistinguishable from a normal group-scoped request"): a key is confined to
  **group-scoped** models; it is **denied** on groupless/platform-global models
  via model security (unless `RestMeta.ALLOW_API_KEY_GLOBAL`), and its
  `permissions` assignment is gated by `APIKEY_PERMS_PROTECTION`. Document both.
- `docs/django_developer/core/permissions.md` — extend the "Global vs
  Group-Scoped Permission Checks" section: model-security also refuses ApiKeys
  on groupless models (not just the `requires_global_perms` decorator).
- `docs/django_developer/helpers/settings_reference.md` — add
  `APIKEY_PERMS_PROTECTION` (dict, default `{}`, `kind="dict"`), next to
  `MEMBER_PERMS_PROTECTION`.
- `CHANGELOG.md` — security block: keys can no longer reach platform-global
  models via REST; the `ALLOW_API_KEY_GLOBAL` opt-in; `APIKEY_PERMS_PROTECTION`;
  deployment note (use a service-account User for global machine access).

### Open questions
None blocking. One decision flagged for sign-off: **include the ApiKey
permission-assignment gate (Gap 1 / change 2) in this item, or split it to a
follow-up?** Recommendation: include it — it's small, symmetric with
GroupMember, and closes the item's titular root cause; the fail-closed choke
point (change 1) is what protects by default regardless.

## Notes

- **Baseline (2026-07-08, `bin/run_tests --agent`)**: passed — 2323 total, 2267
  passed, 0 failed, 56 skipped. Green.
- **VERIFIED end-to-end with a control (2026-07-08).** Two group-A `ApiKey`s,
  identical except for permissions: key WITH `{"manage_users": true}` vs control
  WITH only `{"view_data": true}`. Against `/api/user`
  (`uses_model_security(User)`, groupless, NOT touched by DM-018), through the
  real middleware → dispatcher → `on_rest_request` stack:
  - WITH `manage_users`: **HTTP 200**, response body contained a victim user's
    real `email`/`username` (a user with no relationship to the key's group).
  - Control (no `manage_users`): **HTTP 403**.
  - Targeted `GET /api/user?email=<victim>`: WITH → returns the victim; control
    → 403.
  So it is real cross-tenant data exposure, gated precisely by the key's
  self-claimed permission — not an empty-list-on-no-perm artifact (the
  no-perm control is denied outright). An attacker can also target any specific
  account via the `?email=`/field filters, independent of pagination. This is
  the reproduction to convert into a regression (assert 403 after the fix — do
  NOT keep a bug-confirmation test).
  - Note: this testproject denies the no-perm list with 403
    (`MOJO_REST_LIST_PERM_DENY` path, `mojo/models/rest.py:495-503`); a
    deployment with that setting off returns an empty 200 there instead
    (`rest.py:504` `.none()`) — but that changes only the *no-permission* case,
    not the WITH-permission exposure proven above.
- **Why the "ApiKey is group-scoped" design doesn't catch this:** the model's
  only system-permission guard is the `sys.` prefix (`api_key.py:118-119`) —
  real admin perms (`manage_users`, `manage_aws`, `security`) are not
  `sys.`-prefixed, so a key may hold them; and group-scoping (`is_group_allowed`
  + instance-group rebind) only bites for models WITH a `group` FK — a groupless
  model has no group to scope to, so `rest.py:288` trusts the key's plain perm
  globally. Both intended safeguards have the same blind spot: global data +
  non-`sys.` admin permission names.
- Origin: DM-018 post-build security review (2026-07-08). That review rated
  the **six covered endpoints** CRITICAL (now fixed in DM-018 commit) and the
  **underlying ApiKey-permissions gap** a WARNING (this item).
- The GroupMember analogue of gap (1) was fixed by DM-018
  (`MEMBER_PERMS_PROTECTION` + invite path + `kind="dict"`); mirror that design
  for `ApiKey` if going the protection-map route.
- Do NOT weaken the legitimate group-scoped ApiKey model: `is_group_allowed`
  (`api_key.py:124-133`) + instance-group rebind (`rest.py:267-268`) already
  confine keys correctly for models that HAVE a group FK. The gap is only
  groupless models.

- **Build outcome (2026-07-08, commit `251f8e4`)**: full suite 2329 / 2273 / **0
  failed** / 56 skipped (+6 = the new test file; baseline invariant held). All
  legitimate key flows stayed green (webhook subscription CRUD, geoip sync
  federation, chat, github, assistant memory, api_key unit tests).
- **Build note — `Group` needed a second fix.** The core `_evaluate_permission`
  Branch B fix closes the 13 standard groupless models, but `Group` overrides
  `check_view_permission` (`group.py:566`) which grants a key on
  `request.user.has_permission(perms)` — so detail-by-pk leaked any group to a
  key with `groups`. Added an `is_group_allowed` guard there (list was already
  confined via `ApiKey.get_groups` in Group's custom `on_rest_handle_list`).
  Group has no custom `check_edit_permission`, so writes fall to the fixed
  Branch B. Regression: `test_apikey_group_detail_confined`.

- **Post-review completion (security review, 2026-07-08) — two override
  bypasses fixed.** The core Branch B fix closes the 13 standard groupless
  models, but two models OVERRIDE the permission check and sidestep Branch B:
  1. **CRITICAL — `User.check_edit_permission` (`user.py:910`)** is consulted
     for VIEW-by-pk too (User has no `check_view_permission`), and returned
     `request.user.has_permission(perms)` = the key's self-claim → `GET
     /api/user/<pk>` still leaked any tenant's user. Fixed: it now denies an
     ApiKey identity (unless `ALLOW_API_KEY_GLOBAL`). My first-pass fix + list
     regression missed the detail-by-pk path.
  2. **WARNING — `Group.check_view_permission`** (my own DM-019 edit) gated
     SAVE too but ignored `perms`, so a **zero-permission** key could `PUT` its
     own group's `auth_config`/`geofence`. Fixed: require
     `is_group_allowed(self) AND has_permission(perms)`.
  Added regressions for both (`/api/user/<pk>` denial + no-leak; zero-perm key
  cannot write its own group). Defense-in-depth tail (unguarded
  `self.active_user.is_superuser` in `user.py`; non-empty
  `APIKEY_PERMS_PROTECTION` default) filed as
  `planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md` (P3) —
  not a live hole (the write path is now denied at the permission layer).

## Resolution
- closed: 2026-07-08
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geoip.md,docs/django_developer/account/group.md,docs/django_developer/account/login_events.md,docs/django_developer/account/push.md,docs/django_developer/assistant/README.md,docs/django_developer/aws/cloudwatch.md,docs/django_developer/core/decorators.md,docs/django_developer/core/permissions.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/jobs/admin.md,docs/django_developer/metrics/permissions.md,docs/web_developer/account/admin_portal.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/geoip.md,docs/web_developer/account/login_events.md,docs/web_developer/account/push.md,docs/web_developer/account/user.md,docs/web_developer/assistant/README.md,docs/web_developer/aws/cloudwatch.md,docs/web_developer/jobs/jobs.md,docs/web_developer/security/README.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/group.py,mojo/apps/account/models/member.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/device.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/login_event.py,mojo/apps/account/rest/push.py,mojo/apps/account/rest/user.py,mojo/apps/assistant/rest/assistant.py,mojo/apps/assistant/services/memory.py,mojo/apps/aws/rest/cloudwatch.py,mojo/apps/aws/rest/email.py,mojo/apps/aws/rest/email_ops.py,mojo/apps/aws/rest/messages.py,mojo/apps/aws/rest/s3.py,mojo/apps/aws/rest/send.py,mojo/apps/aws/rest/templates.py,mojo/apps/incident/rest/event.py,mojo/apps/jobs/rest/control.py,mojo/apps/jobs/rest/jobs.py,mojo/apps/metrics/rest/permissions.py,mojo/decorators/auth.py,mojo/models/rest.py,mojo/rest/model_permissions.py,planning/.next_id,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/in_progress/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/inbox/geofence-settings-write-validation-gap.md,planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md,tests/test_global_perms/__init__.py,tests/test_global_perms/_helpers.py,tests/test_global_perms/apikey_gate.py,tests/test_global_perms/apikey_groupless.py,tests/test_global_perms/escalation.py,tests/test_global_perms/invite_protection.py,tests/test_global_perms/model_permissions.py,uv.lock
  ALLOW_API_KEY_GLOBAL opt-out), mojo/apps/account/models/api_key.py
  (can_change_permission + set_permissions + APIKEY_PERMS_PROTECTION),
  mojo/apps/account/models/group.py (check_view_permission: confine +
  require-perm), mojo/apps/account/models/user.py (check_edit_permission
  denies keys), mojo/apps/assistant/services/memory.py (is_superuser crash),
  docs (account/api_keys.md, core/permissions.md, helpers/settings_reference.md,
  + web api_keys/admin_portal/geoip, django group/geoip via docs sweep),
  CHANGELOG.md
- tests added: tests/test_global_perms/apikey_groupless.py (7 —
  denied-on-groupless sweep, /api/user list + detail-by-pk no-leak, Group
  detail confinement + own-group allowed, Group SAVE by zero-perm key denied,
  group-scoped-model still-works, APIKEY_PERMS_PROTECTION assignment gate,
  assistant-memory no-500)
