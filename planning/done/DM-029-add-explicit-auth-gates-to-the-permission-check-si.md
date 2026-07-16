---
# id is assigned by /scope on pickup — leave it blank
id: DM-029
type: chore
title: Add explicit auth gates to the permission-check sites that call has_permission on a possibly-anonymous request.user
priority: P3
effort:
owner:
opened: 2026-07-10
depends_on: []
related: []            # DM-028 (done, the anon-500 fix that surfaced these), DM-016 (choke-point guard)
links: []
---

# Add explicit auth gates to the permission-check sites that call has_permission on a possibly-anonymous request.user

## What & Why
DM-028 hardened `ANONYMOUS_USER.has_permission` to `lambda *a, **kw: False`
(arity-tolerant, always False), which fail-closes the anonymous-arity-500 at
*every* ungated site that calls `user.has_permission(perms)` /
`user_has_permission(request.user, …)` on an unauthenticated request. That is a
global safety net — but a few sites still lack their **own** explicit
`@md.requires_auth()` gate and rely solely on the sentinel's fail-closed return
plus a downstream `raise PermissionDeniedException()`. Making the gate explicit
is clearer and more robust: a future change to the sentinel, or to these helper
methods, should not be able to silently re-open an endpoint. This is
defense-in-depth / hygiene, not a known live vulnerability (post-DM-028 these
sites already fail closed with a clean 403).

Sites identified during DM-028 (from the security-review and Explore passes):
- `mojo/apps/chat/rest/rooms.py:300` (`_check_room_admin`) and `:312`
  (`_check_room_moderator`), plus related call sites in `mojo/apps/chat/...`
  (the DM-028 docs-updater counted ~5 `_check_room_admin`/`_check_room_moderator`
  call sites across `rooms.py`/`messages.py`).
- `mojo/apps/account/models/member.py:87` (`can_change_permission`, reached via
  `set_permissions`).

## Acceptance Criteria
- [ ] Every endpoint whose view body calls `.has_permission(...)` /
      `user_has_permission(request.user, …)` on a possibly-anonymous `request.user`
      either (a) carries an explicit `@md.requires_auth()` (or
      `@md.requires_perms(...)`) gate that runs before the body, or (b) is
      explicitly documented as intentionally relying on the fail-closed sentinel.
- [ ] No behavior change for authenticated callers; anonymous callers continue to
      get a clean 403 (already true post-DM-028).
- [ ] If `/scope` finds any site that is anonymously **reachable** AND was still
      returning a raw 500 that DM-028's lambda fix did NOT already resolve →
      reclassify this item as a **bug** and add a regression test for that site.

## Investigation — open questions for /scope
- **Reconcile the conflicting DM-028 findings (the crux):** the security-review
  listed `chat/rest/rooms.py:300`/`:312` and `member.py:87` as call sites with
  "no preceding `is_authenticated` gate" (i.e. anonymously reachable), while the
  docs-updater independently traced the chat `_check_room_admin`/`_check_room_moderator`
  call sites as **already** sitting behind `@md.requires_auth()` at the route
  level (pure defense-in-depth, no user-visible change). Both cannot be fully
  right — determine, per endpoint, whether the route is already auth-gated.
- For `member.can_change_permission`: identify which REST endpoints reach
  `set_permissions` → `can_change_permission` and whether each is auth-gated
  (e.g. `group/member/invite` is now gated by DM-028; check the member-update
  and any other paths).
- Confidence: the call sites exist (confirmed in DM-028); their reachability /
  existing gating is the only thing to verify. This may right-size to a very small
  change (or "already gated — document and close") depending on findings.
- Scope guard: this is about the *authentication* gate on these specific sites.
  Do NOT expand into re-auditing the whole permission system.

## Plan

**Scoping outcome (2026-07-10): NO ACTION NEEDED — verified every site is already authentication-gated. Closing without a code change** (user decision: close, no code change).

The DM-028 concern was a **false alarm**. The DM-028 security-review flagged `chat/rest/rooms.py:300`/`:312` and `member.py:87` as "no preceding auth gate," but it read the *helper functions* in isolation. A full per-endpoint recon shows every route that reaches one of these permission checks already carries `@md.requires_auth()` (or a stronger `requires_perms`/`requires_global_perms`), or is behind the model-security 401 short-circuit. DM-028's `ANONYMOUS_USER.has_permission = lambda *a, **kw: False` is a redundant fail-closed backstop, not the sole protection. There is nothing to gate.

Per-site verdict (all = already gated; anon cannot reach the `has_permission` call):
- **Chat room admin/mod — all 6 endpoints carry `@md.requires_auth()`:** `on_chat_room_add_member` (`rooms.py:114-116`), `on_chat_room_remove_member` (`155-157`), `on_chat_room_update_rules` (`220-222`), `on_chat_room_mute_member` (`176-178`), `on_chat_room_ban_member` (`198-200`), `on_chat_room_flagged` (`messages.py:76-78`). `_check_room_admin`/`_check_room_moderator` (`rooms.py:293-314`) are module-level helpers with no routes of their own.
- **`member.can_change_permission`** (`member.py:86-95`) — reached only via `on_group_invite_member` (`group.py:37` `requires_auth`, from DM-028) and `on_group_member` (`uses_model_security(GroupMember)`; `SAVE_PERMS` has no `"all"` → `rest.py:234-240` 401-blocks anon before the setter dispatch runs).
- **`group.check_view_permission`** (`group.py:575-597`, the `has_permission` at `:587`) — `VIEW_PERMS` has no `"all"` → same `rest.py:234-240` 401 short-circuit; the custom list path independently re-checks `is_authenticated` (`group.py:828`).
- **Full sweep** of `request.user.has_permission(` / `user_has_permission(request.user` across `rest/**` + models (oauth `:380`, assistant `:76`, metrics helpers, etc.) — every site gated by a `requires_*` decorator, the model-security 401, or a self-gating `is_authenticated` check before the `has_permission` call.

Deliberately NOT done (would be wrong, or pure noise):
- Do **not** convert the chat `@md.requires_auth()` to `@md.requires_perms("manage_chat")` — the helpers intentionally admit a room's own admin/moderator via `ChatMembership` (`rooms.py:296,308`) who may hold no group/global `manage_chat` grant. Route-level `requires_auth` + in-helper fine-grained check is the correct split.
- Do **not** add redundant `if not request.user.is_authenticated: raise` to the four helpers — pure noise given the callers' gates plus DM-028's fail-closed sentinel.

Adjacent, out of scope (noted, not filed): the metrics endpoints in `metrics/rest/{values,categories,base}.py` use `@md.custom_security(...)` + the self-gating `check_view/write_permissions` helpers (`metrics/rest/helpers.py`) rather than a `requires_*` decorator — safe today (helpers check `is_authenticated` before `has_permission`), same "annotation is not a gate" pattern DM-028 flagged. File separately only if a uniform decorator posture is later wanted.

## Notes
- Came out of DM-028's "Open questions" / post-build agent notes (2026-07-10).
- Recon agent enumerated all `has_permission`/`user_has_permission` call sites in `rest/**` + models and traced each reaching endpoint's decorator stack; verdict was uniform (a) already-gated with zero exceptions.

## Resolution
- closed: 2026-07-10
- branch: main
- files changed: none (verification-only; no code change)
- tests added: none (no code change — all sites verified already authentication-gated)
- outcome: NO ACTION NEEDED. The DM-028 security-review flag was a misread of the helpers in isolation; every reaching endpoint is already authentication-gated (full per-site recon in `## Plan`).
