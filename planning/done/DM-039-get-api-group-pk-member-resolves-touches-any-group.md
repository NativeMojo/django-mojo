---
id: DM-039
type: bug
title: GET /api/group/<pk>/member resolves + touches ANY group for any authenticated user (existence oracle, inactive touch)
priority: P3
effort: S
owner: backend
opened: 2026-07-10
depends_on: []
related: [DM-025]
links: []
---

# GET /api/group/<pk>/member resolves + touches ANY group for any authenticated user (existence oracle, inactive touch)

## What & Why
`on_group_me_member` (`mojo/apps/account/rest/group.py:92-100`) does
`request.group = Group.objects.filter(pk=pk).last()` — no `is_active` filter —
and unconditionally `.touch()`es the result. It is gated only by
`@md.requires_auth()`: ANY authenticated user (member or not) can probe an
arbitrary pk and distinguish nonexistent (403) from existing-active-or-inactive
(200 with real perms or the `{"id": -1, "permissions": []}` non-member shape),
and can perturb a deactivated group's `last_activity`/`modified` from a
non-member request. This is the same oracle-plus-touch pattern DM-025 closed
in the dispatcher, now a visible outlier from the framework-wide
"inactive == nonexistent" contract that commit establishes. Flagged as a
deferred follow-up in DM-025's plan and confirmed by its post-build
security review (2026-07-10).

## Acceptance Criteria
- [ ] Decide the contract: switch to `Group.get_active(pk)` (inactive == nonexistent here too), OR document why this endpoint deliberately resolves inactive groups (e.g. member self-service pre-reactivation) — and in that case at minimum stop `touch()`ing inactive groups and remove the nonexistent/exists distinction for non-members.
- [ ] A non-member authenticated user cannot distinguish nonexistent vs existing-but-unrelated group pks via this endpoint.
- [ ] No write side effect (`last_activity`/`modified`) on inactive groups from this endpoint.
- [ ] Legitimate member self-lookup on ACTIVE groups unchanged.
- [ ] Regression test covering the chosen contract.

## Repro — bugs only
1. As any authenticated user (no membership anywhere): `GET /api/group/<pk>/member` for (a) a nonexistent pk, (b) an existing inactive group's pk.
- Expected: indistinguishable responses; no write to the inactive group.
- Actual: (a) 403 vs (b) 200 `{"id": -1, ...}`, and (b) bumps the inactive group's `last_activity`/`modified`.

## Investigation
Confidence: **high** (DM-025 recon + its post-build security review both
read the handler; re-verify exact response shapes during /scope — the 403 vs
200 split and the `{"id": -1}` non-member payload). `Group.get_active` (added
by DM-025, `mojo/apps/account/models/group.py`) is the ready-made drop-in if
the active-only contract is chosen. Regression-test feasibility: high —
inactive-group + touch-assertion fixtures exist in
`tests/test_middleware/group_param_is_active.py`.

## Plan

### Goal
Make `GET /api/group/<pk>/member` fail closed with a single indistinguishable 403 for
every caller who is not an active member of an active group — eliminating the
existence oracle and the write side effect on inactive groups — while leaving member
self-lookup on active groups unchanged.

### Decided contract (user-approved 2026-07-12)
**Uniform 403.** Nonexistent pk, inactive group pk, and active-group-where-caller-is-
not-a-member all return the identical `PermissionDeniedException` response (same
reason, code, wire shape). Only an active member of an ACTIVE group gets a 200 with
their member record. The documented non-member sentinel
`{"id": -1, "permissions": []}` is **removed** — chosen over keeping it because 403
is the conventional deny that developers read correctly without knowing a magic-`-1`
convention, and it matches the post-DM-025 framework norm (deny by default, one
indistinguishable refusal). This is a client-visible change (see Docs + downstream
note).

### Context — what exists
- Handler `on_group_me_member` — `mojo/apps/account/rest/group.py:102-118` (current):
  ```python
  @md.GET('group/<int:pk>/member')
  @md.requires_auth()
  def on_group_me_member(request, pk=None):
      request.group = Group.objects.filter(pk=pk).last()
      if request.group is None:
          raise merrors.PermissionDeniedException(
              reason="GET permission denied: Group",
              model_name="Group",
              branch="group_member_endpoint_unknown_group",
              event_type="user_permission_denied",
          )
      request.group.touch()
      member = request.group.get_member_for_user(request.user, check_parents=True)
      if member is None:
          return {"status": True, "data": {"id": -1, "permissions": [] }}
      member.touch()
      return member.on_rest_get(request)
  ```
  Bugs: line 105 resolves ANY group (no `is_active` filter); line 113 touches it
  unconditionally (write to inactive groups from non-member requests); the
  403-vs-200 split (lines 106-112 vs 116) is the existence oracle.
- `Group.get_active(pk)` — `mojo/apps/account/models/group.py:223-232` (added by
  DM-025): `cls.objects.filter(pk=pk, is_active=True).first()`. Inactive resolves
  exactly like nonexistent — silent `None`, no touch, no distinct error.
- `Group.touch()` — `mojo/apps/account/models/group.py:234-240`: sets
  `last_activity` + full `atomic_save()` (no update_fields, so `modified` bumps
  too). Throttled by `GROUP_LAST_ACTIVITY_FREQ` (300s) but ALWAYS writes when
  `last_activity is None` — test fixtures exploit this.
- `Group.get_member_for_user(user, check_parents=True)` —
  `mojo/apps/account/models/group.py:264-309`: returns the active membership row
  (direct or up the parent chain, max_depth=8), `None` for non-members and non-User
  identities (ApiKey guard at 282-283).
- `GroupMember.touch()` — `mojo/apps/account/models/member.py:149-162`
  (`update_fields=['last_activity']`, throttled).
- Error rendering: `PermissionDeniedException` (`mojo/errors.py:48-86`, defaults
  code=403/status=403) is rendered by the dispatcher at
  `mojo/decorators/http.py:148-173` as
  `{"error": <reason>, "code": 403, "status": false}` with HTTP 403 (or 200 if the
  `_status_200_on_error()` setting is on). `branch`/`model_name`/`event_type` feed
  only the incident event, NOT the wire response — so one raise site with one reason
  guarantees indistinguishable responses.
- Fail-closed idiom to match: `on_group_invite_member`
  (`mojo/apps/account/rest/group.py:36-63`, SECURITY comment at 41-52) and
  `on_group_webhook_secret` (66-99).
- Reusable test patterns: `tests/test_middleware/group_param_is_active.py` —
  setup (37-82) deletes-before-creates one active + one inactive group both with
  `last_activity=None` (defeats the 300s throttle), member user with GroupMember
  grants; regression (85-107) snapshots `modified` before, refetches after, asserts
  `last_activity is None` and `modified` unchanged; `_login` helper (30-34).
- No existing test hits `group/<pk>/member`.

### Changes — what to do
1. **`mojo/apps/account/rest/group.py`** — rewrite `on_group_me_member` (102-118):
   ```python
   @md.GET('group/<int:pk>/member')
   @md.requires_auth()
   def on_group_me_member(request, pk=None):
       # SECURITY: active-only resolution (inactive == nonexistent, DM-025 contract)
       # and ONE raise site for every non-member outcome — nonexistent, inactive, and
       # not-a-member must be wire-indistinguishable (no existence oracle). No touch
       # until membership is confirmed (no write side effect from probes).
       request.group = Group.get_active(pk)
       member = None
       if request.group is not None:
           member = request.group.get_member_for_user(request.user, check_parents=True)
       if member is None:
           raise merrors.PermissionDeniedException(
               reason="GET permission denied: GroupMember",
               model_name="GroupMember",
               branch="group_member_endpoint_denied",
               event_type="user_permission_denied",
           )
       request.group.touch()
       member.touch()
       return member.on_rest_get(request)
   ```
   Why: `get_active` closes inactive resolution; the single raise makes all deny
   cases byte-identical; touches move below the membership check so probes cause
   zero writes; member path (touch + `on_rest_get`) unchanged.
2. **`tests/test_account/test_group_me_member_oracle.py`** — new regression module
   (see Tests).
3. **`docs/web_developer/account/group.md`** — "Get My Membership" section
   (103-120): delete the `{"id": -1, "permissions": []}` sentinel line (120);
   document that non-members, unknown ids, and inactive groups all receive the
   standard 403 permission-denied response, indistinguishable by design; align
   wording with the `request.group` active-only note at 265-267.
4. **`docs/django_developer/account/group.md`** — brief note alongside the existing
   `request.group` contract (~265) that `group/<pk>/member` follows the same
   active-only / fail-closed contract.
5. **`CHANGELOG.md`** — behavior change entry: `GET /api/group/<pk>/member` now
   returns 403 (instead of `{"id": -1, "permissions": []}`) when the caller is not
   an active member of an active group; no more touch on unresolved/inactive groups.

### Design decisions
- **Uniform 403 over uniform sentinel** — user-approved. Sentinel would have avoided
  any client change, but 403 is the conventional deny and self-explanatory;
  rejected keeping the 403/200 split (the oracle itself) and rejected
  `get_active`-only minimal fix (leaves active-group existence probeable,
  failing AC "cannot distinguish nonexistent vs existing-but-unrelated").
- **One raise site, generic reason** — `branch` metadata is incident-only, so a
  single exception guarantees identical wire responses; distinct branches per cause
  were rejected as unnecessary (incident forensics don't need the split and
  splitting risks future divergence).
- **Touch only after membership confirmed** — a member hitting their own group is
  legitimate activity; a probe is not. Group touch of active groups by the
  dispatcher (http.py:79-81) is a separate, already-decided contract and is not
  changed here.
- **`request.group` still set before the raise** — harmless (request dies), keeps
  the incident emitter able to see the group if it resolves.

### Edge cases & risks
- **web-mojo / maestro breakage**: the sentinel was documented
  (`docs/web_developer/account/group.md:120`); if web-mojo branches on `id === -1`
  it must instead treat 403 from this endpoint as "not a member". Downstream
  consumers only pick this up on publish — note it in the release/CHANGELOG so the
  web-mojo check happens before the next maestro pin bump.
- **`_status_200_on_error()` deployments**: wire status may be 200 with
  `code: 403` in body — still uniform across all deny cases, so no oracle;
  tests must assert on body (`status: false` / `code: 403`), not only HTTP status.
- **Member of an INACTIVE group** now gets 403 (previously 200 with their record).
  Intentional: inactive == nonexistent applies to members too, matching the
  dispatcher contract from DM-025.
- **Incident volume**: every non-member lookup now emits a `user_permission_denied`
  event where the sentinel emitted none. If web-mojo calls this endpoint
  speculatively on group pages this could be noisy — acceptable, and visible via the
  `group_member_endpoint_denied` branch if it needs tuning later.
- **ApiKey identities**: `get_member_for_user` returns None for non-User identities
  → they fall into the uniform 403, which is correct fail-closed behavior.

### Tests
New `tests/test_account/test_group_me_member_oracle.py` (testit — read
`docs/django_developer/testit/Overview.md` first; model fixtures on
`tests/test_middleware/group_param_is_active.py:37-107`). Setup: delete-then-create
an active group, an inactive group (both `last_activity=None` to defeat the touch
throttle), a member user (GroupMember in the active group AND a membership row in
the inactive group), and a non-member user. Scenarios:
1. **Uniform deny (the regression)**: non-member GETs `group/<pk>/member` for
   (a) nonexistent pk, (b) inactive group pk, (c) active group pk — all three
   responses identical (same status code, same body: `status: false`,
   `code: 403`, same `error` text). Fails on current code: (a) 403 vs (b)/(c) 200.
2. **No probe writes**: after (b) and (c), refetch both groups — `last_activity`
   still None, `modified` unchanged from pre-request snapshot. Fails on current
   code for both (touch fired).
3. **Member happy path unchanged**: member GETs the active group →
   200, `data.id > 0`, `permissions` present; group + member `last_activity` now
   set (legitimate touch preserved).
4. **Member of inactive group**: member GETs the inactive group → same uniform 403
   as scenario 1; inactive group still untouched.
Every assert carries a descriptive message; run via
`bin/run_tests --agent -t test_account.test_group_me_member_oracle`.

### Docs
- `docs/web_developer/account/group.md` — rewrite "Get My Membership"
  (103-120): remove sentinel, document uniform 403; primary consumer-facing change.
- `docs/django_developer/account/group.md` — one-line contract note near the
  `request.group` section (~265).
- `CHANGELOG.md` — breaking-behavior entry (sentinel → 403) with the web-mojo
  migration note.

### Open questions
None — contract decided (uniform 403, user-approved 2026-07-12). The web-mojo
`id === -1` audit is a downstream release-time task, not a build blocker.

## Notes
- The endpoint's purpose is "current user's membership in group" — check what web-mojo actually calls it for before changing the non-member response shape.
- Build baseline (2026-07-16, `bin/run_tests --agent`): **green** — total 2444, passed 2388, failed 0, skipped 56. No pre-existing failures; every post-change failure is attributable to this build.

## Resolution
- closed: 2026-07-16
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/permissions.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/group.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/rest/group.py,mojo/decorators/auth.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/confirmed/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/confirmed/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/in_progress/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-identity-gate-hardening.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/test-security-full-suite-red.md,planning/inbox/user-is-superuser-unguarded-on-non-user-identity.md,scripts/intake.sh,scripts/ready.sh,tests/test_account/test_group_me_member_oracle.py,tests/test_global_perms/apikey_group_inactive.py,uv.lock
- tests added: tests/test_account/test_group_me_member_oracle.py (4 tests — non-member deny indistinguishable, no probe writes, member self-lookup unchanged, member-of-inactive denied; 3/4 failed pre-fix, all green post-fix)
