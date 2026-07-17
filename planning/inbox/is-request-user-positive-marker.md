---
# id is assigned by /scope on pickup — leave it blank
id:
type: chore
title: Make the is_request_user marker a positive check — hasattr() is coincidentally satisfied by objict-shaped identities
priority: P3
effort:
owner:
opened: 2026-07-17
depends_on: []
related: [DM-045, DM-016, DM-037]
links: []
---

# Make the is_request_user marker a positive check — hasattr() is coincidentally satisfied by objict-shaped identities

## What & Why
From the DM-045 post-build security review (INFO finding, 2026-07-17).
`mojo.helpers.request.is_request_user(request)` — since DM-045 the single
predicate behind three security decisions (both auth-decorator gates and the
model-security machine-identity guard) — is `hasattr(user, "is_request_user")`.
That is an ABSENCE-based test, and `objict` (the framework's own recommended
"just data" container per CLAUDE.md) answers `hasattr(obj, ANY_NAME) == True`
for every name (`getattr` returns None instead of raising). Verified:
`hasattr(objict(a=1), "is_request_user") is True`.

Consequence: a custom `AUTH_BEARER_HANDLERS` identity that maps `request.user`
to an objict — a natural choice given the framework's own style guidance —
silently LOOKS like a real User to the predicate and is routed to the USER
branches with its self-claimed `has_permission` and no group confinement: the
exact class DM-045's `non_user_no_api_key` guard was built to close. The DM-045
regression test (`tests/test_global_perms/apikey_group_inactive.py`,
`test_unregistered_machine_identity_denied`) documents the trap in a comment and
deliberately uses plain classes to dodge it — the case is known but not covered.

Pre-existing (every pre-DM-045 hasattr site had the same hole); DM-045 raised
the stakes by making it the one chokepoint. Fix direction suggested by the
review: a positive marker instead of absence — e.g. a class attribute
`is_request_user = False` on a base/anonymous identity with only `account.User`
overriding it truthy (note `User.is_request_user` is currently a METHOD,
`user.py:297`, and some call sites invoke it — `user.py:924` — so the predicate
must handle attribute-vs-callable carefully), or `isinstance`-based checking.

Also fold in (same predicate, consistency): `mojo/decorators/limits.py:460-467`
(`_resolve_throttle_identity`) still hand-rolls the hasattr check — low security
relevance (throttle bucket keying only), switch it to the shared helper.

## Acceptance Criteria
- [ ] `is_request_user(request)` returns False for an objict-shaped
      `request.user` (regression test: an `objict(is_authenticated=True, ...)`
      identity must be treated as a machine, denied `non_user_no_api_key`).
- [ ] Real `account.User` identities still classify as request users at every
      existing call site (full suite green; DM-037/DM-045 suites green).
- [ ] The remaining hand-rolled `hasattr(user, "is_request_user")` sites are
      audited and either switched to the shared predicate or documented why not
      (`limits.py:467`, `group.py:216,282`, `user.py:302`, `rest/user.py:30`,
      `sms.py:42`, `rest.py:283,539` marker-read sites).
- [ ] No change to `is_request_user()` the User METHOD's call sites
      (`user.py:924`) — or they are migrated deliberately.

## Repro — bugs only
n/a (chore/hardening — no in-repo identity is objict-shaped today; the hole is
reachable only via a custom AUTH_BEARER_HANDLERS handler in a consuming app).

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Second INFO from the same review, needs no action now (design note only): a
  future group-scoped model with `VIEW_PERMS=["all"]` would still deny a
  suspended tenant's own api_key at the DM-045 pre-hook gate — intentional per
  ITEM-037; document if such a model ever appears.
