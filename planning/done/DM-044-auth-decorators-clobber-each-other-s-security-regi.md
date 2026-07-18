---
# id is assigned by /scope on pickup — leave it blank
id: DM-044
type: bug
title: Auth decorators clobber each other's SECURITY_REGISTRY entries — enforced_endpoints under-reports
priority: P2
effort: S
owner: backend
opened: 2026-07-17
depends_on: []
related: [DM-043]
links: []
---

# Auth decorators clobber each other's SECURITY_REGISTRY entries — enforced_endpoints under-reports

## What & Why

Ten decorators in `mojo/decorators/auth.py` register endpoints with a **full
overwrite** — `SECURITY_REGISTRY[key] = {...}` — at lines 22
(`requires_perms`), 85 (`requires_group_perms`), 158 (`requires_global_perms`),
198 (`public_endpoint`), 223 (`custom_security`), 249 (`uses_model_security`),
276 (`token_secured`), 296 (`requires_auth`), 325 (`requires_fresh_auth`),
352 (`requires_bearer`). Only `mojo/decorators/geofence.py::_apply_geofence`
uses the merge pattern (`entry = SECURITY_REGISTRY.get(key, {})` + update).

Decorators apply bottom-up, so whenever one of the ten sits **above**
`@requires_geofence` in a stack (applied later), it wipes the `geofence`
sub-entry. Consequence: `GET /api/geo/rules` → `enforced_endpoints` — sold in
the docs as the compliance artifact for WHERE geofencing is enforced — has
been silently missing most geofenced endpoints (register, forgot,
magic/email sends, sms, totp, passkeys, oauth, handoff, magic/reset/verify/
invite completes...). Roughly only `on_user_login` (and other stacks with no
overwriting decorator above the geofence line) survive.

**Enforcement itself is unaffected** — the wrapper (pre-view) and the DM-043
post-credential checks run regardless of registry state. This is an
audit/visibility bug only, but a real one: the compliance surface lies.

Presumably the same clobbering also loses `type: public` / perms info when
multiple registering decorators stack in other orders — the fix should make
ALL registrations merge, not just geofence's.

## Acceptance Criteria

- [x] All ten registration sites in `mojo/decorators/auth.py` merge into the
      existing entry instead of overwriting (preserve the `geofence` sub-entry
      and each other's keys).
- [x] `GET /api/geo/rules` → `enforced_endpoints` lists **every**
      `@requires_geofence` endpoint (pre-view and `after_auth`), regardless of
      decorator stacking order.
- [x] Regression test: a view decorated `public_endpoint`-above-
      `requires_geofence` (the on_register shape) appears in
      `_enforced_endpoints()`.
- [x] `tests/test_geofence/config_plane.py` upgraded from `len > 0` to
      asserting known members (e.g. on_register present).
- [x] No behavior change to actual enforcement, auth, or perms checks — this
      is registry bookkeeping only.

## Repro

1. In a Django shell (or in-process test): import
   `mojo.apps.account.rest.user`, then call
   `mojo.apps.account.rest.geofence._enforced_endpoints()`.
- Expected: includes `...rest.user.on_register` (decorated
  `@md.requires_geofence(scope="auth")` at `user.py:263`).
- Actual: absent — `@md.public_endpoint()` sits above the geofence decorator
  and its registration at `auth.py:198` overwrote the entry.

## Investigation

- Root cause: **confirmed** (found during DM-043's
  `test_registry_annotates_after_auth`, which had to fall back to probe views;
  see the comment in `tests/test_geofence/post_auth.py`).
- Code path: `mojo/decorators/geofence.py::_apply_geofence` (merge, correct)
  vs the ten overwrite sites listed above.
- Fix shape: replace each `SECURITY_REGISTRY[key] = {...}` with the same
  get-merge-update pattern geofence uses. Watch for entries that
  intentionally replace (none apparent — each writes disjoint keys like
  `type`/`requires_auth`/`perms`).
- Regression-test feasibility: easy, in-process (define stacked probe views,
  assert registry contents).

## Plan

### Goal
Make all ten `SECURITY_REGISTRY` registration sites in `mojo/decorators/auth.py`
merge into the existing entry instead of overwriting it, so
`enforced_endpoints` (and any other registry-derived audit surface) stops
silently dropping sub-entries written by other stacked decorators.

### Context — what exists
- `SECURITY_REGISTRY = {}` at `mojo/decorators/auth.py:9` (module-global dict).
  `mojo/decorators/geofence.py:31` imports the **same object**
  (`from .auth import SECURITY_REGISTRY`) — not a copy.
- **Key formula is identical at all 11 sites**:
  `key = f"{func.__module__}.{func.__name__}"`. Every intermediate wrapping
  decorator in the stack uses `functools.wraps` (verified: `bouncer.py:42`,
  `limits.py:179,263`, `geofence.py:72`, `http.py:138`, `validate.py:6`, and the
  wrapping auth.py decorators), so `__name__`/`__module__` propagate and merge
  targets align regardless of stack order.
- **The one correct site** — `mojo/decorators/geofence.py:58-65`
  (`_apply_geofence`) — already does get-merge-write:
  ```python
  key = f"{func.__module__}.{func.__name__}"
  entry = SECURITY_REGISTRY.get(key, {})
  ...
  entry["geofence"] = gf_entry
  SECURITY_REGISTRY[key] = entry
  ```
- **The ten overwrite sites** in `mojo/decorators/auth.py`, each doing
  `SECURITY_REGISTRY[key] = {...}`:

  | Decorator | Line | Keys written |
  |---|---|---|
  | `requires_perms` | 22 | `type='permissions'`, `permissions`, `function`, `requires_auth=True` |
  | `requires_group_perms` | 85 | `type='permissions'`, `permissions`, `function`, `requires_auth=True` |
  | `requires_global_perms` | 158 | `type='permissions'`, `permissions`, `function`, `requires_auth=True`, `global_only=True` |
  | `public_endpoint` | 198 | `type='public'`, `reason`, `function`, `requires_auth=False` |
  | `custom_security` | 223 | `type='custom'`, `description`, `function`, `requires_auth=True` |
  | `uses_model_security` | 249 | `type='model'`, `model_class`, `model_name`, `function`, `requires_auth=True` |
  | `token_secured` | 276 | `type='token'`, `token_types`, `description`, `function`, `requires_auth=False` |
  | `requires_auth` | 296 | `type='authentication'`, `function`, `requires_auth=True` |
  | `requires_fresh_auth` | 325 | `type='fresh_auth'`, `seconds`, `function`, `requires_auth=True` |
  | `requires_bearer` | 352 | `type='bearer_token'`, `bearer_token`, `function`, `requires_auth=False` |

- Decorators apply bottom-up, so an auth decorator **above**
  `@requires_geofence` registers **later** and wipes the `geofence` sub-entry.
- **Live victim**: `on_register` at `mojo/apps/account/rest/user.py:259-264`:
  ```python
  @md.POST("auth/register")
  @md.public_endpoint()
  @md.strict_rate_limit("register", ip_limit=5, ip_window=300)
  @md.requires_bouncer_token('registration')
  @md.requires_geofence(scope="auth")
  def on_register(request):
  ```
  `public_endpoint` runs last → `geofence` lost → `on_register` absent from
  `_enforced_endpoints()`. Same pattern hits most geofenced auth endpoints;
  roughly only `on_user_login` (no overwriting decorator above the geofence
  line) survives today.
- **Consumers of SECURITY_REGISTRY** (all read defensively via `.get()`, so an
  additive merge breaks none of them):
  - `mojo/apps/account/rest/geofence.py:149-163` `_enforced_endpoints()` —
    iterates items, reads `entry.get("geofence")` → `gf.get("scope")` /
    `gf.get("after_auth")`. The primary victim.
  - `tests/test_security/test_routes.py:123-125,233-234,285-286,372-375,1014` —
    reads `type` (one direct-index at ~286 is a pre-existing condition for
    geofence-only entries; the merge fix only makes `type` present more often).
  - `tests/test_global_perms/model_permissions.py:31-37` — asserts
    `entry.get('global_only') is True`; preserved by merge.
- **Existing tests to touch**:
  - `tests/test_geofence/config_plane.py:110` —
    `assert len(d.enforced_endpoints) > 0, ...` (weak; passes only via
    `on_user_login`).
  - `tests/test_geofence/post_auth.py:212-250`
    `test_registry_annotates_after_auth` — probe-view fallback with a comment at
    lines 216-221 documenting this exact bug ("several auth decorators ...
    OVERWRITE the shared SECURITY_REGISTRY entry ... a pre-existing bug (filed
    separately)").
- No real code stacks two auth.py decorators on one view — the overwrite-vs-merge
  difference matters in practice only for the auth-decorator-over-geofence combo.

### Changes — what to do
1. **`mojo/decorators/auth.py`** — add a small module-level helper below the
   `SECURITY_REGISTRY` definition:
   ```python
   def _register_security(func, **info):
       key = f"{func.__module__}.{func.__name__}"
       entry = SECURITY_REGISTRY.get(key, {})
       entry.update(info)
       SECURITY_REGISTRY[key] = entry
   ```
   Replace all ten `SECURITY_REGISTRY[key] = {...}` assignments (lines above)
   with calls to it, passing the exact same keys/values each site writes today.
   One helper instead of ten inline copies — single point of truth for the merge
   invariant; inline repeats are exactly how the bug happened.
2. **`tests/test_geofence/`** — regression test (in-process): define a probe
   view stacked `@md.public_endpoint()` **above**
   `@md.requires_geofence(scope="auth")` (the on_register shape) and assert:
   - the entry's `geofence` sub-entry survives (`scope == "auth"`), AND
   - `type == "public"` / `requires_auth is False` survive too (both directions
     of the merge), AND
   - the probe's key appears in
     `mojo.apps.account.rest.geofence._enforced_endpoints()`.
   Also assert the real `mojo.apps.account.rest.user.on_register` key appears in
   `_enforced_endpoints()` (import `mojo.apps.account.rest.user` explicitly in
   the test — in-process assertions can't rely on the server's URL loading).
   Fails on current code (geofence sub-entry wiped), passes with the fix.
3. **`tests/test_geofence/config_plane.py:110`** — upgrade `len > 0` to
   membership assertions on the `GET /api/geo/rules` response: keys ending in
   `.on_register` and `.on_user_login` present in `enforced_endpoints`.
4. **`tests/test_geofence/post_auth.py:216-221`** — update the now-stale bug
   comment (bug fixed as DM-044; probes remain as deterministic fixtures).
5. **`CHANGELOG.md`** — entry for the audit-surface fix.

### Design decisions
- **Shared helper vs. ten inline merges**: helper — future decorators can't
  reintroduce the overwrite. Rejected: repeating the get/update/write triplet
  ten times.
- **`entry.update()` last-wins for overlapping scalar keys** (`type`,
  `requires_auth`, `function`): intentionally preserves today's semantics when
  two auth decorators stack (outermost/last-applied wins) while additively
  keeping disjoint keys like `geofence`. No deep-merge machinery — KISS.
- **No change to `geofence.py`**: its merge is already correct; replacing the
  `geofence` sub-dict wholesale on re-decoration is the right behavior (latest
  args win).

### Edge cases & risks
- Overlapping keys between stacked auth decorators → last-wins, identical to
  current behavior; no real code does this anyway.
- `function` key stores whatever `func` that decorator received (inner vs.
  wrapped) — cosmetic, unchanged by the fix.
- Entries with `geofence` but no `type` (geofence-only stacks) already exist
  today; the direct `['type']` index in `test_routes.py` is a pre-existing
  condition the fix only improves.
- Import order: in-process tests must import the endpoint module
  (`mojo.apps.account.rest.user`) before asserting registry membership.
- No behavior change to enforcement/auth/perms — wrappers are untouched; this is
  registry bookkeeping only.

### Tests
- New regression (testit, `@th.django_unit_test()`, in `tests/test_geofence/`):
  stacked probe keeps both `geofence` and `public` info; probe and real
  `on_register` appear in `_enforced_endpoints()`.
- `config_plane.py` membership upgrade (via `GET /api/geo/rules`).
- Baseline first per `.claude/rules/build-baseline.md`, then full default suite
  green after.

### Docs
- `docs/django_developer/account/geofence.md` — optional one-line note that
  `enforced_endpoints` is complete regardless of decorator stacking order.
- `CHANGELOG.md` — behavior-visible fix (audit surface now complete).
- No `docs/web_developer/` changes — response shape unchanged.

### Open questions
None.

## Notes
- Baseline (2026-07-18, `bin/run_tests --agent`): 2513 total / 2457 passed /
  0 failed / 56 skipped — all green. No pre-existing failures.

## Resolution
- closed: 2026-07-18
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,bin/create_testproject,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/core/rate_limiting.md,docs/django_developer/helpers/request.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/README.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/security/abuse_hardening.md,docs/django_developer/security/maestro_board.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/authentication.md,docs/web_developer/account/bouncer.md,docs/web_developer/account/custom_auth_models.md,docs/web_developer/account/geofence.md,docs/web_developer/account/group.md,docs/web_developer/account/user.md,docs/web_developer/core/request_response.md,docs/web_developer/logging/reporting_events.md,docs/web_developer/realtime/websocket.md,docs/web_developer/security/README.md,docs/web_developer/security/maestro_board.md,docs/web_developer/security/rate_limits.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/group.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/bouncer/assess.py,mojo/apps/account/rest/bouncer/event.py,mojo/apps/account/rest/bouncer/views.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/oauth.py,mojo/apps/account/rest/passkeys.py,mojo/apps/account/rest/sms.py,mojo/apps/account/rest/totp.py,mojo/apps/account/rest/user.py,mojo/apps/account/services/auth_config.py,mojo/apps/account/services/disable.py,mojo/apps/account/services/geofence/__init__.py,mojo/apps/account/services/geofence/enforcement.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/handlers/event_handlers.py,mojo/apps/incident/migrations/0032_maestroboard_maestroboardlink.py,mojo/apps/incident/models/__init__.py,mojo/apps/incident/models/maestro_board.py,mojo/apps/incident/models/maestro_board_link.py,mojo/apps/incident/models/rule.py,mojo/apps/incident/models/ticket.py,mojo/apps/incident/rest/__init__.py,mojo/apps/incident/rest/event.py,mojo/apps/incident/rest/maestro_board.py,mojo/apps/incident/rest/maestro_webhook.py,mojo/apps/incident/services/__init__.py,mojo/apps/incident/services/maestro_sync.py,mojo/apps/realtime/asgi.py,mojo/apps/realtime/handler.py,mojo/decorators/auth.py,mojo/decorators/geofence.py,mojo/decorators/http.py,mojo/decorators/limits.py,mojo/helpers/request.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/confirmed/DM-047-filevault-endpoints-fetch-vaultfile-vaultdata-by-p.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/done/DM-042-authenticated-abuse-doom-loop-hardening-default-pe.md,planning/done/DM-043-enforce-login-flow-geofencing-after-credential-ver.md,planning/done/DM-045-harden-the-dm-037-identity-gates-enforce-the-inact.md,planning/done/DM-046-unguarded-self-active-user-is-superuser-in-user-py.md,planning/done/DM-048-group-get-member-for-user-parent-walk-ignores-each.md,planning/future/group-member-deny-timing-side-channel.md,planning/in_progress/DM-044-auth-decorators-clobber-each-other-s-security-regi.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/is-request-user-positive-marker.md,planning/inbox/login-event-snapshot-region-code.md,planning/inbox/maestro-webhook-replay-timestamp.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/serializer-reverse-onetoone-graph-emits-empty-list.md,planning/inbox/test-security-full-suite-red.md,scripts/intake.sh,scripts/ready.sh,testit/client.py,tests/test_account/test_bouncer_limits.py,tests/test_account/test_disable_kill_switch.py,tests/test_account/test_geolocated_ip_aggregation.py,tests/test_account/test_group_me_member_oracle.py,tests/test_assistant/28_test_fk_perm_check.py,tests/test_email/email_change.py,tests/test_geofence/config_plane.py,tests/test_geofence/post_auth.py,tests/test_geofence/registry.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_limits/__init__.py,tests/test_limits/api_throttle.py,tests/test_limits/block_dedup.py,tests/test_limits/traffic_concentration.py,tests/test_maestro_board/__init__.py,tests/test_maestro_board/test_maestro_rest.py,tests/test_maestro_board/test_maestro_service.py,tests/test_middleware/group_param_is_active.py,tests/test_models/batch_feature_flags.py,tests/test_oauth/oauth.py,tests/test_realtime/connection_limits.py,tests/test_verification/verification.py,uv.lock
- tests added: tests/test_geofence/registry.py (test_stacked_decorators_merge_registry,
  test_on_register_in_enforced_endpoints — both failed pre-fix); config_plane.py
  test_rules_post_and_get upgraded to known-member assertions (on_register, on_user_login)
