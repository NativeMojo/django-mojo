---
# id is assigned by /scope on pickup — leave it blank
id: DM-048
type: bug
title: Deactivating a parent group must disable its entire subtree — dynamic effective-activeness (is_effectively_active) routed through every group gate
priority: P2
effort: M
owner: backend
opened: 2026-07-16
depends_on: []
related: [DM-039, DM-037, DM-025, DM-045]
links: []
---

# Deactivating a parent group must disable its entire subtree

## What & Why
Originally filed as: `Group.get_member_for_user(user, check_parents=True)`
(`mojo/apps/account/models/group.py:264-309`) walks up to 8 parent levels and, at
each level, filters the **membership row's** `is_active` but never checks the
**parent group's** `is_active` — so an active membership in a deactivated parent
still authorizes against an active child. **Verified by /scope 2026-07-17**: the
walk never consults `self.is_active` or `current.is_active`, and all 12 production
call sites pass `check_parents=True` (perm checks, realtime topics, assistant
tiers, member/api-key `can_change_permission`).

**Rescoped by user ruling (2026-07-17)** to the stricter, general contract: *if a
parent is disabled, all its children are disabled.* A group is **effectively
active** only if it AND every ancestor is active. Enforced **dynamically** (no
flag cascade — cascading writes create a reactivation one-way door, the same trap
that parked the api-key descendant item). Deactivating a parent instantly darkens
the subtree (memberships, API keys, group resolution); reactivating it instantly
restores individually-active children.

This extends DM-025 ("inactive == nonexistent"), DM-037 (deactivation instantly
suspends API keys — now for whole subtrees), and composes with DM-045's landed
structural gates. It **deliberately overturns** the documented per-group
carve-outs in `ApiKey.is_group_allowed` / `ApiKey.get_groups` ("an active child
under an inactive parent stays reachable") and the test asserting them.

## Acceptance Criteria
- [ ] New single owner of the contract: `Group.is_effectively_active(max_depth=8)`
      — False on the first inactive ancestor (or self), True on a clean chain.
- [ ] `Group.get_active`, the `get_member_for_user` walk, `validate_token`,
      `is_group_allowed`, `get_groups`, the model-security gates, the decorator
      gate, dispatcher `group_uuid`, registration, geofence, OAuth state, and the
      auth-domain cache read all route through effective activeness.
- [ ] A membership in a deactivated ancestor no longer authorizes anywhere; a
      **direct** membership in an active child of a deactivated ancestor no longer
      authorizes either (subtree rule).
- [ ] Fully-active-chain inheritance unchanged; `is_active=False` admin path in
      `get_member_for_user` unchanged.
- [ ] Reactivating the parent restores child access with no one-way door.
- [ ] Regression tests covering the contract (see Plan → Tests).

## Repro — bugs only
1. Active child group C under deactivated parent P; user U is an active member of P
   only. U calls `GET /api/group/<C.pk>/member`.
- Expected: denied — wire-identical to nonexistent/inactive, zero touch writes.
- Actual (verified): 200 with U's P-membership record; C and the membership
  `touch()`ed.

## Plan

### Goal
A group is *effectively active* only if it and every ancestor are active —
deactivating a parent instantly disables its whole subtree (memberships, API
keys, group resolution), dynamically, with no flag cascade and no reactivation
one-way door.

### Context — what exists
- `Group.get_active(pk)` — `mojo/apps/account/models/group.py:223-232` — own-flag
  only (`cls.objects.filter(pk=pk, is_active=True).first()`); DM-025's single
  owner of "inactive == nonexistent" (silent `None`, no oracle, no touch).
  Callers: dispatcher `?group=` (`mojo/decorators/http.py:86`), perm fallbacks
  (`mojo/decorators/auth.py:77,129`), member endpoint
  (`mojo/apps/account/rest/group.py:109`).
- `Group.get_member_for_user(user, check_parents=False, is_active=True,
  max_depth=8)` — `group.py:264-309`. Direct check then parent walk; the
  `is_active` param filters **membership rows only**
  (`current.members.filter(user=user, is_active=True)`); `self.is_active` /
  `current.is_active` never consulted. Each `current` Group object is already
  loaded in-memory during the walk. 12 production call sites, all
  `check_parents=True`: `user_has_permission` (`group.py:218`),
  `check_view_permission` (`group.py:589`), member endpoint
  (`rest/group.py:112`), `ApiKey.can_change_permission` (`api_key.py:161`),
  `GroupMember.can_change_permission` (`member.py:90`), `User.can_access_topic`
  (`user.py:1500` — resolves the group with **no** is_active filter at
  `user.py:1497`), assistant app ×6 (`assistant/handler.py:158`,
  `services/skills.py:57,74,447`, `services/memory.py:156,173` — handler resolves
  via `Group.objects.get(pk=...)`, no is_active).
- DM-037/DM-045 gates, all own-flag today:
  - `ApiKey.validate_token` — `api_key.py:326`:
    `request.group = api_key.group if api_key.group.is_active else None`
  - `ApiKey.is_group_allowed` — `api_key.py:191-206`: `if group is None or not
    group.is_active: return False`, then pk match or `group.is_child_of(self.group)`.
    Carries a comment that an active child under an inactive parent still passes —
    **to be overturned**.
  - `ApiKey.get_groups` — `api_key.py:234,238`: querysets filtered
    `is_active=True` per-group; docstring says active child stays reachable —
    **to be overturned**.
  - Model-security structural gates — `mojo/models/rest.py:281` (pre-hook
    instance-group gate) and `:340` (post-rebind gate), both
    `not <group>.is_active`, both `branch="api_key.group_inactive"` (DM-045).
  - Decorator gate — `mojo/decorators/auth.py:34`
    (`_deny_machine_identity_without_active_group`; defense-in-depth).
- Other own-flag resolutions: dispatcher `group_uuid` branch (`http.py:118`,
  `Group.objects.filter(uuid=group_uuid, is_active=True)`), registration
  (`mojo/apps/account/rest/user.py:314-316`, raises "Group is not active"),
  geofence pre-flight (`rest/geofence.py:75`, inactive → system rules only),
  OAuth `group_from_state` (`rest/oauth.py:95`, inactive → None),
  `Group.resolve_by_auth_domain` (`group.py:784,788`) backed by a 24h Redis cache
  (`group.py:746-768` invalidates only on the group's **own** save — an ancestor
  flip cannot purge it).
- Deactivation surfaces: REST save of `is_active`, `on_action_disable` /
  `on_action_reactivate` (`group.py:625-656`). `on_rest_saved`
  (`group.py:709-731`) only recomputes a global metric and the group's own
  auth-domain cache — **no cascade exists today**; the check must be dynamic.
- No `is_effectively_active`-like helper exists anywhere; no request-scoped
  memoization helper in `mojo/helpers/` (Redis via `mojo/helpers/redis/` is the
  only caching precedent).
- Group `RestMeta` — `group.py:46-122`: `LIST_DEFAULT_FILTERS = {"is_active":
  True}` (own flag, SQL-level). Custom list handler `on_rest_handle_list`
  (`group.py:802-851`); `User.get_groups` (`user.py:349-371`) expands children
  with per-group is_active only.
- DM-039's endpoint (`rest/group.py:102-122`): gates the child via
  `Group.get_active(pk)`, one `PermissionDeniedException` raise site,
  `touch()`es only after membership confirms.

### Changes — what to do
1. `mojo/apps/account/models/group.py` — add:
   ```python
   def is_effectively_active(self, max_depth=8):
       # a group counts as active only if it AND every ancestor is active;
       # depth-capped like get_member_for_user (also guards parent cycles)
       current = self
       depth = 0
       while current is not None and depth <= max_depth:
           if not current.is_active:
               return False
           current = current.parent
           depth += 1
       return True
   ```
   The sole definition of the contract — no site re-implements the walk.
2. `group.py` `get_active` — after own-flag resolution, return `None` unless
   `group.is_effectively_active()`. Fixes dispatcher `?group=`, both perm-fallback
   decorators, and the member endpoint in one move. Same silent-`None` shape (no
   new oracle).
3. `group.py` `get_member_for_user` — when `is_active=True`, gate at the top:
   `if not self.is_effectively_active(): return None` (before the direct-member
   query). One check covers self AND every parent level — if the chain from self
   to root is clean, every walked parent is active by definition — fixing all 12
   call sites including the ones that resolve the group with no is_active filter
   (realtime topics, assistant app) without touching those files.
   `is_active=False` keeps raw behavior (admin/introspection).
4. `mojo/apps/account/models/api_key.py` — `validate_token` (`:326`) and
   `is_group_allowed` (`:196`) → `is_effectively_active()`; `get_groups` →
   post-filter resolved ids through `is_effectively_active()` and return
   `Group.objects.filter(id__in=kept_ids)`. Rewrite the two obsolete
   "active child under an inactive parent stays reachable" comments/docstrings.
5. `mojo/models/rest.py:281,340` and `mojo/decorators/auth.py:34` →
   `is_effectively_active()`. Branch string `api_key.group_inactive` unchanged.
6. One-line swaps to the effective check: `mojo/decorators/http.py:118`
   (`group_uuid` branch — filter by uuid then verify effective),
   `mojo/apps/account/rest/user.py:314` (registration),
   `mojo/apps/account/rest/geofence.py:75`, `mojo/apps/account/rest/oauth.py:95`.
7. `group.py` `resolve_by_auth_domain` — verify `is_effectively_active()` on the
   cache-resolved group at read time (the 24h Redis entry can't see ancestor
   flips; per-read verification closes the hole without new invalidation
   plumbing).
8. Tests + docs per sections below. Regression tests first (bug type — they must
   fail on current main), then the fix.

### Design decisions
- **Dynamic check, no flag cascade** — user-ruled 2026-07-17. Cascading
  `is_active=False` writes down the subtree creates a reactivation one-way door
  (can't distinguish cascade-disabled from deliberately-disabled children — the
  same trap that parked `planning/future/apikey-parent-key-inactive-descendant-
  one-way-door.md`). Dynamic is instant in both directions.
- **One helper, routed everywhere** — `is_effectively_active` is the single
  owner; every gate delegates.
- **Lists stay own-flag in v1** (`LIST_DEFAULT_FILTERS`, `User.get_groups`,
  children listings): SQL can't cheaply walk ancestors. A child of a deactivated
  parent may still *appear* in lists to users who already hold perms, but every
  resolution/authorization gate denies. Disclosure-to-the-already-permitted, not
  an access grant; pruning lists is a possible follow-up item.
- **Depth cap 8**, matching `get_member_for_user(max_depth=8)`; deeper chains are
  already unsupported for membership. Also cycle protection. Cost ≤8 lazy
  `.parent` loads per check, typically 1-2; accepted, optimize later (CTE /
  caching) only if it shows up.
- **Deliberately overturns** `is_group_allowed`/`get_groups` per-group carve-outs
  and `test_apikey_active_child_still_reachable`
  (`tests/test_global_perms/apikey_group_inactive.py:170`) — contract change per
  user ruling, not a regression.
- `get_member_for_user` gates via one top-of-method check rather than a per-level
  `current.is_active` guard — equivalent under the subtree rule and simpler.

### Edge cases & risks
- API keys of every descendant group go dark when an ancestor is deactivated —
  DM-037 extended to subtrees; instant, reversible via reactivation.
- Registration / OAuth / geofence against a child of a deactivated org behave
  exactly as if the group were inactive (generic deny / system-rules-only) — same
  wire shapes, no new existence oracle.
- Chains deeper than the cap: levels past 8 are not verified (and memberships
  past 8 were never honored) — documented limitation, unchanged.
- Existing all-active-chain tests (`tests/test_auth/accounts.py:213-377`,
  `test_group_me_member_oracle.py`, `group_param_is_active.py`) must stay green.
- Per-request cost: a few extra small queries on group-scoped requests; accepted.

### Tests
testit (`docs/django_developer/testit/Overview.md`); hierarchy fixtures modeled on
`tests/test_auth/accounts.py` REST-built Org→Dept→Team; delete-first setup;
`last_activity=None` to defeat the 300s touch throttle (DM-039 pattern). Bug type
→ regressions must fail on current main before the fix.
- `tests/test_account/test_group_me_member_oracle.py` (extend DM-039 suite):
  1. Original repro: U active member of deactivated P only; active child C →
     `GET /api/group/<C.pk>/member` denied, wire-identical to the other deny
     paths, **zero** touch writes on C and the membership.
  2. Inactive middle: U member of active grandparent, middle parent deactivated →
     denied.
  3. Subtree rule: U **direct** active member of active C under deactivated P →
     denied.
  4. Fully active chain still resolves the ancestor membership (inheritance
     unchanged).
  5. Reactivate P → the denied lookups from (1)/(3) immediately succeed (no
     one-way door).
- `tests/test_middleware/group_param_is_active.py` (extend): `?group=<child-of-
  inactive-parent>` → `request.group is None`, no touch on the child.
- `tests/test_global_perms/apikey_group_inactive.py` (extend + flip): key bound
  to an active child of a deactivated parent → `validate_token` strips group /
  detail+list denied with `branch="api_key.group_inactive"`; **flip**
  `test_apikey_active_child_still_reachable` (`:170`) to assert denial;
  reactivation hierarchy case restores access.
- Direct-model: `C.get_member_for_user(u, check_parents=True)` → `None` under an
  inactive ancestor; same call with `is_active=False` still returns the row
  (admin path preserved).

### Docs
- `docs/django_developer/account/group.md` — Membership (`:196-232`) + hierarchy:
  define effective activeness, subtree deactivation, depth cap, dynamic (no
  cascade) semantics.
- API-key doc touched by DM-037: deactivating an ancestor suspends descendant
  groups' keys.
- `docs/web_developer/` — group deactivation semantics note if a group endpoints
  page exists.
- `CHANGELOG.md` — behavior change entry.

### Open questions
None — the three embedded calls (lists own-flag in v1, api-key test flip,
timing side-channel parked to `planning/future/`) were approved 2026-07-17.

## Notes
- Baseline (2026-07-17, pre-edit, `bin/run_tests --agent`): 2830 total / 2449
  passed / 0 failed / 381 skipped (opt-in + env-gated skips). All green — any
  post-change failure is DM-048's.
- Source: DM-039 post-build security-review (2026-07-16); verified + rescoped to
  the subtree contract by /scope with user ruling (2026-07-17).
- The minor DM-039-review timing side-channel (deny paths wire-identical but not
  equal-cost) is parked at
  `planning/future/group-member-deny-timing-side-channel.md`.
- Post-build agents (2026-07-18): test-runner full suite green (2456→2457
  passed, 0 failed); security-review — core contract clean, 1 WARNING
  (get_groups N+1 → fixed with batched subtree derivation + regression test)
  and 2 INFO consistency gaps (bouncer `?group_uuid=` fallback,
  auth_config.resolve_group_from_request → both routed through
  is_effectively_active; UX-only, authorization points already enforced);
  docs-updater swept 8 additional stale pages across both tracks.
- Commits: 4be73be9 (implementation + tests + primary docs), 0c16aa85
  (post-build hardening + docs sweep).

## Resolution
- closed: 2026-07-18
- branch: main
- files changed: .claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,bin/create_testproject,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/core/rate_limiting.md,docs/django_developer/helpers/request.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/README.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/security/abuse_hardening.md,docs/django_developer/security/maestro_board.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/authentication.md,docs/web_developer/account/bouncer.md,docs/web_developer/account/custom_auth_models.md,docs/web_developer/account/geofence.md,docs/web_developer/account/group.md,docs/web_developer/account/user.md,docs/web_developer/core/request_response.md,docs/web_developer/logging/reporting_events.md,docs/web_developer/realtime/websocket.md,docs/web_developer/security/README.md,docs/web_developer/security/maestro_board.md,docs/web_developer/security/rate_limits.md,memory.md,mojo/apps/account/models/api_key.py,mojo/apps/account/models/group.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/bouncer/assess.py,mojo/apps/account/rest/bouncer/event.py,mojo/apps/account/rest/bouncer/views.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/oauth.py,mojo/apps/account/rest/passkeys.py,mojo/apps/account/rest/sms.py,mojo/apps/account/rest/totp.py,mojo/apps/account/rest/user.py,mojo/apps/account/services/auth_config.py,mojo/apps/account/services/disable.py,mojo/apps/account/services/geofence/__init__.py,mojo/apps/account/services/geofence/enforcement.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/handlers/event_handlers.py,mojo/apps/incident/migrations/0032_maestroboard_maestroboardlink.py,mojo/apps/incident/models/__init__.py,mojo/apps/incident/models/maestro_board.py,mojo/apps/incident/models/maestro_board_link.py,mojo/apps/incident/models/rule.py,mojo/apps/incident/models/ticket.py,mojo/apps/incident/rest/__init__.py,mojo/apps/incident/rest/event.py,mojo/apps/incident/rest/maestro_board.py,mojo/apps/incident/rest/maestro_webhook.py,mojo/apps/incident/services/__init__.py,mojo/apps/incident/services/maestro_sync.py,mojo/apps/realtime/asgi.py,mojo/apps/realtime/handler.py,mojo/decorators/auth.py,mojo/decorators/geofence.py,mojo/decorators/http.py,mojo/decorators/limits.py,mojo/helpers/request.py,mojo/models/rest.py,planning/.config,planning/.next_id,planning/_template.md,planning/confirmed/DM-044-auth-decorators-clobber-each-other-s-security-regi.md,planning/confirmed/DM-047-filevault-endpoints-fetch-vaultfile-vaultdata-by-p.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/done/DM-042-authenticated-abuse-doom-loop-hardening-default-pe.md,planning/done/DM-043-enforce-login-flow-geofencing-after-credential-ver.md,planning/done/DM-045-harden-the-dm-037-identity-gates-enforce-the-inact.md,planning/done/DM-046-unguarded-self-active-user-is-superuser-in-user-py.md,planning/future/group-member-deny-timing-side-channel.md,planning/in_progress/DM-048-group-get-member-for-user-parent-walk-ignores-each.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/is-request-user-positive-marker.md,planning/inbox/login-event-snapshot-region-code.md,planning/inbox/maestro-webhook-replay-timestamp.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/serializer-reverse-onetoone-graph-emits-empty-list.md,planning/inbox/test-security-full-suite-red.md,scripts/intake.sh,scripts/ready.sh,testit/client.py,tests/test_account/test_bouncer_limits.py,tests/test_account/test_disable_kill_switch.py,tests/test_account/test_geolocated_ip_aggregation.py,tests/test_account/test_group_me_member_oracle.py,tests/test_assistant/28_test_fk_perm_check.py,tests/test_email/email_change.py,tests/test_geofence/post_auth.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_limits/__init__.py,tests/test_limits/api_throttle.py,tests/test_limits/block_dedup.py,tests/test_limits/traffic_concentration.py,tests/test_maestro_board/__init__.py,tests/test_maestro_board/test_maestro_rest.py,tests/test_maestro_board/test_maestro_service.py,tests/test_middleware/group_param_is_active.py,tests/test_models/batch_feature_flags.py,tests/test_oauth/oauth.py,tests/test_realtime/connection_limits.py,tests/test_verification/verification.py,uv.lock
- tests added: tests/test_account/test_group_me_member_oracle.py (+5 — parent
  membership denied on active child, grandparent denied across inactive middle,
  direct member of child denied under inactive parent, direct-model contract +
  is_active=False admin path, reactivation restores);
  tests/test_middleware/group_param_is_active.py (+1 — dispatcher group= child
  of inactive parent not resolved/not touched);
  tests/test_global_perms/apikey_group_inactive.py (+2, 1 flipped —
  active-child reachability flips to deny when parent deactivated, child key
  dark on ancestor deactivation + instant reactivation, get_groups subtree
  prune). 7 failed pre-fix, all green post-fix.
