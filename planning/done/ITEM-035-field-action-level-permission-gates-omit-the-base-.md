---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-035
type: bug
title: Field/action-level permission gates omit the base groups/users permission that the same resource's broader gate already accepts (ApiKey, GroupMember, Group, OAuthConnection, GeoLocatedIP)
priority: P2
effort: M
owner: backend
opened: 2026-07-12
depends_on: []
related: []
links: []
---

# Field/action-level permission gates omit the base groups/users permission that the same resource's broader gate already accepts

## What & Why
Several models define TWO permission gates for the same resource: a broad one
(RestMeta `VIEW_PERMS`/`SAVE_PERMS`) that accepts the base-tier permission
(`"groups"` or `"users"`), and a narrower one (a custom method, or a
stricter action-specific perm list) guarding one field or action that
requires the *elevated* `"manage_groups"`/`"manage_users"` tier instead —
without also accepting the base-tier permission as a fallback. A user/member
holding only the base permission passes the broad gate (can view/create/save
the resource generally) and then hits an unexpected 403 on the narrower
action, with nothing about the broad gate warning them it was coming.

Discovered on `ApiKey`: a group member holding only `"groups"` can
create/save an API key (`RestMeta.SAVE_PERMS`, api_key.py:56, includes
`"groups"`) but cannot set *any* permission on it —
`can_change_permission`'s fallback list (api_key.py:162-163) is
`["manage_group","manage_members","manage_users","manage_groups"]`, missing
`"groups"`. A repo-wide audit for the same shape (grep every
`manage_groups`/`manage_users` site, trace its paired gate, check for the
base-tier omission) found 4 more confirmed instances:

1. **`GroupMember.can_change_permission`**
   (`mojo/apps/account/models/member.py:94`) — identical fallback list,
   identical missing `"groups"`, even though `VIEW_PERMS`/`SAVE_PERMS`
   (member.py:46-47) include it. **This is a live bug in the already-shipped
   Group Member permission editor** (web-mojo's `MemberView.js`) — any member
   with only `"groups"` gets a 403 toggling a permission on a fellow member
   today, not just a theoretical risk.
2. **`Group.on_action_disable` / `on_action_reactivate`**
   (`mojo/apps/account/models/group.py:630-631`, `:649-650`) — each requires
   bare `"manage_groups"` with no `"groups"` fallback, even though the outer
   `SAVE_PERMS` gate that lets a caller reach these `POST_SAVE_ACTIONS`
   (group.py:49-50, 53; enforced via `check_edit_permission`,
   group.py:599-611) includes `"groups"`.
3. **`OAuthConnection` admin-bypass delete**
   (`mojo/apps/account/rest/oauth.py:392-393`) — requires bare
   `"manage_users"`; `VIEW_PERMS`/`SAVE_PERMS`
   (`mojo/apps/account/models/oauth.py:35-36`) include `"users"`, and this
   admin-bypass check is the *only* delete gate for connections you don't own
   (`on_oauth_connection`, oauth.py:385-386, routes every DELETE here with no
   framework-level `DELETE_PERMS` fallback consulted).
4. **`GeoLocatedIP`** (`mojo/apps/account/models/geolocated_ip.py:105-106`) —
   `VIEW_PERMS` (line 105) includes `"users"`, `SAVE_PERMS` (line 106)
   doesn't — blocks a `"users"`-tier admin from the model's
   `POST_SAVE_ACTIONS` (line 108: `refresh`, `threat_analysis`, `block`,
   `unblock`, `whitelist`, `unwhitelist`) on an IP they can otherwise see.

By contrast, the equivalent `User.on_action_disable`/`on_action_reactivate`
methods (`mojo/apps/account/models/user.py:932,950`) correctly use
`["users","manage_users"]` — confirming this is an inconsistency, not an
intentional design choice, and that `Group`'s methods are the outlier
relative to their own closest analog in the same codebase.

## Acceptance Criteria
(Reworked during /scope — user confirmed the intended semantics: the bare
domain term `"users"`/`"groups"` is `view_X` + `manage_X` **combined into one
simple term**, not a lower tier. The fix is therefore central — Option B —
rather than patching each perm list; see `## Plan`.)

- [ ] Holding the bare domain term satisfies any permission check for its
      `view_X` / `manage_X` forms, at all three checker levels
      (`User.has_permission`, `GroupMember.has_permission`,
      `ApiKey.has_permission`). One-directional: `manage_X` alone does NOT
      satisfy a check for bare `X`.
- [ ] Expansion applies only to the seven domain categories
      (`users`, `groups`, `security`, `comms`, `jobs`, `metrics`, `files`) —
      NOT to fine-grained perms (`manage_group`, `manage_members`,
      `manage_settings`, …).
- [ ] All seven affected gates now admit a bare-term holder with **no edits
      to their perm lists**: ApiKey `can_change_permission`
      (api_key.py:149, 162-163), GroupMember `can_change_permission`
      (member.py:87, 94), `Group.on_action_disable`/`on_action_reactivate`
      (group.py:630, 649), OAuthConnection admin-bypass delete
      (rest/oauth.py:393), `GeoLocatedIP.SAVE_PERMS` (geolocated_ip.py:106),
      group-member invite (rest/group.py:53).
- [ ] Regression test per gate: a principal holding ONLY the bare term
      (`"groups"` or `"users"`) can perform the previously-blocked action.
- [ ] Unit tests for the expansion itself, including the one-directional and
      non-category negative cases.
- [ ] No regression for principals holding the elevated permission or global
      admin/superuser.
- [ ] Docs updated (both tracks) to state the combined-term rule; CHANGELOG
      entry.

## Repro — bugs only
1. (ApiKey) Member M of group G holds ONLY `"groups"` (no
   `manage_group`/`manage_groups`). M creates an API key on G (succeeds). M
   attempts to set any permission on that key.
   - Expected: succeeds (M already proved they can manage keys in this group
     by creating one).
   - Actual: `PermissionDeniedException` — `can_change_permission` requires
     `manage_group`/`manage_members`/`manage_users`/`manage_groups`;
     `"groups"` isn't in that list (api_key.py:162-163).
2. (GroupMember — same shape) Member M with only `"groups"` attempts to
   toggle a permission on a fellow member via `POST /api/group/member` with a
   `permissions.<x>` key.
   - Expected: succeeds.
   - Actual: 403 (member.py:94).

## Investigation
Confidence: **confirmed** for findings 1-4 (each traced to the exact gate
plus its paired broader gate, file:line, cross-checked against how the
framework actually resolves these lists —
`_evaluate_permission`/`check_edit_permission`, rest.py:202-352). One
additional **lower-confidence** finding, deliberately left OUT of Acceptance
Criteria above because it compares two different actions rather than two
gates on the identical endpoint: the group-member invite endpoint
(`mojo/apps/account/rest/group.py:53`, perms list missing `"groups"`) vs.
direct member creation (`member.py:47`, includes `"groups"`) — the invite
path also auto-creates a brand-new `User` when the invitee has no account
(rest/group.py:410-418), so requiring a higher bar there is plausibly
deliberate rather than a bug. Flagged for /scope to decide whether to fold in
or explicitly reject.

Per-instance evidence for why each shipped unnoticed (existing test coverage
never exercised the base-tier-only case):
- ApiKey: no existing test exercises a bare-`"groups"` member against
  `can_change_permission`.
- GroupMember: `tests/test_global_perms/invite_protection.py` uses
  member-level `"manage_group"`, never bare `"groups"`, against this gate.
- Group: `tests/test_account/test_disable_lifecycle.py:541-570`
  (`test_rest_group_disable_reactivate`) grants the test admin BOTH
  `manage_users` and `manage_groups` (setup lines 30-31) — bare `"groups"`
  never tested.
- OAuthConnection / GeoLocatedIP: no test found exercising the base-tier
  permission against these specific narrow gates.

Regression-test feasibility: high for all 4 — each model already has REST
test coverage to extend with a base-permission-only fixture.

## Plan

### Goal
Make the permission checkers themselves treat the bare domain term
(`"users"`, `"groups"`, …) as satisfying checks for its `view_X`/`manage_X`
forms, so every gate that accepts `manage_X` automatically accepts bare `X` —
fixing all seven audited instances centrally and making this bug class
structurally impossible in future perm lists.

### Context — what exists

**Semantics (user-confirmed, 2026-07-12):** the bare domain term is
`view_X` + `manage_X` combined into one simple term — NOT a lower tier. The
historical failure mode is a hand-written perm list that remembers `manage_X`
but forgets `view_X`/bare `X` ("can edit but can't view"). Option A (patch
each of the 7 lists) was proposed and rejected in favor of this central fix
(Option B), explicitly approved by the user.

**The three permission checkers** (all end in a plain dict lookup — this is
where expansion goes):

- `User.has_permission` — `mojo/apps/account/models/user.py:492-503`:
  superuser ⇒ True; list/set ⇒ OR-recurse; `"all"`/`"authenticated"` ⇒ True;
  else `return self.permissions.get(perm_key, False)` (line 503).
- `GroupMember.has_permission` — `mojo/apps/account/models/member.py:108-129`:
  list/set OR; `"sys."` prefix delegates to `self.user.has_permission(bare)`
  (line 125 — user-level expansion will apply there automatically);
  `"all"`/`"authenticated"`/`"member"` ⇒ True; else
  `return self.permissions.get(perm_key, False)` (line 129).
- `ApiKey.has_permission` — `mojo/apps/account/models/api_key.py:115-134`:
  list/set OR; `sys.*` always False; `"all"`/`"authenticated"`/`"member"` ⇒
  True; else `return bool(self._get_permissions_dict().get(perm_key, False))`
  (line 134).

**How gates resolve** (`mojo/models/rest.py:202-352`,
`_evaluate_permission`): RestMeta perm lists are evaluated by OR-membership
via the checkers above — group-scoped models go through
`Group.user_has_permission(user, perms)` (`group.py:213-221`: tries
`user.has_permission` globally, then falls back to the member row via
`get_member_for_user(..., check_parents=True)`), flat models via
`request.user.has_permission(perms)`. So fixing the three checkers fixes
every gate, RestMeta list, and inline `has_permission([...])` call at once.

**The seven gates this repairs (verified current code — do not edit these):**
1. `api_key.py:149` global bypass `["manage_groups", "manage_users"]` and
   `api_key.py:162-163` member fallback
   `["manage_group", "manage_members", "manage_users", "manage_groups"]`
   inside `can_change_permission` (invoked by `set_permissions`,
   api_key.py:166-182, dispatched via `on_rest_save_field`,
   rest.py:1349-1360).
2. `member.py:87` global bypass and `member.py:94` member fallback — same
   lists, same shape, inside `GroupMember.can_change_permission` (invoked by
   `set_permissions`, member.py:97-106, and by the invite endpoint).
3. `group.py:630` and `group.py:649` —
   `if not self.active_user.has_permission("manage_groups"):` in
   `on_action_disable`/`on_action_reactivate`. (User analog at
   `user.py:932, 950` already uses `["users", "manage_users"]`.)
4. `mojo/apps/account/rest/oauth.py:393` —
   `if request.user.has_permission("manage_users"):` — the admin bypass in
   `on_oauth_connection_delete`, the ONLY delete path for a connection you
   don't own (the wrapper at oauth.py:385-386 routes every DELETE here; the
   owner path filters `user=request.user` ⇒ non-owner gets 404).
5. `mojo/apps/account/models/geolocated_ip.py:106` —
   `SAVE_PERMS = ['manage_users', 'manage_security', 'security']` (VIEW_PERMS
   line 105 includes `'users'`). Actions gated by it: `refresh`,
   `threat_analysis`, `block`, `unblock`, `whitelist`, `unwhitelist`
   (line 108) — block/unblock/whitelist drive real fleet-wide firewall
   broadcasts (`jobs.broadcast_execute` → ipset/iptables), which under the
   confirmed semantics a bare-`"users"` holder is entitled to (they hold
   `manage_users` by definition).
6. `mojo/apps/account/rest/group.py:53` — invite endpoint perms
   `["manage_users", "manage_members", "manage_group", "manage_groups"]`,
   checked via `request.group.user_has_permission(...)`. Note `Group.invite`
   (`models/group.py:400-423`) auto-creates a `User` when the invitee email
   has no account — acceptable, since a bare-term holder already holds
   `manage_users`/`manage_groups` by definition.
7. (Class-wide) any future list that names `manage_X` without bare `X`.

### Changes — what to do

1. **Create `mojo/helpers/perms.py`** (new; nothing similar exists in
   `mojo/helpers/` — checked). No stdlib logging, no type hints:

   ```python
   # Domain-category permissions. The bare term ("users") is view_users and
   # manage_users combined into one simple term — holding it satisfies any
   # check for either. One-directional: manage_users does NOT imply "users".
   DOMAIN_CATEGORIES = {"users", "groups", "security", "comms", "jobs", "metrics", "files"}

   def implied_perms(perm_key):
       """Perm keys whose holder satisfies a check for perm_key."""
       for prefix in ("view_", "manage_"):
           if perm_key.startswith(prefix):
               base = perm_key[len(prefix):]
               if base in DOMAIN_CATEGORIES:
                   return (perm_key, base)
       return (perm_key,)
   ```

2. **`mojo/apps/account/models/user.py:503`** — replace the final lookup in
   `has_permission`:
   ```python
   from mojo.helpers.perms import implied_perms   # at module import block
   ...
   return any(bool(self.permissions.get(pk, False)) for pk in implied_perms(perm_key))
   ```

3. **`mojo/apps/account/models/member.py:129`** — same one-line change on
   the final lookup (leave the `sys.` branch alone — it delegates to
   `User.has_permission`, which now expands).

4. **`mojo/apps/account/models/api_key.py:134`** — same change:
   `return any(bool(self._get_permissions_dict().get(pk, False)) for pk in implied_perms(perm_key))`.
   (Compute `_get_permissions_dict()` once into a local before the loop.)

5. **`mojo/apps/account/models/group.py:632, 651`** (the two
   `PermissionDeniedException` messages, currently
   `"manage_groups required to disable a group"` etc.) — reword to match the
   User analog's style: `"admin tier (groups / manage_groups) required to
   disable a group"` / `"... to reactivate a group"`. Do NOT change the
   checks themselves — expansion handles them.

6. **No edits to any of the seven perm lists.** That's the point of Option B.

7. **Docs** — see Docs section. **CHANGELOG.md** — entry under the current
   rolling block: bare domain terms now satisfy view_/manage_ checks
   centrally; list the seven previously-blocked gates now open to bare-term
   holders.

### Design decisions
- **Central expansion over per-list patches (Option A rejected, by user):**
  seven known sites today, and the class regenerates every time someone
  writes a new list. The checker is the single choke point.
- **Fixed whitelist of domain categories** (the seven from
  `.claude/rules/models.md`) rather than expanding any `view_*`/`manage_*`
  suffix: prevents nonsense implications like member-scoped `manage_group`
  (singular) ← `"group"`, or `manage_members` ← `"members"`, or
  `manage_settings` ← `"settings"` — none of those bare terms are defined
  permissions.
- **One-directional:** `manage_users` does not satisfy a check for bare
  `"users"`. Lists that accept the bare term (e.g. `SAVE_PERMS = [...,
  "groups"]`) always also name the manage forms explicitly, so no reverse
  implication is needed — and adding one would silently promote manage-tier
  holders to the combined tier.
- **Expansion also reaches the `*_PERMS_PROTECTION` maps** (protection
  values are checked via `req_member.has_permission(protection[perm])`,
  api_key.py:160-161 / member.py:92-93): a protection entry requiring
  `manage_users` is now satisfied by bare `"users"`. This is correct under
  the confirmed semantics, not a leak.
- **Group disable/reactivate stays a global (user-level) check** via
  `self.active_user.has_permission(...)`, exactly like the User analog
  (user.py:932, 950). A member holding member-level-only `{"groups": True}`
  still can't disable the group — unchanged relative behavior, consistent
  with the closest analog; not part of this bug.
- **ApiKey keys granted a bare domain term** now pass manage-tier checks
  too. Intended: that is what the term means. `sys.*` remains always-denied
  for keys.

### Edge cases & risks
- Strictly widening for holders of exactly the seven bare terms; all other
  perms behave identically. Superuser, `"all"`, `"authenticated"`,
  `"member"`, and empty-list (allow) paths untouched.
- `sys.manage_users` at member level → `User.has_permission("manage_users")`
  → expands → member holding `sys.` nothing but user holding bare `"users"`
  passes. Correct.
- **Existing tests may encode the old buggy expectation** — most likely
  `tests/test_global_perms/escalation.py` (sweeps `ALL_ENDPOINT_PERMS`,
  which includes bare terms, against endpoints asserting denial). Per
  testing rules: if such a test now fails because it asserts the bug, fix
  the test — but verify each flip against the semantics (bare term should
  pass wherever manage does) before touching it. Baseline run first, per
  build-baseline rule.
- No model/schema changes → no `bin/create_testproject` needed.
- Performance: two string `startswith` + a set lookup per check — negligible.

### Tests
All testit (`@th.django_unit_test()`, `opts`, descriptive assert messages;
read `docs/django_developer/testit/Overview.md` first). Setup must delete
rows it will create (long-lived DB). Remember `opts.client` hits a separate
server process — no `mock.patch`; use `th.server_settings()` if a Setting
override is needed.

**New file `tests/test_global_perms/base_term_expansion.py`** — reuse
`tests/test_global_perms/_helpers.py` (`make_user(perms)` grants global
perms; `make_group_member(perms)` grants member-level-only perms):

Unit-level (direct checker calls):
- user with `{"users": True}` → `has_permission("manage_users")` and
  `has_permission("view_users")` both True.
- user with `{"manage_users": True}` → `has_permission("users")` False
  (one-directional).
- member with `{"groups": True}` → `has_permission("manage_groups")` True;
  `has_permission("manage_group")` False (singular is not a category).
- ApiKey with `permissions={"groups": True}` →
  `has_permission("manage_groups")` True; `has_permission("sys.manage_users")`
  still False.
- user with `{"members": True}` → `has_permission("manage_members")` False
  (non-category suffix not expanded).

End-to-end regressions (each must FAIL before the fix, PASS after):
1. **ApiKey perm change** (extend `tests/test_user_mgmt/api_keys.py`;
   existing setup at lines 10-40 grants admin `manage_group`/`manage_groups`
   — add a member holding ONLY member-level `{"groups": True}`): that member
   POSTs `/api/group/apikey` creating a key with a `permissions` dict (pick
   a perm NOT in any `APIKEY_PERMS_PROTECTION` Setting; clear/avoid that
   Setting in setup) → expect 200. (Repro 1 in this item.)
2. **Member perm toggle** (in the new file, fixtures from `_helpers.py`):
   member with only `{"groups": True}` POSTs
   `/api/group/member/<pk>` with `{"permissions": {"view_data": true}}` on a
   fellow member → expect 200. (Repro 2; the shipped MemberView.js bug.)
3. **Group disable/reactivate** (extend
   `tests/test_account/test_disable_lifecycle.py`; mirror
   `test_rest_group_disable_reactivate` at lines 541-570 but the actor holds
   ONLY global `"groups"` via `add_permission("groups")`, not the
   `manage_users`/`manage_groups` the existing admin gets at lines 30-31) →
   both actions 200.
4. **OAuth admin delete** (extend `tests/test_oauth/oauth.py`; mirror
   `test_oauth_connection_admin_delete` at lines 784-801 but actor holds
   ONLY `add_permission(["users"])`) → deleting another user's connection
   succeeds.
5. **GeoLocatedIP save gate** (extend `tests/test_account/` geoip tests):
   logged-in user holding ONLY global `{"users": True}` POSTs
   `/api/system/geoip/<pk>` — assert NOT permission-denied. Pick the least
   side-effectful action (`unwhitelist` on a non-whitelisted row, or
   `threat_analysis`) and follow the existing patterns in
   `test_geoip_actions.py` / `test_geoip_sync_endpoint.py`; note existing
   action tests bypass HTTP (they call `on_rest_save` with a fake request),
   so this is the first HTTP-gate test — builder must check what
   `jobs.broadcast_execute`/external lookups do in the test server before
   choosing the action.
6. **Invite** (in the new file): member with only `{"groups": True}` POSTs
   `/api/group/member/invite` with the email of an EXISTING user (avoids the
   auto-create-User + send_invite email path) → expect 200.

Run: `bin/run_tests --agent` (baseline BEFORE any edit, per
`.claude/rules/build-baseline.md`), read `var/test_failures.json`.

### Docs
- `docs/django_developer/` — the page documenting RestMeta
  permissions/`VIEW_PERMS`/`SAVE_PERMS` (locate via
  `grep -rl "SAVE_PERMS" docs/django_developer/`): add the combined-term
  rule — bare domain term = view+manage combined, expansion is automatic in
  the checkers, list `DOMAIN_CATEGORIES`, note one-directionality.
- `docs/web_developer/` — wherever endpoint permission requirements name
  `manage_users`/`manage_groups` for the affected endpoints (group
  disable/reactivate, oauth_connection delete, apikey/member permission
  editing, invite): note the bare term also suffices.
- `CHANGELOG.md` — behavior change entry (see Changes #7).

### Open questions
None — semantics and approach (Option B) explicitly confirmed by the user;
GeoLocatedIP and the invite endpoint fold-ins resolved (both IN, as
consequences of the central fix, no list edits).

## Notes
- **Baseline (2026-07-12, before any edit):** `bin/run_tests --agent` →
  total 2427 / passed 2371 / failed 0 / skipped 56. All green — any
  post-change failure is attributable to this build.
- Discovered via an audit prompted by investigating the `ApiKey` permissions
  bug — sibling item filed separately (different root cause, mechanical
  string-vs-dict fix, different acceptance criteria):
  `apikey-set-permissions-drops-non-dict-values.md`. Also related (other
  repo, unscoped): a web-mojo item redesigning the GroupView API Key
  permissions editor to mirror the Group Member permission UI. Backfill
  `related:` on all three once scoped and IDed.
- Audit also checked (all consistent, no asymmetry found, not included
  here): `phonehub/models/config.py:63-65`;
  `account/models/push/{config,template,delivery,device}.py`;
  `phonehub/models/phone.py:70-71`; several
  `assistant/services/tools/{groups,users}.py` tool decorators;
  `mojo/rest/model_permissions.py:86,122`; `user.py:1490` (has a working
  fallback below it, not a dead-end). `account/models/setting.py:37-38`
  pairs `"manage_settings"` with `"groups"` (reverse direction, different
  permission entirely) — FYI only, not in scope here.
- Good counter-example found in the same audit, for calibration:
  `account/services/inactive.py:163-167` already treats `manage_groups` and
  `groups` as equivalent via an explicit OR query — this is the correct
  pattern the 4 confirmed findings above should converge on.

## Resolution
- closed: 2026-07-12
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/api_keys.md,docs/django_developer/core/permissions.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/geoip.md,docs/web_developer/account/group.md,docs/web_developer/account/oauth.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/group.py,mojo/apps/account/models/member.py,mojo/apps/account/models/user.py,mojo/helpers/perms.py,planning/.next_id,planning/done/ITEM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/in_progress/ITEM-035-field-action-level-permission-gates-omit-the-base-.md,tests/test_account/test_disable_lifecycle.py,tests/test_global_perms/base_term_expansion.py,tests/test_oauth/oauth.py,tests/test_user_mgmt/api_keys.py
- tests added: tests/test_global_perms/base_term_expansion.py (3 checker-expansion tests + member perm-toggle, invite, geoip SAVE-gate regressions); tests/test_user_mgmt/api_keys.py::apikey_rest_create_bare_groups_member; tests/test_account/test_disable_lifecycle.py::test_rest_group_disable_reactivate_bare_groups; tests/test_oauth/oauth.py::test_oauth_connection_bare_users_admin_delete
