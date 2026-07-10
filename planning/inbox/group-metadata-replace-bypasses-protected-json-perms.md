---
id:
type: bug
title: JSONField __replace bypasses PROTECTED_JSON_PERMS — manage_group apikey can rewrite metadata.protected wholesale
priority: P1
effort: S
owner:
opened: 2026-07-10
depends_on: []
related: []
links:
  - wmwx/wmx_api WMX-API-127 (security review finding, 2026-07-10)
---

# `{"metadata": {"__replace": true, ...}}` skips the protected-JSON guard

## What & Why

`on_rest_update_jsonfield` (mojo/models/rest.py ~1494-1506) runs the
`PROTECTED_JSON_PERMS` guard (`_can_edit_protected_json`) and the audit log
only on the **merge** branch. A client-supplied top-level
`{"__replace": true}` takes the replace branch, which does a wholesale
`setattr` of the JSONField with **no** protected-key check.

Concrete impact (found while building wmx's geofence checkout-parity push):
`Group.SAVE_PERMS` is `["manage_groups", "manage_group", "groups"]` and
`PROTECTED_JSON_PERMS = ["admin_compliance", "admin_verify"]`. A group-scoped
ApiKey holding only `manage_group` can `POST /api/group/<pk>` with
`{"metadata": {"__replace": true, ...}}` and rewrite the ENTIRE metadata dict
— including `metadata.protected.*`. On mverify that includes
`metadata.protected.payments.allowed_origins`, the hosted checkout page's
`frame-ancestors` CSP + postMessage origin allowlist: a compromised partner
integration key could whitelist an attacker origin to frame the real checkout
page, despite the protected-perm gate existing precisely to prevent that.
(Scope limit: apikey writes stay confined to the key's own group tree — no
cross-tenant reach.)

The same bypass applies to every model using PROTECTED_JSON_PERMS-guarded
JSONFields, and the replace branch also skips the protected-edit audit log.

## Repro

1. Group with `metadata.protected.payments.allowed_origins` set.
2. ApiKey on that group with `manage_group` only (no admin_compliance).
3. `POST /api/group/<pk>` body
   `{"metadata": {"__replace": true, "protected": {"payments": {"allowed_origins": ["https://evil.example"]}}}}`
4. Expected: 403 (protected key, insufficient perms). Actual: 200, metadata
   replaced wholesale, no audit event.

## Suggested fix

In the replace branch, when the INCOMING value or the EXISTING field value
contains a `protected` key (or any PROTECTED_JSON_ROOTS), require
`_can_edit_protected_json` exactly as the merge branch does, and emit the
same audit event. Alternatively: disallow `__replace` entirely on fields
that declare protected roots.

## Acceptance Criteria

- [ ] `__replace` on a JSONField with protected content 403s without
      PROTECTED_JSON_PERMS (both directions: incoming carries `protected`,
      or existing value has it and would be clobbered)
- [ ] Protected-edit audit event fires on the replace path
- [ ] Regression test covering the ApiKey/manage_group repro above
