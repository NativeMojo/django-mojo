---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Audit requires_perms group-fallback on global-effect endpoints (privilege escalation class)
priority: P1
effort:
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-017]
links: []
---

# Audit requires_perms group-fallback on global-effect endpoints

## What & Why

ITEM-017's post-build security review confirmed a privilege-escalation class:
`@md.requires_perms(...)` (`mojo/decorators/auth.py:14-54`) falls back to
`request.group.user_has_permission(request.user, perms, True)` when the user
lacks the permission globally (`REQUIRES_PERMS_IS_GROUP` default True,
`auth.py:11`), and `request.group` is resolved from a **client-supplied**
`"group": <id>` field (`mojo/decorators/http.py:74`, `auth.py:42-43`).
`GroupMember.add_permission`/`set_permissions` accept **arbitrary string
keys** (gate `MEMBER_PERMS_PROTECTION` defaults `{}`, `member.py:8-9`), so any
tenant/group admin can hand a teammate any permission name *scoped to their
own group* — and that grant satisfies `requires_perms` on ANY endpoint if the
caller just adds their own group id to the request.

That is correct for endpoints whose effect is actually scoped to
`request.group` (the framework's normal pattern), and a cross-tenant
escalation for endpoints whose effect is **global**. ITEM-017's geofence
config plane was fixed with a local `_requires_global_perms`
(`mojo/apps/account/rest/geofence.py:46-80` — global `User.permissions` /
superuser only, registered in SECURITY_REGISTRY with `global_only: True`).

The review flagged at least one other global-effect endpoint using the
fallback: `mojo/apps/jobs/rest/control.py:15-16` (global job-system control).
The full surface has NOT been audited.

## Acceptance Criteria

- [ ] Enumerate every `@md.requires_perms` / `@md.requires_group_perms` usage
      and classify each endpoint's effect: group-scoped (fallback correct) vs
      global (fallback = escalation).
- [ ] Global-effect endpoints stop honoring group-scoped grants — either
      reuse/centralize `_requires_global_perms` (promote it out of
      `rest/geofence.py` into `mojo/decorators/auth.py`, e.g.
      `requires_global_perms`) or an equivalent explicit check.
- [ ] Regression tests for each fixed endpoint mirroring
      `tests/test_geofence/config_plane.py::test_group_scoped_perm_cannot_touch_global_config`.
- [ ] Consider whether `MEMBER_PERMS_PROTECTION` should ship a non-empty
      default (allowlist/denylist of member-assignable permission keys) —
      that would shrink the whole class, but is a behavior change for
      existing deployments; scope decides.
- [ ] Docs: `docs/django_developer/core/permissions.md` explains when the
      group fallback applies and how to opt out for global-effect endpoints.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- Origin: ITEM-017 post-build security review (2026-07-08), which rated the
  geofence instance CRITICAL (platform-wide jurisdiction rules + IP allowlist
  writable by a single-tenant admin). The geofence surface is already fixed;
  this item is the rest of the audit.
- The reviewer verified the fallback mechanism itself is long-standing,
  pre-existing behavior — the audit is about which endpoints sit on top of it.
