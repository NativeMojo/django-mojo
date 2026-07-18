---
# id is assigned by /scope on pickup — leave it blank
id: DM-038
type: bug
title: REST batch save ignores CAN_UPDATE / CAN_CREATE flags (immutability bypass when CAN_BATCH is enabled)
priority: P3
effort: S
owner: backend
opened: 2026-07-10
depends_on: []
related: [DM-032]
links: []
---

# REST batch save ignores CAN_UPDATE / CAN_CREATE flags

## What & Why
`on_rest_handle_batch` (mojo/models/rest.py) enforces per-row *permission*
checks (DM-032) but never evaluates the per-verb feature flags: update rows
skip `CAN_UPDATE` (checked by `on_rest_handle_save`, rest.py ~:446-455) and
create rows skip `CAN_CREATE` (checked by `on_rest_handle_create`,
rest.py ~:584-585). A future model that sets `CAN_UPDATE = False` to make rows
immutable (e.g. an audit/ledger record) — or `CAN_CREATE = False` — and also
opts into `CAN_BATCH = True` would have that explicit control bypassed via the
batch endpoint, even though the single-instance verb is hard-disabled.

**Latent, not live:** no shipped model sets `CAN_BATCH = True`, so the gap
cannot be exercised today. It was deliberately scoped out of DM-032 (see
that item's "Design decisions" — `CAN_UPDATE`'s default/`CAN_SAVE`-alias
semantics make blanket per-row enforcement a behavior change) and flagged by
DM-032's security review as a real immutability bypass, not just a policy
inconsistency.

## Acceptance Criteria
- [ ] A model with `CAN_BATCH = True` and `CAN_UPDATE = False` refuses batch
      update rows (or refuses to enable batch at all — decide during scope).
- [ ] Same for `CAN_CREATE = False` and batch create rows.
- [ ] Decide the mechanism during scope: per-row flag enforcement in the batch
      loop (mind `CAN_UPDATE` default/`CAN_SAVE` alias semantics from
      `on_rest_handle_save`) vs. a guard that refuses `CAN_BATCH = True` on a
      model whose `CAN_UPDATE`/`CAN_CREATE` is False, so the combination can't
      be armed silently.
- [ ] Regression test on a runtime-`CAN_BATCH` model (in-process pattern from
      tests/test_models/batch_row_permissions.py).
- [ ] Existing batch and single-instance behavior otherwise unchanged; suite
      green.

## Repro — bugs only
1. On any model, set `RestMeta.CAN_BATCH = True` and `CAN_UPDATE = False`
   (no shipped model does this today — repro is via a test/dev model).
2. As a caller holding SAVE_PERMS, POST `{"batched": [{"id": <pk>, ...}]}` to
   the list endpoint.
- Expected: row refused — the single-instance `POST /api/<model>/<pk>` raises
  `feature_disabled`/`can_update_false`.
- Actual: row is updated — batch never reads `CAN_UPDATE`/`CAN_CREATE`.

## Plan

### Goal
Make `on_rest_handle_batch` honor the per-verb feature flags — update rows respect
`CAN_UPDATE` (with the `CAN_SAVE` deprecated alias), create rows respect
`CAN_CREATE` — so a `CAN_BATCH = True` model can't bypass a hard-disabled verb via
the batch endpoint.

### Context — what exists
- `mojo/models/rest.py:617` `on_rest_handle_batch` — gates once at class level
  (`CAN_BATCH`, then `["SAVE_PERMS", "VIEW_PERMS"]`), then loops rows. Update rows
  (pk resolves) get a per-row permission check (rest.py:666) then
  `instance.update_from_dict(item)`; create rows get a per-row permission check
  (rest.py:673) then `cls.create_from_dict(item, request=request)`. **Neither branch
  reads `CAN_UPDATE`/`CAN_CREATE`.** This is the bug.
- `mojo/models/rest.py:446-460` `on_rest_handle_save` — resolves `CAN_UPDATE`:
  ```python
  can_update = cls.get_rest_meta_prop("CAN_UPDATE", None)
  if can_update is None:
      can_save = cls.get_rest_meta_prop("CAN_SAVE", None)
      if can_save is not None:
          _warn_can_save_deprecated(cls.__name__)
          can_update = can_save
      else:
          can_update = True
  if not can_update:
      raise me.PermissionDeniedException(... event_type="feature_disabled",
                                          branch="can_update_false")
  ```
  Feature gate runs BEFORE the permission check (`rest_check_permission_or_raise`
  at :462).
- `mojo/models/rest.py:584-590` `on_rest_handle_create` — `if not
  cls.get_rest_meta_prop("CAN_CREATE", True): raise ... branch="can_create_false"`,
  also before its permission check (:592).
- `mojo/models/rest.py:1412-1435` `_report_batch_row_denied` — drop-with-audit
  helper (DM-032): emits a `batch_row_denied` incident via
  `class_report_incident_for_user`, wrapped in try/except so audit never blocks the
  flow. This is the template for the new feature-disabled helper.
- Batch loop drop-with-audit convention (rest.py:666-676): boolean check →
  `_report_...` → `errors.append({"index": idx, "error": ...})` → `continue`.
- Test pattern: `tests/test_models/batch_row_permissions.py` — runtime `CAN_BATCH`
  via `setattr(ChatRoom.RestMeta, "CAN_BATCH", True)` in a try/finally that
  `delattr`s it, and calls `ChatRoom.on_rest_handle_batch(req)` **in-process**
  (setattr does not cross into the testit server process). `_build_request` builds
  an `objict` request; `_response_data` asserts `status is True` and returns
  `body["data"]` (has `count` and optional `errors`).

### Changes — what to do
1. **`mojo/models/rest.py`** — extract the `CAN_UPDATE` resolution (rest.py:446-453)
   into a classmethod so the alias/default semantics live in one place:
   ```python
   @classmethod
   def _resolve_can_update(cls):
       can_update = cls.get_rest_meta_prop("CAN_UPDATE", None)
       if can_update is None:
           can_save = cls.get_rest_meta_prop("CAN_SAVE", None)
           if can_save is not None:
               _warn_can_save_deprecated(cls.__name__)
               can_update = can_save
           else:
               can_update = True
       return can_update
   ```
   Replace the inline block in `on_rest_handle_save` with
   `if not cls._resolve_can_update():`.
2. **`mojo/models/rest.py` `on_rest_handle_batch`** — before the row loop (after the
   `CAN_BATCH` gate + class-level permission check, ~rest.py:643), resolve both
   static flags once:
   ```python
   can_update = cls._resolve_can_update()
   can_create = cls.get_rest_meta_prop("CAN_CREATE", True)
   ```
3. **In the loop**, add the feature gate BEFORE each branch's permission check
   (mirror single-instance ordering):
   - update branch (`instance is not None`, before the :666 permission check):
     ```python
     if not can_update:
         cls._report_batch_row_feature_disabled(request, instance, idx,
                                                 branch="batch_can_update_false")
         errors.append({"index": idx, "error": "UPDATE not allowed"})
         continue
     ```
   - create branch (before the :673 permission check):
     ```python
     if not can_create:
         cls._report_batch_row_feature_disabled(request, None, idx,
                                                 branch="batch_can_create_false")
         errors.append({"index": idx, "error": "CREATE not allowed"})
         continue
     ```
4. **`mojo/models/rest.py`** — add `_report_batch_row_feature_disabled` next to
   `_report_batch_row_denied` (rest.py:1412), identical shape but
   `event_type="feature_disabled"`, `details=f"Batch row feature-disabled: index
   {index} on {cls.__name__}"`, same metadata (`branch`, `index`, `instance_id`,
   `model_name`, `request_path`), wrapped in try/except.
5. **`tests/test_models/batch_feature_flags.py`** (new) — see Tests.
6. **Docs** — `docs/django_developer/rest/permissions.md` "Batch Save Permissions"
   (~:333) + `CHANGELOG.md`.

### Design decisions
- **Per-row drop-with-audit, NOT refuse-CAN_BATCH-entirely.** The flags are
  class-level/static, but refusing batch whenever either flag is False would kill
  the legitimate append-only-ledger case (`CAN_CREATE=True, CAN_UPDATE=False`
  wanting batch *creates*). Per-row honors each verb independently and matches
  DM-032's established drop-with-audit convention (a mixed batch on a
  `CAN_UPDATE=False` model still applies its creates, drops its updates). No
  non-atomic partial-application surprise.
- **Flags resolved once before the loop** — they don't vary per row/caller; avoids
  per-row overhead and repeated alias-resolution.
- **Feature gate before permission gate**, per row — exact mirror of
  `on_rest_handle_save`/`on_rest_handle_create` ordering.
- **Shared `_resolve_can_update`** so the `CAN_SAVE` alias + default-True semantics
  can't drift between the single and batch paths.
- **Specific error strings** ("UPDATE not allowed" / "CREATE not allowed") are safe:
  the flag is class-level and returns identically for every caller/row, so there's
  no per-tenant enumeration signal (unlike the generic "permission denied" used for
  the permission drops).
- **Distinct incident** `event_type="feature_disabled"` (not `batch_row_denied`) so
  operators see the same category the single-instance path emits; branch names
  (`batch_can_update_false`/`batch_can_create_false`) distinguish the batch origin.

### Edge cases & risks
- **Mixed create+update batch** on a single-flag-disabled model: each row gated by
  its own verb — the other verb's rows proceed. Covered by tests.
- **`CAN_SAVE` alias parity**: `CAN_SAVE = False` (no `CAN_UPDATE`) must block batch
  update rows identically — guaranteed by shared `_resolve_can_update`, asserted by
  a test.
- **Audit failure never blocks** — helper wraps `class_report_incident_for_user` in
  try/except, like `_report_batch_row_denied`.
- **`request.group` reset** between rows (rest.py:653-656) is untouched — the new
  gate `continue`s before touching the request, so no tenant leak.

### Tests
`tests/test_models/batch_feature_flags.py` — in-process, mirror
`batch_row_permissions.py` (`_build_request`, try/finally setattr/delattr of the
flags on `ChatRoom.RestMeta`). Use own-tenant rows (user_a / room_a) so permission
passes and ONLY the feature flag can block. Restore any mutated row name in
`finally`.
- **`CAN_UPDATE=False` mixed batch**: `[{"id": room_a.pk, "name": "x"}, {"name":
  new}]` → room_a name unchanged in DB, create row succeeds; `count == 1`, one error
  entry at index 0. Fails pre-fix (row was written).
- **`CAN_CREATE=False` mixed batch**: `[{"id": room_a.pk, "name": ok}, {"name":
  new}]` → create dropped, update applied; `count == 1`, one error at index 1;
  `ChatRoom.objects.filter(name=new)` is None.
- **`CAN_SAVE=False` alias**: setattr `CAN_SAVE=False` (no `CAN_UPDATE`), batch
  update row on room_a → dropped, name unchanged; asserts alias parity.
Run: `bin/run_tests --agent -t test_models.batch_feature_flags`. Baseline first
(`bin/run_tests --agent`, read `var/test_failures.json`).

### Docs
- `docs/django_developer/rest/permissions.md` — "Batch Save Permissions" (~:333):
  note batch also enforces `CAN_UPDATE`/`CAN_CREATE` per row (drop-with-audit,
  `feature_disabled` category, branches `batch_can_update_false`/
  `batch_can_create_false`); a create-only ledger (`CAN_UPDATE=False`) can still
  batch its creates.
- `docs/web_developer/` — batch is not enabled on any shipped model, so caller-facing
  behavior is unchanged; add a note only if a batch section already exists (check
  during build).
- `CHANGELOG.md` — bug entry (DM-038).

### Open questions
None.

## Notes
- **Baseline (2026-07-16, pre-edit)** — `bin/run_tests --agent`, authoritative
  `var/test_failures.json`: total 2448, passed 2392, failed 0, skipped 56. GREEN.
  (The terminal table's `test_incident`/`test_security` rows are `--full`-only
  opt-in modules, excluded from the default suite — not failures.)
- Filed from DM-032's security review (post-build agent, 2026-07-10),
  which rated it INFO/latent. Sibling of the per-row permission fix.
- The same review noted (no change required) that batch concentrates the
  pre-existing create-vs-denied pk-enumeration signal into one request when
  `CREATE_PERMS` is broad — worth remembering if batch is ever enabled on a
  high-value tenant boundary.

## Resolution
- closed: 2026-07-16
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/group.md,docs/web_developer/core/request_response.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/rest/group.py,mojo/decorators/auth.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/confirmed/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/in_progress/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-identity-gate-hardening.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/filevault-unfiltered-pk-cross-tenant-access.md,planning/inbox/get-member-for-user-parent-walk-ignores-parent-is-active.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/test-security-full-suite-red.md,planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md,scripts/intake.sh,scripts/ready.sh,tests/test_account/test_group_me_member_oracle.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_models/batch_feature_flags.py,uv.lock
- tests added: tests/test_models/batch_feature_flags.py (3 — CAN_UPDATE=False drops
  update rows / applies creates; CAN_CREATE=False drops create rows / applies
  updates; CAN_SAVE=False alias parity). All fail pre-fix, pass post-fix.
