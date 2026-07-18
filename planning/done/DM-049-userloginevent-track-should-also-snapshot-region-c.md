---
id: DM-049
type: chore
title: UserLoginEvent.track() should also snapshot region_code (ISO 3166-2) alongside the region name
priority: P3
effort: XS
owner: backend
opened: 2026-07-16
depends_on: []
related: []
links: []
build_strategy: delegate
build_model: opus
---

# Snapshot region_code on UserLoginEvent

## What & Why
`UserLoginEvent` denormalizes geo from `GeoLocatedIP` at `track()`
(`mojo/apps/account/models/login_event.py:92-106`) but copies only the
subdivision NAME (`region="California"`) — `GeoLocatedIP.region_code`
(`US-CA`, `geolocated_ip.py:38`) is dropped. Downstream consumers that
compare login geo against code-based policy lists must maintain their own
name→code mapping (wmx_api's geolocation compliance report builder now
does exactly that — `apps/wmx/reports/services/builders/geolocation.py`
`_STATE_CODE_BY_NAME`, WMX-API-131).

Add a `region_code` column, populate it in `track()` from
`geo.region_code`, expose it in the RestMeta graphs. Historical rows stay
name-only (consumers keep their mapping for history); new rows become
directly joinable against ISO code lists.

## Acceptance Criteria
- [ ] `region_code` CharField (indexed, nullable) populated by `track()`.
- [ ] Included in `basic`/`list` graphs + CSV format if present.
- [ ] Migration + test (track() writes both name and code).

## Plan

**Build routing**: `build_strategy: delegate`, `build_model: opus` — rubric floor
is sonnet (XS chore, exact in-repo precedent), opus chosen by the user for the
first delegate-mode build.

### Goal
Copy `GeoLocatedIP.region_code` (ISO 3166-2, e.g. `"US-CA"`) onto `UserLoginEvent`
at `track()` time and expose it in the REST graphs (and thereby CSV export), so
downstream consumers can join login geo against code-based policy lists instead of
maintaining a name→code map (WMX-API-131).

### Context — what exists
- `mojo/apps/account/models/login_event.py`
  - Field block (13–42): `region = models.CharField(max_length=100, db_index=True,
    null=True, blank=True)` at line 23, between `country_code` (22) and `city` (24).
    No `region_code` today.
  - `RestMeta` (44–79): `SEARCH_FIELDS = ['ip_address', 'country_code', 'region',
    'city']` (49). `region` appears in all three `GRAPHS` field lists — `basic`
    (53), `list` (60), `default` (70). No `FORMATS` key.
  - `track()` (84–148): inits `region = None` (96); copies `region = geo.region`
    (103) when a `GeoLocatedIP` row exists for `request.ip`; passes `region=region`
    into `cls.objects.create(...)` (128). Metrics block (138–146) keys off the
    region NAME.
- `mojo/apps/account/models/geolocated_ip.py:34-38` — the source field (mirror it
  exactly):
  ```python
  region = models.CharField(max_length=100, db_index=True, null=True, blank=True)
  # ISO 3166-2 subdivision code, e.g. "US-FL". Populated lazily on refresh()
  # from providers that expose it (MaxMind, ip-api, ipstack). For geofence DSL
  # region matching.
  region_code = models.CharField(max_length=10, db_index=True, null=True, blank=True)
  ```
- CSV export needs no extra work once graphs are updated: `on_rest_list_response`
  (`mojo/models/rest.py:847-859`) checks `RestMeta.FORMATS` (absent here) then
  falls to `get_rest_meta_graph(["basic", "default"])` — first-match
  (`mojo/models/rest.py:98-107`) → `basic`'s `fields` list verbatim.
- Filtering needs no work: `on_rest_list_filter` (`mojo/models/rest.py:1005`)
  derives exact-match filters from real model fields — `?region_code=US-CA` works
  as soon as the column exists.
- Migrations: latest is `mojo/apps/account/migrations/0046_geolocatedip_whitelisted_until.py`.
  Exact precedent for this change: `0042_geolocatedip_region_code.py` — a single
  `AddField` of the identical field type on the sibling model.
- Production call site: `jwt_login()` (`mojo/apps/account/rest/user.py:665-666`)
  → `UserLoginEvent.track(...)`. No other code constructs rows.
- Tests: `tests/test_account/test_login_event.py` — `setup_login_event` (7–50)
  creates `opts.geo_ip` (28–36: `country_code="US", region="California"`, no
  `region_code`); `test_track_creates_event_with_geo` (53–72) asserts each
  denormalized field incl. `event.region == "California"` (66);
  `test_unknown_ip_creates_event_with_null_geo` (150–163) asserts
  `event.region is None` (161).

### Changes — what to do
1. `mojo/apps/account/models/login_event.py`
   - Add after line 23:
     ```python
     # ISO 3166-2 subdivision code, e.g. "US-CA" (see GeoLocatedIP.region_code)
     region_code = models.CharField(max_length=10, db_index=True, null=True, blank=True)
     ```
   - `track()`: add `region_code = None` beside line 96; `region_code =
     geo.region_code` beside line 103; `region_code=region_code,` in the
     `create(...)` call beside line 128.
   - `RestMeta.GRAPHS`: insert `'region_code'` immediately after `'region'` in all
     three field lists (`basic` 53, `list` 60, `default` 70).
2. Migration: run `bin/create_testproject` after the model edit (it runs
   makemigrations + migrate). Do NOT hand-author the file. Expected result:
   `0047_userloginevent_region_code.py` with one `AddField` mirroring 0042's
   shape; verify the generated file and dependency chain before committing.
3. `tests/test_account/test_login_event.py`
   - `opts.geo_ip` fixture (28–36): add `region_code="US-CA",`.
   - `test_track_creates_event_with_geo`: after the `region` assertion (66) add
     `assert event.region_code == "US-CA", f"Expected region_code US-CA, got {event.region_code}"`.
   - `test_unknown_ip_creates_event_with_null_geo`: after the `region is None`
     assertion (161) add
     `assert event.region_code is None, f"Expected None region_code, got {event.region_code}"`.
4. Docs (both tracks):
   - `docs/django_developer/account/login_events.md`: add a `region_code` row to
     the Fields table after `region` (`CharField(10)` — ISO 3166-2 subdivision
     code from GeoLocatedIP, nullable, indexed); add `region_code` to the `list`
     row of the Graphs table (the `default` row says "All list fields + …" and
     inherits).
   - `docs/web_developer/account/login_events.md`: add `"region_code": "US-CA",`
     after `"region"` in both JSON examples (`graph=list` and `graph=default`);
     add a `region_code` query-param row (exact-match filter, e.g. `US-CA`).
5. `CHANGELOG.md`: add a `**chore**` entry (DM-049) at the top of the current
   rolling block, matching the style of recent DM entries.

### Design decisions
- Field params mirror `GeoLocatedIP.region_code` exactly (`max_length=10,
  db_index=True, null=True, blank=True`) — proven precedent; max ISO 3166-2
  length is 6 chars so 10 is ample.
- Include in the `default` graph too (AC names only basic/list): `default`
  already carries `region`; omitting the code there would be an inconsistency in
  the fullest graph.
- No `FORMATS` block: CSV inherits `basic`'s fields (see Context), so the graph
  edit satisfies the CSV criterion for free.
- `SEARCH_FIELDS` untouched: the stated use case is exact-match joining, already
  covered by the generic `?region_code=` filter; `search=` is free-text and not
  the use case.
- No backfill/data migration — ticket explicitly keeps historical rows name-only.
- **Non-goal**: metrics slugs and `is_new_region` semantics stay keyed on the
  region NAME (renaming slugs would break downstream metric consumers).
- **Non-goal**: `GeoLocatedIP.geolocate()`'s subnet-fallback
  (`mojo/apps/account/models/geolocated_ip.py:781`) copies `region` but not
  `region_code` when creating a row from a subnet match — pre-existing gap in the
  SOURCE model; do not fix it here. Consequence: some new login events may
  legitimately have `region` set but `region_code=None`.
- **Non-goal**: aggregation endpoints (`mojo/apps/account/rest/login_event.py:49`
  — `logins/summary` / `logins/user`) keep drilling down by region NAME.

### Edge cases & risks
- No `GeoLocatedIP` row for the IP → `region_code` stays `None`, identical to
  `region`/`city`/lat/long today. No new code path.
- `geo.region_code is None` while `geo.region` is set (older cached rows,
  providers without subdivision codes, subnet-fallback rows) → event gets
  name-only. Expected; field is nullable.
- Migration number may differ from 0047 if something else lands first —
  `bin/create_testproject` resolves it; verify the generated dependency chain,
  don't hardcode.

### Tests
Run `bin/run_tests --agent -t test_account.test_login_event` (plus the default
suite baseline per `.claude/rules/build-baseline.md`).
- Extend `test_track_creates_event_with_geo`: `region_code == "US-CA"` alongside
  `region == "California"`.
- Extend `test_unknown_ip_creates_event_with_null_geo`: `region_code is None`
  alongside `region is None`.

### Docs
Both tracks as listed in Changes 4; CHANGELOG per Changes 5.

### Open questions
None.

## Notes

- Baseline (`bin/run_tests --agent`, default suite, before any edit): total 2537 /
  passed 2481 / failed 0 / skipped 56. All green — `failures: []`. Opt-in modules
  `test_incident` (243) and `test_security` (82) skipped (require `--extra slow`),
  out of scope for the default baseline. Any failure after this change is mine.

## Resolution
- closed: 2026-07-18
- branch: main
- files changed: .claude/agents/security-review.md,.claude/rules/git.md,.claude/skills/build/SKILL.md,.claude/skills/request/SKILL.md,.claude/skills/scope/SKILL.md,AI_DEV.md,CHANGELOG.md,CLAUDE.md,WORKFLOW.md,bin/create_testproject,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/geofence.md,docs/django_developer/account/group.md,docs/django_developer/account/login_events.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/mojo_model.md,docs/django_developer/core/permissions.md,docs/django_developer/core/rate_limiting.md,docs/django_developer/filevault/README.md,docs/django_developer/helpers/request.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/realtime/README.md,docs/django_developer/realtime/architecture.md,docs/django_developer/rest/permissions.md,docs/django_developer/security/README.md,docs/django_developer/security/abuse_hardening.md,docs/django_developer/security/maestro_board.md,docs/web_developer/README.md,docs/web_developer/account/api_keys.md,docs/web_developer/account/authentication.md,docs/web_developer/account/bouncer.md,docs/web_developer/account/custom_auth_models.md,docs/web_developer/account/geofence.md,docs/web_developer/account/group.md,docs/web_developer/account/login_events.md,docs/web_developer/account/user.md,docs/web_developer/account/user_self_management.md,docs/web_developer/core/aggregation.md,docs/web_developer/core/filtering.md,docs/web_developer/core/request_response.md,docs/web_developer/filevault/README.md,docs/web_developer/logging/reporting_events.md,docs/web_developer/realtime/websocket.md,docs/web_developer/security/README.md,docs/web_developer/security/maestro_board.md,docs/web_developer/security/rate_limits.md,memory.md,mojo/apps/account/migrations/0047_userloginevent_region_code.py,mojo/apps/account/models/api_key.py,mojo/apps/account/models/group.py,mojo/apps/account/models/login_event.py,mojo/apps/account/models/user.py,mojo/apps/account/rest/bouncer/assess.py,mojo/apps/account/rest/bouncer/event.py,mojo/apps/account/rest/bouncer/views.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/oauth.py,mojo/apps/account/rest/passkeys.py,mojo/apps/account/rest/sms.py,mojo/apps/account/rest/totp.py,mojo/apps/account/rest/user.py,mojo/apps/account/services/auth_config.py,mojo/apps/account/services/disable.py,mojo/apps/account/services/geofence/__init__.py,mojo/apps/account/services/geofence/enforcement.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/filevault/rest/data.py,mojo/apps/filevault/rest/file.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/handlers/event_handlers.py,mojo/apps/incident/migrations/0032_maestroboard_maestroboardlink.py,mojo/apps/incident/models/__init__.py,mojo/apps/incident/models/maestro_board.py,mojo/apps/incident/models/maestro_board_link.py,mojo/apps/incident/models/rule.py,mojo/apps/incident/models/ticket.py,mojo/apps/incident/rest/__init__.py,mojo/apps/incident/rest/event.py,mojo/apps/incident/rest/maestro_board.py,mojo/apps/incident/rest/maestro_webhook.py,mojo/apps/incident/services/__init__.py,mojo/apps/incident/services/maestro_sync.py,mojo/apps/realtime/asgi.py,mojo/apps/realtime/handler.py,mojo/decorators/auth.py,mojo/decorators/geofence.py,mojo/decorators/http.py,mojo/decorators/limits.py,mojo/helpers/crypto/vault.py,mojo/helpers/request.py,mojo/models/rest.py,mojo/models/rest_aggregation.py,planning/.config,planning/.next_id,planning/_template.md,planning/done/DM-001-render-allowlisted-extra-registration-fields-promo.md,planning/done/DM-002-step-up-recent-authentication-gate-for-sensitive-o.md,planning/done/DM-003-register-page-enter-on-phone-otp-field-fires-step-.md,planning/done/DM-004-sign-in-alternate-method-button-row-overflows-clip.md,planning/done/DM-005-phone-register-one-wrong-sms-code-burns-the-sessio.md,planning/done/DM-006-sms-sign-in-with-an-unrecognized-number-dead-ends-.md,planning/done/DM-007-full-test-suite-is-flaky-content-guard-false-posit.md,planning/done/DM-008-phone-signup-may-fail-to-sign-in-an-existing-accou.md,planning/done/DM-009-get-remote-ip-trusts-client-supplied-x-forwarded-f.md,planning/done/DM-010-websocket-ip-resolver-trusts-client-spoofable-sour.md,planning/done/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/done/DM-012-auth-middleware-500s-on-a-malformed-authorization-.md,planning/done/DM-013-management-command-to-create-initial-users-admins.md,planning/done/DM-014-var-dev-server-conf-overrides-config-dev-server-co.md,planning/done/DM-015-configurable-outbound-webhook-signature-header-use.md,planning/done/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,planning/done/DM-017-geofence-config-evidence-plane-editable-system-rul.md,planning/done/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/done/DM-019-self-minted-group-apikey-with-arbitrary-permission.md,planning/done/DM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/DM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/DM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/DM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/DM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/done/DM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/done/DM-026-github-oauth-login-on-the-bouncer-hosted-auth-page.md,planning/done/DM-027-group-rest-save-collapses-to-the-view-check-any-ac.md,planning/done/DM-028-post-api-group-member-invite-returns-a-raw-500-typ.md,planning/done/DM-029-add-explicit-auth-gates-to-the-permission-check-si.md,planning/done/DM-030-jsonfield-replace-bypasses-protected-json-perms-ma.md,planning/done/DM-031-geofence-test-override-mojo-test-mode-are-db-redis.md,planning/done/DM-032-rest-batch-save-skips-instance-level-permission-ch.md,planning/done/DM-033-fileman-initiated-uploads-can-t-be-completed-or-fk.md,planning/done/DM-034-oauth-login-drops-the-redirect-param-user-lands-on.md,planning/done/DM-035-field-action-level-permission-gates-omit-the-base-.md,planning/done/DM-036-apikey-set-permissions-silently-discards-non-dict-.md,planning/done/DM-037-apikey-validate-token-grants-group-context-without.md,planning/done/DM-038-rest-batch-save-ignores-can-update-can-create-flag.md,planning/done/DM-039-get-api-group-pk-member-resolves-touches-any-group.md,planning/done/DM-040-incident-maestroboard-push-link-tickets-into-a-rem.md,planning/done/DM-041-config-driven-item-id-prefixes-dm-canonical-workfl.md,planning/done/DM-042-authenticated-abuse-doom-loop-hardening-default-pe.md,planning/done/DM-043-enforce-login-flow-geofencing-after-credential-ver.md,planning/done/DM-044-auth-decorators-clobber-each-other-s-security-regi.md,planning/done/DM-045-harden-the-dm-037-identity-gates-enforce-the-inact.md,planning/done/DM-046-unguarded-self-active-user-is-superuser-in-user-py.md,planning/done/DM-047-filevault-endpoints-fetch-vaultfile-vaultdata-by-p.md,planning/done/DM-048-group-get-member-for-user-parent-walk-ignores-each.md,planning/done/DM-051-list-endpoint-stat-aggregation-batched-counts-for-.md,planning/future/group-member-deny-timing-side-channel.md,planning/in_progress/DM-049-userloginevent-track-should-also-snapshot-region-c.md,planning/inbox/apikey-group-context-ignores-group-is-active.md,planning/inbox/apikey-parent-key-inactive-descendant-one-way-door.md,planning/inbox/apikey-suspension-residual-surfaces.md,planning/inbox/batch-ignores-can-update-can-create-flags.md,planning/inbox/filevault-sharing-token-hardening.md,planning/inbox/group-me-member-endpoint-oracle-touch.md,planning/inbox/is-request-user-positive-marker.md,planning/inbox/list-filter-sensitive-field-count-oracle.md,planning/inbox/maestro-webhook-replay-timestamp.md,planning/inbox/member-perms-ignore-group-is-active.md,planning/inbox/phone-verify-dev-bypass-code-db-settable.md,planning/inbox/serializer-reverse-onetoone-graph-emits-empty-list.md,planning/inbox/test-scheduled-task-minute-59-boundary-flake.md,planning/inbox/test-security-full-suite-red.md,scripts/intake.sh,scripts/ready.sh,testit/client.py,tests/test_account/test_bouncer_limits.py,tests/test_account/test_disable_kill_switch.py,tests/test_account/test_geolocated_ip_aggregation.py,tests/test_account/test_group_me_member_oracle.py,tests/test_account/test_login_event.py,tests/test_assistant/28_test_fk_perm_check.py,tests/test_email/email_change.py,tests/test_filevault/3_test_rest_scoping.py,tests/test_geofence/config_plane.py,tests/test_geofence/post_auth.py,tests/test_geofence/registry.py,tests/test_global_perms/apikey_group_inactive.py,tests/test_limits/__init__.py,tests/test_limits/api_throttle.py,tests/test_limits/block_dedup.py,tests/test_limits/traffic_concentration.py,tests/test_maestro_board/__init__.py,tests/test_maestro_board/test_maestro_rest.py,tests/test_maestro_board/test_maestro_service.py,tests/test_middleware/group_param_is_active.py,tests/test_models/batch_feature_flags.py,tests/test_models/list_stats.py,tests/test_oauth/oauth.py,tests/test_realtime/connection_limits.py,tests/test_verification/verification.py,uv.lock
- tests added: Extended `tests/test_account/test_login_event.py` (no new test
  functions; existing coverage extended for the new field): `setup_login_event`
  fixture now seeds `region_code="US-CA"` on `opts.geo_ip`;
  `test_track_creates_event_with_geo` asserts `event.region_code == "US-CA"`
  alongside the region-name assertion; `test_unknown_ip_creates_event_with_null_geo`
  asserts `event.region_code is None`. Full default suite green post-change
  (2537 total / 2481 passed / 0 failed / 56 skipped), unchanged from baseline.
