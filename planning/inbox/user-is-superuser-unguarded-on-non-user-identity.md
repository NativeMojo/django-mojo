---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Unguarded self.active_user.is_superuser in user.py crashes (500) on a non-User identity
priority: P3
effort: XS
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-019, ITEM-018]
links: []
---

# Unguarded self.active_user.is_superuser in user.py crashes on a non-User identity

## What & Why

Defense-in-depth hardening flagged by ITEM-019's post-build security review
(2026-07-08). `request.user` / `self.active_user` can be a bare **`ApiKey`**
(under `Authorization: apikey <token>`), which has **no `is_superuser`
attribute**. Several `User` methods do `self.active_user.is_superuser` (or
`request.user.is_superuser`) **unguarded**, so a non-User identity reaching them
raises `AttributeError` → 500 instead of a clean permission decision. Sites the
review named (verify line numbers, they drift): `user.py` ~877
(`_handle_existing_user_pre_save`), ~825, ~462, ~467, ~573, ~601, ~651.

ITEM-019 already fixed the analogous crash in
`mojo/apps/assistant/services/memory.py` via
`getattr(user, "is_superuser", False)`, and — importantly — **closed the
security boundary** so a key can no longer reach `User`'s write path
(`User.check_edit_permission` now denies ApiKey identities). So these accesses
are **not currently a security hole** — a key is denied at the permission layer
before reaching them. This item is pure robustness: relying on an
`AttributeError` for denial is fragile, and a future refactor that "fixes" the
crash without preserving the deny could silently open a cross-tenant write.

## Acceptance Criteria

- [ ] Every `self.active_user.is_superuser` / `request.user.is_superuser` in
      `user.py` (and a grep of the wider codebase for the same pattern on an
      identity that can be an ApiKey) uses
      `getattr(identity, "is_superuser", False)` — behavior-preserving for real
      `User` identities, safe-`False` for `ApiKey`/anonymous.
- [ ] A regression: an ApiKey identity reaching one of these paths yields a
      clean permission decision (or 4xx), never a 500.
- [ ] Optional (INFO from the same review): evaluate a non-empty
      `APIKEY_PERMS_PROTECTION` default that requires a global/`sys.`-escalated
      grant to assign `manage_users`/`manage_groups`/`security` to a key —
      defense-in-depth independent of the (now-closed) groupless-model gate.
      Mirror the decision made for `MEMBER_PERMS_PROTECTION` (currently empty).

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- Origin: ITEM-019 post-build security review (2026-07-08). The review rated the
  two override bypasses (`User.check_edit_permission` cross-tenant read via
  `/api/user/<pk>`; `Group` SAVE by a zero-perm key) CRITICAL/WARNING — both
  fixed in ITEM-019. These `is_superuser` accesses were the INFO/robustness tail.
- Canonical idiom already in the codebase: `getattr(user, "is_superuser",
  False)` (ITEM-019, `memory.py`) and `hasattr(user, "is_request_user")` for the
  "is this a real request User?" test.
