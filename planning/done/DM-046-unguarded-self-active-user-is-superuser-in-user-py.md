---
# id is assigned by /scope on pickup — leave it blank
id: DM-046
type: bug
title: Unguarded self.active_user.is_superuser in user.py crashes (500) on a non-User identity
priority: P3
effort: XS
owner: backend
opened: 2026-07-08
depends_on: []
related: [DM-019, DM-018]
links: []
---

# Unguarded self.active_user.is_superuser in user.py crashes on a non-User identity

## What & Why

Defense-in-depth hardening flagged by DM-019's post-build security review
(2026-07-08). `request.user` / `self.active_user` can be a bare **`ApiKey`**
(under `Authorization: apikey <token>`), which has **no `is_superuser`
attribute**. Several `User` methods do `self.active_user.is_superuser` (or
`request.user.is_superuser`) **unguarded**, so a non-User identity reaching them
raises `AttributeError` → 500 instead of a clean permission decision. Sites the
review named (verify line numbers, they drift): `user.py` ~877
(`_handle_existing_user_pre_save`), ~825, ~462, ~467, ~573, ~601, ~651.

DM-019 already fixed the analogous crash in
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

**Goal**: Verify whether this is still a live bug; close without a code change
if it has already been resolved by other means.

**Verification (2026-07-17)**: Resolved as moot, by a fix independent of this
item. Commit `5639d6fd` (2026-07-10, after this item was filed) added a real
`is_superuser` property directly to `ApiKey`
(`mojo/apps/account/models/api_key.py:93-95`):

```python
@property
def is_superuser(self):
    return False
```

The commit message cites this exact bug. All seven named call sites in
`user.py` (now at lines 463, 468, 576, 604, 654, 828, 880) still do a bare
`.is_superuser` read — none were rewritten to `getattr(..., "is_superuser",
False)` as the AC literally asked for — but none can raise `AttributeError`
anymore, since `ApiKey.is_superuser` now answers the question directly (`False`,
same as a real non-superuser `User`). The write path
(`_handle_existing_user_pre_save` / `on_rest_pre_save`) remains additionally
gated by `User.check_edit_permission`, which still denies `ApiKey` identities
before reaching those lines at all.

**Decision**: no `getattr()` rewrite needed. `getattr` would only matter for a
future identity type that omits `is_superuser` entirely — a thinner, unfiled
risk, not what this item described. Closing without code changes.

**Optional AC item** (`APIKEY_PERMS_PROTECTION` default): left unaddressed —
out of scope for this closure, re-file separately if still wanted.

## Notes

- Origin: DM-019 post-build security review (2026-07-08). The review rated the
  two override bypasses (`User.check_edit_permission` cross-tenant read via
  `/api/user/<pk>`; `Group` SAVE by a zero-perm key) CRITICAL/WARNING — both
  fixed in DM-019. These `is_superuser` accesses were the INFO/robustness tail.
- Canonical idiom already in the codebase: `getattr(user, "is_superuser",
  False)` (DM-019, `memory.py`) and `hasattr(user, "is_request_user")` for the
  "is this a real request User?" test.

## Resolution
- closed: 2026-07-17
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,bin/create_testproject,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/core/rate_limiting.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/README.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/security/abuse_hardening.md,docs/django_developer/security/maestro_board.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/authentication.md,docs/web_developer/account/bouncer.md,docs/web_developer/account/group.md,docs/web_developer/account/user.md,docs/web_developer/core/request_response.md,docs/web_developer/logging/reporting_events.md,docs/web_developer/realtime/websocket.md,docs/web_developer/security/README.md,docs/web_developer/security/maestro_board.md,docs/web_developer/security/rate_limits.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/bouncer/assess.py,mojo/apps/account/rest/bouncer/event.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/oauth.py,mojo/apps/account/rest/passkeys.py,mojo/apps/account/rest/sms.py,mojo/apps/account/rest/totp.py,mojo/apps/account/rest/user.py,mojo/apps/account/services/disable.py,mojo/apps/account/services/geofence/__init__.py,mojo/apps/account/services/geofence/enforcement.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/handlers/event_handlers.py,mojo/apps/incident/migrations/0032_maestroboard_maestroboardlink.py,mojo/apps/incident/models/__init__.py,mojo/apps/incident/models/maestro_board.py,mojo/apps/incident/models/maestro_board_link.py,mojo/apps/incident/models/rule.py,mojo/apps/incident/models/ticket.py,mojo/apps/incident/rest/__init__.py,mojo/apps/incident/rest/event.py,mojo/apps/incident/rest/maestro_board.py,mojo/apps/incident/rest/maestro_webhook.py,mojo/apps/incident/services/__init__.py,mojo/apps/incident/services/maestro_sync.py,mojo/apps/realtime/asgi.py,mojo/apps/realtime/handler.py,mojo/decorators/auth.py,mojo/decorators/geofence.py,mojo/decorators/http.py,mojo/decorators/limits.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/done/DM-042-authenticated-abuse-doom-loop-hardening-default-pe.md,planning/done/DM-043-enforce-login-flow-geofencing-after-credential-ver.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-identity-gate-hardening.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/filevault-unfiltered-pk-cross-tenant-access.md,planning/inbox/get-member-for-user-parent-walk-ignores-parent-is-active.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/login-event-snapshot-region-code.md,planning/inbox/maestro-webhook-replay-timestamp.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/security-registry-decorator-clobbering.md,planning/inbox/serializer-reverse-onetoone-graph-emits-empty-list.md,planning/inbox/test-security-full-suite-red.md,planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md,scripts/intake.sh,scripts/ready.sh,testit/client.py,tests/test_account/test_bouncer_limits.py,tests/test_account/test_disable_kill_switch.py,tests/test_account/test_geolocated_ip_aggregation.py,tests/test_account/test_group_me_member_oracle.py,tests/test_assistant/28_test_fk_perm_check.py,tests/test_email/email_change.py,tests/test_geofence/post_auth.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_limits/__init__.py,tests/test_limits/api_throttle.py,tests/test_limits/block_dedup.py,tests/test_limits/traffic_concentration.py,tests/test_maestro_board/__init__.py,tests/test_maestro_board/test_maestro_rest.py,tests/test_maestro_board/test_maestro_service.py,tests/test_models/batch_feature_flags.py,tests/test_oauth/oauth.py,tests/test_realtime/connection_limits.py,tests/test_verification/verification.py,uv.lock
- tests added:
