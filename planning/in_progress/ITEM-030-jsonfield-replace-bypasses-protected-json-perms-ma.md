---
id: ITEM-030
type: bug
title: JSONField __replace bypasses PROTECTED_JSON_PERMS — manage_group apikey can rewrite metadata.protected wholesale
priority: P1
effort: S
owner: backend
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

## Plan

### Goal

Make the `PROTECTED_JSON_PERMS` guard and its `meta:protected_changed` audit
event cover every write path in `on_rest_update_jsonfield` — replace
(`__replace` / `JSON_REPLACE_FIELDS`) and non-dict wholesale overwrite — not
just the merge branch.

### Context — what exists

All logic lives in one function, `mojo/models/rest.py:1476-1516`
(`on_rest_update_jsonfield`), dispatched from `on_rest_save_field` at
rest.py:1343 for every JSONField in a REST save. Current structure:

```python
existing_value = getattr(self, field_name, {}) or {}          # :1490
if isinstance(field_value, dict) and isinstance(existing_value, dict):
    replace_fields = self.get_rest_meta_prop("JSON_REPLACE_FIELDS", [])
    should_replace = field_name in replace_fields or field_value.pop("__replace", False)  # :1494
    if should_replace:
        setattr(self, field_name, field_value)                # :1497 — UNGUARDED
    else:
        if "protected" in field_value:                        # :1500 — guard, merge only
            if not self._can_edit_protected_json(request):
                raise me.PermissionDeniedException("Permission denied: cannot modify protected metadata")
            ...changed_keys diff...                           # :1505
            self.model_logit(..., kind="meta:protected_changed")  # :1506
        merged_value = objict.merge_dicts(existing_value, field_value)
        setattr(self, field_name, merged_value)
    ...LOG_META_CHANGES generic log (:1510-1514, both branches)...
else:
    setattr(self, field_name, field_value)                    # :1516 — UNGUARDED
```

- `_can_edit_protected_json` (rest.py:1518-1530): False if `request is None`
  or anonymous; True for superuser; else `user.has_permission(PROTECTED_JSON_PERMS)`.
  Plain instance method — callable identically from any branch.
- The only model declaring `PROTECTED_JSON_PERMS` is `account.Group`
  (mojo/apps/account/models/group.py:51): `["admin_compliance", "admin_verify"]`,
  guarding `Group.metadata`. `Group.SAVE_PERMS = ["manage_groups", "manage_group", "groups"]`.
- `incident.Ticket` declares `JSON_REPLACE_FIELDS = ["metadata"]`
  (mojo/apps/incident/models/ticket.py:105) — its metadata always takes the
  replace path. It has no `PROTECTED_JSON_PERMS`, so the guard there is
  superuser-only and fires only if a ticket's metadata actually contains a
  `protected` key (it doesn't in framework code — no behavior change expected).
- Two unguarded write paths exist, not one:
  1. **Replace branch** (:1496-1497) — the reported bug.
  2. **Outer else** (:1515-1516) — incoming non-dict (string that fails JSON
     parse, list, null) or existing non-dict: wholesale `setattr` clobbers an
     existing `protected` subtree with no check. Same class of bypass; fix it
     in the same pass.
- No `PROTECTED_JSON_ROOTS` constant exists; the protected root is the
  hardcoded literal `"protected"`. Keep it that way (KISS).
- No existing test exercises `PROTECTED_JSON_PERMS` at all.
  `tests/test_account/test_group_save_perms.py` is the copy-pattern for
  group + limited-perm member/ApiKey REST tests (see Tests below).

### Changes — what to do

**1. `mojo/models/rest.py` — restructure `on_rest_update_jsonfield` (only file with code changes).**

Hoist the guard above the merge/replace split so one check covers both, and
add the same check to the outer else:

```python
existing_value = getattr(self, field_name, {}) or {}
if isinstance(field_value, dict) and isinstance(existing_value, dict):
    replace_fields = self.get_rest_meta_prop("JSON_REPLACE_FIELDS", [])
    should_replace = field_name in replace_fields or field_value.pop("__replace", False)
    # guard the "protected" root key — merge touches it only when the incoming
    # dict carries it; a replace also clobbers any existing protected subtree
    touches_protected = "protected" in field_value or (should_replace and "protected" in existing_value)
    if touches_protected:
        if not self._can_edit_protected_json(request):
            raise me.PermissionDeniedException("Permission denied: cannot modify protected metadata")
        # always audit protected metadata changes regardless of LOG_CHANGES/LOG_META_CHANGES
        username = getattr(getattr(request, "user", None), "username", "unknown")
        incoming_prot = field_value.get("protected")
        existing_prot = existing_value.get("protected")
        if isinstance(incoming_prot, (dict, type(None))) and isinstance(existing_prot, (dict, type(None))):
            incoming_prot = incoming_prot or {}
            existing_prot = existing_prot or {}
            # merge: only keys the caller sent can change; replace: removed keys change too
            keys = set(incoming_prot) | (set(existing_prot) if should_replace else set())
            changed_keys = sorted(k for k in keys if incoming_prot.get(k) != existing_prot.get(k))
        else:
            changed_keys = ["*"]
        self.model_logit(request, f"{username} modified {field_name}.protected keys: {', '.join(changed_keys)} on pk={self.pk}", kind="meta:protected_changed")
    if should_replace:
        setattr(self, field_name, field_value)
    else:
        setattr(self, field_name, objict.merge_dicts(existing_value, field_value))
    # ...LOG_META_CHANGES block unchanged...
else:
    # wholesale overwrite with/of a non-dict can also create or clobber "protected"
    if (isinstance(existing_value, dict) and "protected" in existing_value) or \
            (isinstance(field_value, dict) and "protected" in field_value):
        if not self._can_edit_protected_json(request):
            raise me.PermissionDeniedException("Permission denied: cannot modify protected metadata")
        username = getattr(getattr(request, "user", None), "username", "unknown")
        self.model_logit(request, f"{username} modified {field_name}.protected keys: * on pk={self.pk}", kind="meta:protected_changed")
    setattr(self, field_name, field_value)
```

Also update the function docstring to state that the protected guard applies
to both merge and replace. (Snippet is the intended shape, not sacred —
builder matches surrounding style; no type hints, per core rules.)

**2. `tests/test_account/test_group_protected_metadata.py` — new regression test module** (see Tests).

**3. Docs (see Docs):** `docs/django_developer/core/mojo_model.md`,
`docs/django_developer/account/group.md`, `CHANGELOG.md`.

No model/schema changes — no `bin/create_testproject` needed.

### Design decisions

- **Fail-closed on both directions.** A replace 403s when *either* the incoming
  dict carries `protected` *or* the existing value has one that would be
  clobbered — even if the incoming subtree is byte-identical to the existing
  one. This matches the merge branch, which already 403s on any incoming
  `protected` regardless of whether values change. Rejected alternative:
  silently carrying the existing `protected` subtree over on unprivileged
  replace — surprising magic, diverges from the merge branch's explicit-403
  contract, and hides the caller's intent.
- **Guard the replace path rather than banning `__replace` on protected
  fields.** The item offered "disallow `__replace` entirely" as an
  alternative; rejected because top-level replace of `metadata` is legitimate
  for privileged callers, and `incident.Ticket` relies on
  `JSON_REPLACE_FIELDS = ["metadata"]` for normal operation.
- **One hoisted check, no new helper.** The whole fix stays inside
  `on_rest_update_jsonfield` (plus a two-line copy in the outer else). No new
  `mojo/helpers/` utility, no `PROTECTED_JSON_ROOTS` abstraction — nothing
  else needs them (KISS).
- **Audit `changed_keys` on replace includes removed keys** (union of incoming
  and existing protected keys whose values differ); merge keeps today's
  incoming-only diff. Non-dict `protected` on either side logs `["*"]`,
  matching current behavior.

### Edge cases & risks

- **Nested geofence flow unaffected.** The documented
  `{"metadata": {"geofence": {..., "__replace": true}}}` pattern
  (docs/django_developer/account/geofence.md:94) puts `__replace` *inside* a
  sub-dict, handled by `objict.merge_dicts` on the merge branch — the
  top-level `should_replace` and this guard don't touch it.
- **Unprivileged replace of non-protected metadata still works.** When neither
  the incoming dict nor the existing value contains `protected`,
  `touches_protected` is False and behavior is unchanged — covered by a test.
- **Internal callers with `request=None`.** `_can_edit_protected_json(None)`
  is False, so a direct Python call replacing a protected-bearing field now
  raises where it previously (wrongly) succeeded. Already the documented
  contract for merges (mojo_model.md:592: pass a superuser/SYSTEM_REQUEST).
  No framework code calls `on_rest_update_jsonfield` outside REST dispatch.
- **`incident.Ticket` metadata** always replaces; the guard only bites if a
  ticket's metadata contains a `protected` key, which nothing in the
  framework writes. If a downstream app did, superuser-only is the correct
  fail-closed default.
- **`field_value.pop("__replace")` mutates before the check** — harmless:
  popping `__replace` can't add or remove a `protected` key.
- **Order matters: guard before `setattr`.** The permission failure must raise
  before any assignment so the in-memory instance is never dirtied.

### Tests

New module `tests/test_account/test_group_protected_metadata.py`, copying the
setup pattern of `tests/test_account/test_group_save_perms.py` (testit,
`from testit import helpers as th`, `@th.django_unit_setup()` /
`@th.django_unit_test()`, models imported inside functions, delete-before-create,
descriptive assert messages). Run with
`bin/run_tests --agent -t test_account.test_group_protected_metadata`.

Setup: a Group with
`metadata = {"motto": "original", "protected": {"payments": {"allowed_origins": ["https://real.example"]}}}`;
an ApiKey via `ApiKey.create_for_group(group=grp, name=..., permissions={"manage_group": True})`
(bearer pattern: `opts.client.bearer = "apikey"`, `opts.client.access_token = token`,
`opts.client.is_authenticated = True`); a member user holding `admin_verify`
for the positive case.

1. **The repro (regression):** manage_group-only ApiKey POSTs
   `/api/group/<pk>` with
   `{"metadata": {"__replace": true, "protected": {"payments": {"allowed_origins": ["https://evil.example"]}}}}`
   → 403; reload group, `metadata.protected` unchanged and `motto` intact.
2. **Clobber direction:** same key POSTs
   `{"metadata": {"__replace": true, "motto": "new"}}` (no `protected` key)
   → 403; existing `protected` subtree survives.
3. **Merge guard still intact:** same key POSTs
   `{"metadata": {"protected": {"x": 1}}}` (no `__replace`) → 403.
4. **Non-dict clobber:** same key POSTs `{"metadata": []}` (or a non-JSON
   string) → 403; metadata unchanged.
5. **Unprivileged replace without protected content succeeds:** a second
   group whose metadata has NO `protected` key; same-pattern key POSTs
   `{"metadata": {"__replace": true, "motto": "rewritten"}}` → 200 and
   metadata is replaced (proves no over-blocking).
6. **Privileged replace succeeds + audits:** user with `admin_verify`
   (member perm via `mm.add_permission("admin_verify")`, login flow as in
   test_group_save_perms.py) POSTs the repro body → 200, metadata replaced,
   and a `mojo.apps.logit` Log row exists with
   `kind="meta:protected_changed"` for this group created during the test
   (filter by kind + model/pk; delete matching rows in setup).

Per build-baseline rule: capture `bin/run_tests --agent` baseline before any
edit; regression test must fail on the unfixed code (tests 1, 2, 4 fail;
3 and 5 pass pre-fix) and all pass post-fix.

### Docs

- `docs/django_developer/core/mojo_model.md` — "Protected JSON Fields"
  section (:554-567): state the guard applies to merges, `__replace` /
  `JSON_REPLACE_FIELDS` replaces (including replaces that would drop an
  existing `protected` subtree), and non-dict overwrites; audit fires on all
  paths. Fix the example if it implies merge-only.
- `docs/django_developer/account/group.md` — correct the wrong
  `PROTECTED_JSON_PERMS = ["manage_groups"]` (:100-108 and :150) to the real
  `["admin_compliance", "admin_verify"]`; add one line that `__replace`
  cannot bypass the gate.
- `docs/web_developer/` — no existing PROTECTED_JSON_PERMS/`__replace`
  coverage; if a page documents group metadata updates, add a one-line
  403-on-protected note, otherwise skip.
- `CHANGELOG.md` — security-fix entry: protected-JSON guard now enforced on
  JSONField replace and non-dict overwrite paths.

### Open questions

None.

## Notes

- Baseline (2026-07-10, pre-edit, `bin/run_tests --agent`): total 2404,
  passed 2348, failed 0, skipped 56 — all green, no pre-existing failures.
- Post-fix (same suite): total 2410 (+6 new regression tests), passed 2354,
  failed 0 — green, no regressions.
- Small scope addition found during build: `_can_edit_protected_json` read
  `user.is_superuser` directly, but under apikey auth `request.user` is an
  ApiKey (no `is_superuser` attr) → the already-guarded MERGE path 500'd
  (`AttributeError`) instead of 403 for keys. Fixed with
  `getattr(user, "is_superuser", False)`; covered by the merge-path regression
  test (asserts 403, not 500).
- Regression tests confirmed failing pre-fix exactly as predicted: replace,
  clobber, non-dict = 200 (bypass); merge = 500; privileged replace = no
  audit row. All six pass post-fix.
- Post-build security review (agent) found one audit-fidelity regression in
  the first fix: a privileged MERGE of `{"protected": null}` (which wipes the
  subtree — merge_dicts drops null-valued keys) audited an EMPTY changed-keys
  list, where pre-fix code logged `["*"]`. Fixed: an explicit null on merge
  now unions in the existing keys like a replace does; 7th regression test
  asserts the audit names the removed keys. Permission gate itself was
  confirmed sound (no remaining bypass vectors; getattr is_superuser cannot
  widen access; no fail-open on None/anonymous request).
- Post-build docs sweep corrected a pre-existing geofence.md claim: nested
  `{"geofence": {..., "__replace": true}}` was documented but objict's
  merge_dicts has no nested-__replace handling (the key would persist
  literally). Doc now shows merge + null deletion, and the
  PROTECTED_JSON_PERMS interaction for top-level replaces.
- Final suite: total 2411, passed 2355, failed 0 — green.

## Resolution
- closed:
- branch:
- files changed:
- tests added: tests/test_account/test_group_protected_metadata.py — 7 tests
  (replace-with-protected 403, replace-clobbering-protected 403,
  merge-with-protected clean 403 for ApiKey, non-dict-overwrite 403,
  unprotected replace still 200, privileged replace 200 + audit row,
  privileged null-wipe merge audits removed key names)
