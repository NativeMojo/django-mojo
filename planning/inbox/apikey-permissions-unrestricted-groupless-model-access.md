---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Self-minted group ApiKey with arbitrary permissions reaches groupless models cross-tenant
priority: P1
effort:
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-018, ITEM-017]
links: []
---

# Self-minted group ApiKey with arbitrary permissions reaches groupless models cross-tenant

## What & Why

Surfaced by ITEM-018's post-build security review (2026-07-08) as a WARNING +
the root cause of a CRITICAL. ITEM-018 closed the CRITICAL for the six
`requires_perms` AWS/device endpoints it covered (switched to
`requires_global_perms`), but the **underlying mechanism is broader** and
distinct from the `requires_perms` group-fallback ITEM-018 audited. It affects
the `@md.uses_model_security(...)` / `on_rest_request` path, so it is filed
separately rather than expanding ITEM-018 further.

Two independent gaps combine:

1. **`ApiKey.permissions` is unrestricted at assignment.**
   `mojo/apps/account/models/api_key.py:36` is a bare `JSONField` with only a
   `has_permission` getter — there is **no `set_permissions` / `can_change_permission`
   gate** analogous to the one `GroupMember` has (`member.py:84-104`, hardened
   by ITEM-018). A group admin who can create a group ApiKey via
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
ITEM-018 (these use `uses_model_security` directly, NOT `requires_perms`, so
ITEM-018 did not touch them):

- **`User`** (`account/rest/user.py`, `uses_model_security(User)`; User has no
  group FK) with a self-minted `users`/`manage_users` key → LIST/GET/UPDATE all
  users platform-wide (cross-tenant PII, and potentially permission edits).
- **`GeoLocatedIP`** (`account/rest/device.py:32`,
  `uses_model_security(GeoLocatedIP)`) with `security`/`manage_users` → global
  threat/geo data + firewall state surface.
- Audit **every** `uses_model_security` model for a missing `group` FK; each is
  a candidate (e.g. incident/metrics/logit/fileman globals).

ITEM-018 already closed the six AWS/device *function* endpoints
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
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- Origin: ITEM-018 post-build security review (2026-07-08). That review rated
  the **six covered endpoints** CRITICAL (now fixed in ITEM-018 commit) and the
  **underlying ApiKey-permissions gap** a WARNING (this item).
- The GroupMember analogue of gap (1) was fixed by ITEM-018
  (`MEMBER_PERMS_PROTECTION` + invite path + `kind="dict"`); mirror that design
  for `ApiKey` if going the protection-map route.
- Do NOT weaken the legitimate group-scoped ApiKey model: `is_group_allowed`
  (`api_key.py:124-133`) + instance-group rebind (`rest.py:267-268`) already
  confine keys correctly for models that HAVE a group FK. The gap is only
  groupless models.
