# Standardize User/Group Disable Lifecycle

**Type**: request
**Status**: resolved
**Date**: 2026-05-09
**Priority**: medium

## Description

Today `User.is_active` and `Group.is_active` are flipped by three independent code paths — admin REST writes, the `inactive_sweep` job, and `pii_anonymize()` — with no shared shape for *why*, *when*, *by whom*, or *with what note*. Audit context lives in incident events and `logit.Log`, but nothing is queryable from the user/group record itself. A small set of related flags (`disable_warned`, `disable_warn_date`, `no_disable`) is already scattered at the top level of `metadata["protected"]`.

Consolidate all of this into a single `metadata.protected.disable.*` namespace on both `User` and `Group`, expose it through new `disable` / `reactivate` `POST_SAVE_ACTIONS`, refactor `inactive_sweep` to write the new shape, and add a Redis-backed throttle-read REST endpoint for support tooling. No new DB columns — the JSONField is already indexed and queryable, and this kind of audit-shaped state belongs there.

## Context

Conversation with @ian on 2026-05-09 walked through the three disable paths and confirmed that:

- **No new columns.** `metadata.protected` is already write-protected (`PROTECTED_JSON_PERMS` / superuser only, audited via `meta:protected_changed`) and visible in graphs. Promoting fields to columns is schema churn for no operational gain at expected scale.
- **JSONField queries are sufficient.** `metadata__protected__disable__reason="admin"` works in Postgres. The columns-vs-JSON tradeoff was explicitly weighed; column promotion is reserved for hot-path filters at >100k user scale (not the case today).
- **`metadata.protected` is NOT secret** — only write-locked. Use `MojoSecrets` (`set_secret`/`get_secret`) when something must be hidden from REST graphs. Disable state is intentionally visible.
- **Existing pattern matches `GeoLocatedIP`** — `POST_SAVE_ACTIONS = ["refresh", "threat_analysis", "block", "unblock", "whitelist", "unwhitelist"]` is the established shape for "named per-instance lifecycle ops"; we mirror it for users/groups.
- **`GroupMember.is_active` does NOT cascade** from `User.is_active` and that is intentional. This request does not change that. (The audit step in §"What changes" verifies no permission-check site silently relies on member.is_active without also gating on user.is_active.)

## Acceptance Criteria

- New `metadata.protected.disable` namespace is documented and used by every code path that flips `User.is_active` or `Group.is_active`.
- `POST /api/user/<id>` body `{"disable": {"reason": "...", "note": "..."}}` flips `is_active=False` and writes the disable block atomically. `{"reactivate": {"note": "..."}}` reverses it and appends a `history` entry. Both require `manage_users`.
- Same shape on Group: `POST /api/group/<id>` with `{"disable": ...}` / `{"reactivate": ...}`. Permission: `manage_groups` (matches existing `SAVE_PERMS`).
- `inactive_sweep` writes the new namespace (`disable.warning.sent_at`, `disable.warning.days_until_disable_at_send`, `disable.reason="inactive"`, `disable.exempt_from_auto_disable`) and reads BOTH legacy and new keys for one release (`disable_warned`, `disable_warn_date`, `no_disable` continue to be honored on read).
- `pii_anonymize()` sets `disable.reason="anonymized"` + `disable.at` before flipping `is_active`.
- New REST: `GET /api/auth/throttle?user_id=N&key=login` returns current Redis state for support tooling: `{count, limit, window, retry_after_seconds}`. Requires `manage_users`. Also accepts `username=...` resolution like `clear_rate_limit` does.
- `disable.history` is capped at the last 20 entries (oldest dropped, FIFO).
- `User.metadata` and `Group.metadata` graphs already include `metadata` in `default`/`list` graphs, so no graph changes needed — but verify `disable` shows up in the response and document the new shape.
- A one-time data migration walks existing users/groups with legacy keys and rewrites them under the new namespace. Idempotent — safe to re-run.
- Tests cover: disable/reactivate happy path, double-disable rejected, reactivate of never-disabled rejected, history cap at 20, exempt flag honored by sweep, throttle read endpoint returns sane data when no counter exists, back-compat — sweep still warns/disables a user with only the legacy `disable_warned` key set.
- Docs updated in both tracks. `docs/django_developer/account/inactive_sweep.md` rewritten to reference new namespace; new doc page `docs/django_developer/account/disable_lifecycle.md` describes the schema, helpers, and integration points; `docs/web_developer/account/user.md` and `group.md` document the disable/reactivate POST_SAVE_ACTIONS and the throttle-read endpoint. `CHANGELOG.md` entry.

## Investigation

### What exists

- `User.metadata` / `Group.metadata` JSONField with `set_protected_metadata` / `get_protected_metadata` helpers
- `_can_edit_protected_json` gate at [mojo/models/rest.py:1453](mojo/models/rest.py:1453) — superuser or `RestMeta.PROTECTED_JSON_PERMS`. Audit log at [rest.py:1441](mojo/models/rest.py:1441) (`kind="meta:protected_changed"`)
- `POST_SAVE_ACTIONS` pattern across `account` app — closest analog is [GeoLocatedIP](mojo/apps/account/models/geolocated_ip.py:102) with named `block`/`unblock` actions
- `inactive_sweep` at [mojo/apps/account/asyncjobs.py:15](mojo/apps/account/asyncjobs.py:15) → service in [mojo/apps/account/services/inactive.py](mojo/apps/account/services/inactive.py); legacy keys `disable_warned`, `disable_warn_date`, `no_disable` written at top of `metadata.protected`
- `pii_anonymize()` at [mojo/apps/account/models/user.py:787](mojo/apps/account/models/user.py:787) — sets `is_active=False` directly with no audit-shape metadata
- `MANAGE_USERS_ONLY_FIELDS` guard at [user.py:53](mojo/apps/account/models/user.py:53) — `is_active` is already gated to `manage_users` writers
- Throttle internals: `check_account_attempt` at [mojo/decorators/limits.py:298](mojo/decorators/limits.py:298), Redis key shape `srl:{key}:account:{account_id}`, sliding-window helpers `_check_sliding` / `_retry_after_sliding`
- `clear_rate_limit` REST endpoint at [mojo/apps/account/rest/user.py:31](mojo/apps/account/rest/user.py:31) — same permission/lookup model the new read endpoint should mirror
- Existing doc at [docs/django_developer/account/inactive_sweep.md](docs/django_developer/account/inactive_sweep.md) already documents legacy protected-metadata keys — needs rewrite

### What changes

| File | Change |
|---|---|
| `mojo/apps/account/models/user.py` | Add `POST_SAVE_ACTIONS = ['send_invite', 'disable', 'reactivate']`. Add `on_action_disable(value)` and `on_action_reactivate(value)` methods. `pii_anonymize()` writes `disable.reason="anonymized"` + `disable.at` before flipping flag. Add `is_disabled` (or `disable_state`) read-only property/extra returning the namespace dict. |
| `mojo/apps/account/models/group.py` | Add `disable` / `reactivate` to `POST_SAVE_ACTIONS`, mirror `on_action_*` methods. Add `PROTECTED_JSON_PERMS = ["manage_groups"]` if not already covering — verify. |
| `mojo/apps/account/services/disable.py` | NEW. `disable_entity(entity, reason, by_user, note=None)` and `reactivate_entity(entity, by_user, note=None)` — single source of truth that both REST actions and the sweep call into. Caps `disable.history` at 20 entries. Emits incident event. |
| `mojo/apps/account/services/inactive.py` | Refactor `warn_inactive_users/groups` to write `disable.warning.*`. `disable_inactive_users/groups` calls `disable_entity(reason="inactive", by_user=None)`. `_clear_stale_warnings` reads new shape AND legacy shape for one release. |
| `mojo/apps/account/rest/user.py` | NEW endpoint `@md.GET('auth/throttle')` with `manage_users`. Returns `{count, limit, window, retry_after_seconds}` for the resolved `(key, account_id)` from Redis. Accepts `user_id` or `username`. |
| `mojo/decorators/limits.py` | Add `read_account_attempt(key, account_id)` helper that returns `(count, limit, window, retry_after)` without incrementing. Symmetric with `check_account_attempt`. |
| `mojo/apps/account/migrations/00XX_disable_lifecycle_data.py` | Data migration: walk users/groups with `metadata.protected.disable_warned` / `no_disable` and rewrite under `disable.*`. Leave legacy keys in place for one release; remove in a follow-up. Idempotent. |
| `docs/django_developer/account/disable_lifecycle.md` | NEW — full schema, helpers, sweep integration, history-cap behavior, back-compat note. |
| `docs/django_developer/account/inactive_sweep.md` | Rewrite "Protected Metadata Keys" section to point at `disable_lifecycle.md` for the canonical schema. |
| `docs/web_developer/account/user.md` | Document `disable` / `reactivate` POST_SAVE_ACTIONS, the throttle-read endpoint, the new graph fields. |
| `docs/web_developer/account/group.md` | Document `disable` / `reactivate` POST_SAVE_ACTIONS. |
| `CHANGELOG.md` | Entry under next release. |
| `tests/test_account/disable_lifecycle.py` | NEW test file — full matrix. |

### Constraints

- **Back-compat for one release.** Read both legacy keys (`disable_warned`, `no_disable`) AND new namespace (`disable.warning.*`, `disable.exempt_from_auto_disable`) in the sweep. The data migration prefers writing new shape but leaves legacy keys in place. A follow-up release removes legacy reads + a second migration deletes legacy keys.
- **Permission write-protection** is already enforced by `_can_edit_protected_json`. The new `on_action_*` methods bypass that gate intentionally because they're already gated by the model's `SAVE_PERMS` (`manage_users` / `manage_groups`) — but the audit log at `rest.py:1441` only fires on the JSON merge path, NOT on POST_SAVE_ACTIONS. The new `disable_entity` service must explicitly emit a `meta:protected_changed`-equivalent audit log via `model_logit` to preserve audit symmetry.
- **`disable.history` size cap.** Cap at 20. Past that, drop oldest. Don't grow JSON forever — long-term audit trail should live in incident events / `logit.Log`, not the user record.
- **No FK on `by_user_id`.** Stays as plain int in JSON, matching the design discussion. If the admin user is later deleted, the int orphans gracefully — incident events have the username at the time of the action.
- **Throttle read is read-only.** No reset, no creation of counters, no side effects in Redis. Reset stays on `clear_rate_limit`.
- **`is_active` writes via REST still work** (`{"is_active": false}` continues to flip the flag) for back-compat with current admin tooling — they just won't write the namespace block. Document this as the legacy path; `disable`/`reactivate` actions are the recommended path going forward.

### Schema

```json
{
  "protected": {
    "disable": {
      "active": false,                          // mirrors !User.is_active for fast graph reads; writers maintain this
      "reason": "admin|inactive|anonymized|abuse|self|null",
      "at": "2026-05-09T14:22:01Z",
      "by_user_id": 42,                         // null for system-driven (sweep, anonymize)
      "by_username": "alice",                   // captured at action time; survives admin deletion
      "note": "Violated TOS §4. Refunded.",
      "exempt_from_auto_disable": false,        // replaces "no_disable"
      "warning": {                              // sweep state; cleared on disable or reactivation
        "sent_at": "2026-05-02T...",
        "days_until_disable_at_send": 7
      },
      "history": [                              // capped at 20, FIFO
        {
          "at": "2026-02-11T...",
          "reason": "inactive",
          "by_user_id": null,
          "by_username": "system",
          "note": null,
          "reactivated_at": "2026-02-15T...",
          "reactivated_by_user_id": 42,
          "reactivated_by_username": "alice",
          "reactivated_note": "User contacted support, account in good standing"
        }
      ]
    }
  }
}
```

`Group.metadata.protected.disable.*` uses the same shape; `reason` enum drops `anonymized`/`self` and adds `archived` if needed downstream.

### Related files

- [mojo/apps/account/models/user.py](mojo/apps/account/models/user.py)
- [mojo/apps/account/models/group.py](mojo/apps/account/models/group.py)
- [mojo/apps/account/models/geolocated_ip.py](mojo/apps/account/models/geolocated_ip.py) — POST_SAVE_ACTIONS reference pattern
- [mojo/apps/account/services/inactive.py](mojo/apps/account/services/inactive.py)
- [mojo/apps/account/asyncjobs.py](mojo/apps/account/asyncjobs.py)
- [mojo/apps/account/rest/user.py](mojo/apps/account/rest/user.py)
- [mojo/decorators/limits.py](mojo/decorators/limits.py)
- [mojo/models/rest.py](mojo/models/rest.py) — protected metadata gate at line 1434
- [docs/django_developer/account/inactive_sweep.md](docs/django_developer/account/inactive_sweep.md)

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `/api/user/<id>` body `{"disable": {"reason": "...", "note": "..."}}` | Flip `User.is_active=False` and write `disable.*` namespace + history entry | `manage_users` |
| POST | `/api/user/<id>` body `{"reactivate": {"note": "..."}}` | Flip `User.is_active=True`, append history entry | `manage_users` |
| POST | `/api/group/<id>` body `{"disable": {"reason": "...", "note": "..."}}` | Same as above for Group | `manage_groups` |
| POST | `/api/group/<id>` body `{"reactivate": {"note": "..."}}` | Same | `manage_groups` |
| GET | `/api/auth/throttle?user_id=N&key=login` | Read Redis sliding-window state for support tooling. Accepts `username=...` as alternative lookup. Returns `{count, limit, window, retry_after_seconds}` | `manage_users` |

Existing endpoints unchanged: `POST /api/auth/manage/clear_rate_limit` (keeps the same shape — pairs naturally with the new GET).

The legacy bare-flag write (`POST /api/user/<id>` body `{"is_active": false}`) keeps working for back-compat but is no longer the recommended path — does not write `disable.*` namespace.

## Settings

No new settings. Existing settings reused:

| Setting | Default | Purpose |
|---|---|---|
| `ACCOUNT_AUTO_DISABLE_ENABLED` | `False` | (existing) Master switch for inactivity sweep |
| `ACCOUNT_INACTIVE_DAYS` | `90` | (existing) |
| `ACCOUNT_INACTIVE_WARNING_DAYS` | `7` | (existing) |
| `GROUP_AUTO_DISABLE_ENABLED` | `False` | (existing) |
| `GROUP_INACTIVE_DAYS` | `90` | (existing) |
| `LOGIN_USERNAME_LIMIT` | `10` | (existing) read-back via new throttle endpoint |
| `LOGIN_USERNAME_WINDOW` | `900` | (existing) read-back via new throttle endpoint |

## Tests Required

- `disable_user_writes_namespace` — `POST {"disable": {"reason": "admin", "note": "x"}}` flips `is_active=False`, populates `disable.{active,reason,at,by_user_id,by_username,note}`, history empty.
- `disable_user_already_disabled_rejected` — second disable while already disabled returns 400 / no-op (decide: probably 400 with message; idempotent reactivate is more useful than idempotent disable).
- `reactivate_user_appends_history` — disable → reactivate produces `disable.history[0]` with both sides populated and clears `disable.{active,reason,at,by_user_id,by_username,note,warning}`.
- `reactivate_never_disabled_rejected` — 400 with message, no state change.
- `disable_history_cap_at_20` — 21 disable+reactivate cycles; assert `len(history) == 20` and oldest entry is gone.
- `disable_action_requires_manage_users` — call from `view_users` user → 403.
- `pii_anonymize_writes_namespace` — calling `pii_anonymize()` populates `disable.reason="anonymized"` and `disable.at`.
- `inactive_sweep_writes_new_shape` — fixture user past threshold; sweep run; assert `disable.warning.sent_at` (warn phase) then `disable.reason="inactive"` (disable phase).
- `inactive_sweep_reads_legacy_warning` — fixture user with only legacy `disable_warned=True` and `disable_warn_date` set; assert sweep treats them as already-warned and disables when threshold passes (back-compat).
- `inactive_sweep_honors_new_exempt_flag` — `disable.exempt_from_auto_disable=True` skipped.
- `inactive_sweep_honors_legacy_exempt_flag` — legacy `no_disable=True` still skipped (back-compat).
- `data_migration_idempotent` — run migration twice; assert second run is no-op.
- `data_migration_rewrites_legacy` — fixture with legacy keys; assert namespace populated, legacy keys still present (removed in follow-up release).
- `throttle_read_no_counter` — call endpoint for user with no failed logins; returns `{count: 0, limit: 10, window: 900, retry_after_seconds: 0}`.
- `throttle_read_under_limit` — 3 failed logins; endpoint returns count=3, retry_after=0.
- `throttle_read_over_limit` — 10 failed logins; endpoint returns count=10, retry_after>0.
- `throttle_read_requires_manage_users` — anonymous and `view_users` callers → 403/401.
- `throttle_read_username_lookup` — `?username=...` resolves correctly.
- `throttle_read_unknown_user` — returns 400 / clear error.
- `group_disable_reactivate` — symmetric to user tests, perm gate is `manage_groups`.
- `disable_action_emits_audit_log` — assert `meta:protected_changed`-equivalent log entry with `kind` describing the action.

## Out of Scope

- **Promoting any disable field to a real DB column.** Explicitly rejected in the design discussion. Revisit only if a hot-path filter at large user count proves JSON queries are too slow.
- **`GroupMember.is_active` cascading from `User.is_active`.** Memberships stay independent. A separate audit task can grep for permission-check sites that read `member.is_active` without also gating on `user.is_active`; that audit is recommended but not part of this request.
- **Persistent `User.locked_until` for failed-login lockouts.** Throttling stays in Redis. The new endpoint just exposes existing state.
- **A separate `User.disabled_reason` enum column.** Same rationale as above.
- **Auto-reactivate on next login after `disable.reason="inactive"`.** Worth doing later as a separate request — would need a grace-window setting and to thread through the login flow. Not in this scope.
- **Removing the legacy bare-flag write path** (`{"is_active": false}`). Stays in place; deprecation tracked separately when the data-migration follow-up lands.
- **Removing the legacy protected-metadata keys** (`disable_warned`, `disable_warn_date`, `no_disable`). Removed in a follow-up release after one full release cycle of dual-read.
- **Group-membership freeze/thaw on user disable.** Rejected unless the `GroupMember` audit reveals a real gap.
- **`Group` self-deactivation / `pii_anonymize` analog.** Out of scope; not currently a feature.

## Plan

**Status**: planned
**Planned**: 2026-05-09

### Objective

Centralize User/Group disable state on a single `disable.py` service that owns all writes to `metadata.protected.disable.*`, called from REST `disable`/`reactivate` POST_SAVE_ACTIONS, the inactive sweep, and `pii_anonymize` — eliminating the three independent flip sites for `is_active`. Add a Redis-backed throttle-read REST endpoint for support tooling. Provide one-release back-compat for legacy keys.

### Confirmed decisions

1. **Drop `disable.active` mirror.** `is_active` is the single source of truth.
2. **`pii_anonymize()` appends to history.** If the user has a non-empty live disable block, push it to `history` with `reactivated_at=null` + `reactivated_note="Anonymized; not reactivated"` before overwriting the live block with the anonymize record. Active users get an empty history (nothing to push). Re-anonymize cannot happen (terminal).
3. **Throttle endpoint path: `GET /api/auth/manage/throttle`** — parallels existing `auth/manage/clear_rate_limit`.
4. **Throttle scope v1: account + `key="login"` only.** Other keys → 400. IP/duid/muid lookups deferred.
5. **Idempotency = explicit 400.** Disable on already-disabled and reactivate on already-active throw `ValueException`.
6. **Reason enum split.** REST: User accepts `{admin, abuse}`; Group accepts `{admin, abuse, archived}`. Server-only: `{inactive, anonymized, self}`.

### Steps

1. **`mojo/apps/account/services/disable.py`** (NEW) — single-source service. Functions:
   - `disable_entity(entity, *, reason, by_user=None, note=None, request=None)` — atomic `filter(is_active=True).update(is_active=False)` to detect race; if updated_count==0 treat as already-disabled (raise). Writes `disable.{reason,at,by_user_id,by_username,note}`. Clears `disable.warning`. Emits `model_logit(kind="<model>:disable")` + `incident.report_event`.
   - `reactivate_entity(entity, *, by_user=None, note=None, request=None)` — guards already-active. Copies live disable block + adds `reactivated_{at,by_user_id,by_username,note}`, appends to `disable.history` with FIFO cap 20. Clears live `disable.{reason,at,by_user_id,by_username,note,warning}`. Flips `is_active=True`. Logs.
   - `record_anonymize(entity, *, by_user=None, request=None)` — pushes current live disable block (if non-empty) to `history` with `reactivated_at=null, reactivated_note="Anonymized; not reactivated"`. Writes new live block `{reason="anonymized", at, by_user_id, by_username}`. Caller is responsible for `is_active=False` (pii_anonymize already does it).
   - `mark_warning(entity, *, days_until_disable)` — writes `disable.warning.{sent_at,days_until_disable_at_send}`. Clears legacy `disable_warned`/`disable_warn_date` if present.
   - `clear_warning(entity)` — clears both new and legacy shapes.
   - `is_exempt(entity)` — returns True if `disable.exempt_from_auto_disable` OR legacy `no_disable`.
   - `migrate_legacy(entity)` — idempotent: if new namespace already populated, no-op; else rewrite legacy keys into new namespace, leave legacy in place.
   - `_append_history(entity, entry)` — internal; FIFO cap 20.

2. **`mojo/apps/account/models/user.py`** — `POST_SAVE_ACTIONS = ['send_invite', 'disable', 'reactivate']`. Add `on_action_disable(value)` validating `reason in {admin, abuse}` then calling `disable_service.disable_entity(self, ..., by_user=self.active_request.user, request=self.active_request)`. Add `on_action_reactivate(value)` calling `reactivate_entity`. Update `pii_anonymize()` at [user.py:787](mojo/apps/account/models/user.py:787) to call `disable_service.record_anonymize(self, by_user=...)` immediately before the existing `self.is_active = False` line at [user.py:814](mojo/apps/account/models/user.py:814).

3. **`mojo/apps/account/models/group.py`** — `POST_SAVE_ACTIONS = ['realtime_message', 'disable', 'reactivate']`. Mirror `on_action_*` with reason enum `{admin, abuse, archived}`.

4. **`mojo/apps/account/services/inactive.py`** — Refactor:
   - `warn_inactive_users` / `warn_inactive_groups` — replace inline `set_protected_metadata("disable_warned"/"disable_warn_date")` with `disable_service.mark_warning(entity, days_until_disable=...)`.
   - `disable_inactive_users` / `disable_inactive_groups` — replace direct `User.objects.filter(...).update(is_active=False)` block with `disable_service.disable_entity(entity, reason="inactive", by_user=None)`. Service handles the atomic update internally.
   - `_clear_stale_warnings` — read both shapes (new `disable.warning.sent_at` AND legacy `disable_warn_date`); clear via `disable_service.clear_warning`.
   - Exemption filter — currently `metadata__contains={"protected": {"no_disable": True}}`; extend with Q union to also exclude `metadata__contains={"protected": {"disable": {"exempt_from_auto_disable": True}}}`.

5. **`mojo/decorators/limits.py`** — Add `read_account_attempt(key, account_id, limit, window)` after `check_account_attempt` at [limits.py:298](mojo/decorators/limits.py:298). Returns `dict(count, limit, window, retry_after_seconds)`. No Redis writes. Fail-open returns count=0 on Redis errors.

6. **`mojo/apps/account/rest/user.py`** — Add new endpoint after `clear_rate_limit` at [rest/user.py:31](mojo/apps/account/rest/user.py:31):
   ```
   @md.GET('auth/manage/throttle')
   @md.requires_perms("manage_users")
   ```
   Accepts `user_id` or `username`, `key` (default `"login"`). For `key="login"`, look up `LOGIN_USERNAME_LIMIT` (default 10) / `LOGIN_USERNAME_WINDOW` (default 900). Other keys → 400. Calls `read_account_attempt` and returns the dict.

7. **Data migration** — `mojo/apps/account/migrations/00XX_disable_lifecycle_migrate.py` (number assigned by `bin/create_testproject`). `migrations.RunPython(forward, reverse=migrations.RunPython.noop)`. Forward: queryset of users/groups with `metadata__protected__disable_warned__isnull=False | metadata__protected__no_disable__isnull=False | metadata__protected__disable_warn_date__isnull=False`. For each, call `disable_service.migrate_legacy(entity)`. Idempotent — entity-level skip if new namespace already populated.

8. **`tests/test_account/test_disable_lifecycle.py`** (NEW) — covers all 19 scenarios from request file. Reuse fixture patterns from `tests/test_account/test_inactive_sweep.py`. Use `@th.django_unit_test()` decorator and `opts.client` for REST tests.

9. **Docs**:
   - NEW `docs/django_developer/account/disable_lifecycle.md` — schema, service API, history cap behavior, back-compat notes, integration with sweep and pii_anonymize
   - UPDATE `docs/django_developer/account/inactive_sweep.md` — replace "Protected Metadata Keys" section with link to new doc; note dual-read of legacy keys
   - UPDATE `docs/django_developer/account/user.md` — `disable`/`reactivate` POST_SAVE_ACTIONS section
   - UPDATE `docs/django_developer/account/group.md` — same
   - UPDATE `docs/django_developer/account/README.md` — add link to disable_lifecycle.md
   - UPDATE `docs/web_developer/account/user.md` — disable/reactivate body shapes; throttle endpoint
   - UPDATE `docs/web_developer/account/group.md` — disable/reactivate body shapes
   - UPDATE `CHANGELOG.md` — entry under next release

10. **No cron change** — existing `inactive_sweep` registration in [mojo/apps/account/cronjobs.py:14](mojo/apps/account/cronjobs.py:14) keeps working; only the inner service rewrites the metadata shape.

### Design Decisions

- **Service-layer single source of truth.** All `is_active` flips and `disable.*` writes go through `disable.py`. Three callers (REST, sweep, pii_anonymize) avoid drift.
- **Drop `disable.active` mirror** — `is_active` is single truth.
- **No FK on `by_user_id`** — plain int + captured `by_username` survives admin deletion.
- **Service bypasses `_can_edit_protected_json` REST gate intentionally.** That gate guards arbitrary client-driven JSON writes; the service is server-trusted and emits its own audit log via `model_logit`.
- **`PROTECTED_JSON_PERMS` unchanged on both models.** User has none; Group keeps `["admin_compliance","admin_verify"]`. New actions gate via SAVE_PERMS at REST entry, not via the JSON perms.
- **Throttle read scoped to login key only for v1.** Future expansion is a separate request.
- **Idempotency = 400.** Disabling already-disabled and reactivating already-active throw `ValueException` so UI gets clear signals.
- **Anonymize history rule:** push prior live block to history with `reactivated_at=null` so prior disable reasons are preserved; the anonymize record itself stays in the live block as the terminal state.

### Edge Cases

- **Race: REST disable + sweep disable simultaneously** → atomic update returns 0 on loser; service treats as already-disabled and raises `ValueException`. No double history.
- **`metadata=None` or missing `protected`** → service uses `setdefault` chain.
- **`disable_entity` clears any active warning** — sweep-disable inherits "warning was sent" implicitly via `reason="inactive"`.
- **Legacy `disable_warn_date` malformed (unparseable string)** → skip with `logit.warning`, mirror existing `_clear_stale_warnings` pattern at [inactive.py:34](mojo/apps/account/services/inactive.py:34).
- **REST `{"disable": {}}` with no reason** → 400 `ValueException("reason is required")`.
- **REST `{"disable": {"reason": "inactive"}}`** → 400 (server-only enum).
- **History overflow** — when `len(history) >= 20`, drop oldest before appending.
- **`pii_anonymize` on already-disabled user** → live disable block (with prior reason) is pushed to history with `reactivated_at=null`, then live block overwritten with anonymize record. Captured.
- **Group disable with `reason="archived"`** — accepted; treated identically to other disable reasons.
- **Group `PROTECTED_JSON_PERMS=["admin_compliance","admin_verify"]` does NOT include `manage_groups`** — intentional. `on_action_*` bypasses the JSON gate; gating is via `SAVE_PERMS=["manage_groups",...]` at REST entry.
- **`by_user=None` from sweep / anonymize** → service writes `by_user_id=None, by_username="system"`.
- **Throttle read with no Redis counter** → returns `{count: 0, limit: 10, window: 900, retry_after_seconds: 0}`.
- **Throttle read with unknown user** → 400 with clear error message.
- **Throttle read with `key` other than `login`** → 400 with message about scope.
- **Migration partial completion** → entity-level idempotency check (`disable.reason` or `disable.warning.sent_at` or `disable.exempt_from_auto_disable` populated) makes re-runs safe.
- **`pii_anonymize` already-anonymized user** → terminal one-shot; assume cannot happen. If it does (test scenario), the existing `disable.reason="anonymized"` gets pushed to history, then a new identical live block is written. Acceptable but undefined behavior.

### Testing

| Scenario | File |
|---|---|
| `disable_user_writes_namespace` | `tests/test_account/test_disable_lifecycle.py` |
| `disable_user_already_disabled_rejected` (400) | same |
| `reactivate_user_appends_history` | same |
| `reactivate_never_disabled_rejected` (400) | same |
| `disable_history_cap_at_20` | same |
| `disable_action_requires_manage_users` (403 from view_users) | same |
| `pii_anonymize_active_user_writes_namespace` | same |
| `pii_anonymize_disabled_user_pushes_to_history` | same |
| `inactive_sweep_writes_new_shape` (warn + disable phases) | same |
| `inactive_sweep_reads_legacy_warning` (back-compat) | same |
| `inactive_sweep_honors_new_exempt_flag` | same |
| `inactive_sweep_honors_legacy_exempt_flag` (back-compat) | same |
| `data_migration_idempotent` | same |
| `data_migration_rewrites_legacy` | same |
| `throttle_read_no_counter` | same |
| `throttle_read_under_limit` | same |
| `throttle_read_over_limit` | same |
| `throttle_read_requires_manage_users` | same |
| `throttle_read_username_lookup` | same |
| `throttle_read_unknown_user` (400) | same |
| `throttle_read_unsupported_key` (400) | same |
| `group_disable_reactivate` (manage_groups) | same |
| `disable_action_emits_audit_log` | same |
| `disable_invalid_reason_rejected` (400) | same |
| Existing sweep tests still pass with refactored service | `tests/test_account/test_inactive_sweep.py` (no edits expected) |

### Docs

| File | Action |
|---|---|
| `docs/django_developer/account/disable_lifecycle.md` | NEW — full schema, service API, history cap, back-compat |
| `docs/django_developer/account/inactive_sweep.md` | UPDATE — replace "Protected Metadata Keys" section with link |
| `docs/django_developer/account/user.md` | UPDATE — POST_SAVE_ACTIONS section |
| `docs/django_developer/account/group.md` | UPDATE — same |
| `docs/django_developer/account/README.md` | UPDATE — link to disable_lifecycle.md |
| `docs/web_developer/account/user.md` | UPDATE — disable/reactivate body shapes; throttle endpoint |
| `docs/web_developer/account/group.md` | UPDATE — disable/reactivate body shapes |
| `CHANGELOG.md` | Entry |

## Resolution

**Status**: resolved
**Date**: 2026-05-09
**Commits**: 9b0ac23, fc5e7c8

### What Was Built

Centralized User/Group disable lifecycle on a new `mojo.apps.account.services.disable` module that owns every write to `is_active` and the `metadata.protected.disable.*` namespace. Three callers go through it: REST `disable`/`reactivate` POST_SAVE_ACTIONS, the inactive sweep, and `pii_anonymize`. Atomic flip via conditional `update()` detects races and raises `ValueException` on already-in-target-state. History is FIFO-capped at 20.

Also added a `manage_users`-only `GET /api/auth/manage/throttle` endpoint that exposes the per-account login attempt counter from Redis without modifying it.

Legacy keys (`disable_warned`, `disable_warn_date`, `no_disable`) are still honoured on read for one release. Data migration `0041_disable_lifecycle_migrate` populates the new namespace from legacy keys without removing them.

### Files Changed

**New:**
- `mojo/apps/account/services/disable.py` — single-source service
- `mojo/apps/account/migrations/0041_disable_lifecycle_migrate.py` — data migration
- `tests/test_account/test_disable_lifecycle.py` — 26 tests
- `docs/django_developer/account/disable_lifecycle.md`

**Modified:**
- `mojo/apps/account/models/user.py` — `on_action_disable`, `on_action_reactivate`, `pii_anonymize` hook
- `mojo/apps/account/models/group.py` — `on_action_disable`, `on_action_reactivate` (`manage_groups` only)
- `mojo/apps/account/rest/user.py` — `GET /api/auth/manage/throttle`
- `mojo/apps/account/services/inactive.py` — refactored to call disable service
- `mojo/decorators/limits.py` — added `read_account_attempt`
- `tests/test_account/test_inactive_sweep.py` — 2 assertions updated to new shape
- `docs/django_developer/account/{user,group,inactive_sweep,README}.md`
- `docs/django_developer/assistant/README.md`
- `docs/web_developer/account/{user,group}.md`
- `CHANGELOG.md`

### Tests

- `tests/test_account/test_disable_lifecycle.py` — 26 scenarios (service, REST, sweep, throttle, migration). All passing.
- `tests/test_account/test_inactive_sweep.py` — existing 16 tests still passing after refactor.
- Full suite (`bin/run_tests --agent`): 2182 total, 1831 passed, 351 opt-in skipped, 0 failed.
- Run: `bin/run_tests --agent -t test_account.test_disable_lifecycle`

### Docs Updated

Both tracks plus the assistant README. New canonical reference is `docs/django_developer/account/disable_lifecycle.md`. The inactive sweep doc points to it for the schema.

### Security Review

One must-consider finding addressed in commit fc5e7c8: Group `on_action_disable` / `on_action_reactivate` originally accepted `"groups"` (baseline write perm) alongside `"manage_groups"`. Tightened to `manage_groups` only, matching the User pattern. Bare `is_active` writes via REST still work under the broader Group `SAVE_PERMS` for back-compat but don't populate the audit namespace.

Other findings noted (intentional admin-only existence oracle on throttle endpoint; correct atomic-race handling in `disable_entity`; no anonymized-user PII in the preserved disable namespace; idempotent migration) — no action required.

### Follow-up

- **Next release:** remove the legacy `disable_warned` / `disable_warn_date` / `no_disable` reads from `disable_service.is_exempt` / `has_warning` / `get_warning_sent_at`, then a follow-up migration to delete the legacy keys.
- **Consider later:** auto-reactivate on next login if `disable.reason == "inactive"` and within a grace window — separate request.
- **Consider later:** GroupMember.is_active cascade audit (grep for permission-check sites that read `member.is_active` without also gating on `user.is_active`).
