# Auto-Disable Inactive Users and Groups

**Type**: request
**Status**: resolved
**Date**: 2026-04-05
**Priority**: high

## Description

Add a nightly cron job that automatically disables users and groups after a configurable period of inactivity (default 90 days). Users receive a warning email 7 days before being disabled. Groups trigger a warning email to any system user with `manage_groups` or `groups` permission. Protected entities (`metadata.protected.no_disable = True`), superusers, and staff are exempt.

## Context

Stale accounts and groups are a security risk — unused credentials can be compromised without anyone noticing. Most compliance frameworks (SOC2, HIPAA, PCI-DSS) require inactive account policies. Today, disabling stale accounts is entirely manual.

Both `User` and `Group` already have `last_activity` (DateTimeField, nullable, indexed), `is_active` (BooleanField), and `metadata` (JSONField) — all the infrastructure needed. The `touch()` method on both models updates `last_activity` with configurable frequency (`USER_LAST_ACTIVITY_FREQ` / `GROUP_LAST_ACTIVITY_FREQ`, default 300s). The email system (`user.send_template_email()`) and cron job framework (`@schedule` + `jobs.publish()`) are already in place.

## Design

### Two-Phase Process

**Phase 1 — Warning (day 83 of inactivity, 7 days before disable)**:
- Query users/groups where `last_activity < now - (INACTIVE_DAYS - WARNING_DAYS)` AND `is_active = True`
- Skip protected, superusers, staff, and already-warned entities
- Send warning email
- Set `metadata.protected.disable_warned = True` and `metadata.protected.disable_warn_date = <ISO date>` to avoid re-sending

**Phase 2 — Disable (day 90 of inactivity)**:
- Query users/groups where `last_activity < now - INACTIVE_DAYS` AND `is_active = True`
- Skip protected, superusers, staff
- Set `is_active = False`
- Report incident event (`account:auto_disabled` / `group:auto_disabled`)
- Clear the warning metadata

### Exemptions

| Condition | Exempt? |
|---|---|
| `metadata.protected.no_disable = True` | Yes — never warned or disabled |
| `user.is_superuser = True` | Yes — auto-exempt |
| `user.is_staff = True` | Yes — auto-exempt |
| `user.last_activity is None` AND `user.last_login is None` | Yes — never logged in (invited but unused, handled separately) |
| `group.last_activity is None` | Yes — never been active, skip (new or unused group) |

### Warning Emails

**Users**: `send_template_email("account_inactive_warning", context)` sent to the user directly. Context includes: days until disable, how to reactivate (just log in), link to the platform.

**Groups**: Find all system users with `manage_groups` or `groups` permission via `User.objects.filter(is_active=True, permissions__has_key="manage_groups") | User.objects.filter(is_active=True, permissions__has_key="groups")`. Send each a `group_inactive_warning` template email listing the group name and days until disable.

### Activity Basis

- **Users**: `user.last_activity` field, updated by `user.touch()` on every authenticated request (throttled to `USER_LAST_ACTIVITY_FREQ` seconds)
- **Groups**: `group.last_activity` field, updated by `group.touch()` when the group is accessed

If a user logs in after receiving the warning, their `last_activity` updates, and on the next cron run they're no longer in the disable window — effectively self-reactivating.

### Incident Events

Every disable action reports via `incident.report_event()`:

| Event | Category | Level | Details |
|---|---|---|---|
| User auto-disabled | `account:auto_disabled` | 4 | User ID, username, email, days inactive |
| Group auto-disabled | `group:auto_disabled` | 4 | Group ID, name, member count, days inactive |
| User warned | `account:inactive_warning` | 2 | User ID, days until disable |
| Group warned | `group:inactive_warning` | 2 | Group ID, days until disable |

## Acceptance Criteria

- Users inactive for 90 days (configurable) are auto-disabled (`is_active = False`)
- Groups inactive for 90 days (configurable) are auto-disabled
- Users receive warning email 7 days before disable
- Group warnings go to system users with `manage_groups` or `groups` permission
- `metadata.protected.no_disable = True` exempts user or group from both warning and disable
- Superusers and staff are auto-exempt (no metadata needed)
- Users with `last_activity is None` and `last_login is None` are skipped (never logged in)
- Logging in after warning resets the inactivity clock (no disable)
- All disable actions report incident events
- Warning emails are sent once per cycle (tracked via metadata flag)
- Job is idempotent — running twice doesn't double-disable or double-warn

## Investigation

**What exists**:
- `User.last_activity` / `Group.last_activity` — indexed DateTimeFields, updated by `touch()`
- `User.is_active` / `Group.is_active` — the disable mechanism
- `User.metadata` / `Group.metadata` — JSONField for `protected.no_disable` flag
- `user.send_template_email()` — sends email via database templates + AWS SES
- `@schedule` decorator in `mojo/apps/account/cronjobs.py` — existing cron pattern
- `jobs.publish()` in `mojo/apps/account/asyncjobs.py` — existing async job pattern
- `incident.report_event()` — event reporting for the incident pipeline
- `User.objects.filter(permissions__has_key=...)` — JSONField permission lookup

**What changes**:
- New: `mojo/apps/account/asyncjobs.py` — add `disable_inactive_users()` and `disable_inactive_groups()` job functions
- New: `mojo/apps/account/cronjobs.py` — add `@schedule` entry for nightly inactive sweep
- New: Email templates — `account_inactive_warning` and `group_inactive_warning` (seed data)
- Modified: `docs/django_developer/account/README.md` — document settings, exemption mechanism, and event categories

**Constraints**:
- Must not disable accounts that have never logged in (`last_activity is None` + `last_login is None`) — these are pending invites, not stale accounts
- Must not disable superusers/staff — could lock out the only admin
- Email sending must be fail-safe — if email fails, still proceed with disable on schedule (don't let email failure prevent security action)
- Job must handle large user counts efficiently — batch queries, not per-user loops
- `metadata.protected.no_disable` must be settable via REST by users with `manage_users` permission

**Related files**:
- `mojo/apps/account/models/user.py` — User model with `last_activity`, `is_active`, `metadata`
- `mojo/apps/account/models/group.py` — Group model with same fields
- `mojo/apps/account/cronjobs.py` — existing `@schedule` cron pattern
- `mojo/apps/account/asyncjobs.py` — existing async job pattern
- `mojo/apps/aws/services/email.py` — email sending service
- `mojo/apps/incident/__init__.py` — `report_event()` API

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `ACCOUNT_INACTIVE_DAYS` | `90` | Days of inactivity before auto-disable |
| `ACCOUNT_INACTIVE_WARNING_DAYS` | `7` | Days before disable to send warning email |
| `ACCOUNT_AUTO_DISABLE_ENABLED` | `False` | Feature flag — must be explicitly enabled |
| `GROUP_INACTIVE_DAYS` | `90` | Days of inactivity before group auto-disable (separate from user setting) |
| `GROUP_AUTO_DISABLE_ENABLED` | `False` | Feature flag for group auto-disable |

## Tests Required

- User inactive 90+ days → disabled
- User inactive 83-89 days → warning email sent, not disabled
- User inactive 83+ days who logs in → no longer in disable window
- Superuser inactive 90+ days → NOT disabled
- Staff user inactive 90+ days → NOT disabled
- User with `metadata.protected.no_disable = True` → NOT disabled or warned
- User with `last_activity = None` and `last_login = None` → NOT disabled (never logged in)
- Group inactive 90+ days → disabled
- Group warning email sent to users with `manage_groups`/`groups` permission
- Group with `metadata.protected.no_disable = True` → NOT disabled
- Warning email sent only once per cycle (idempotent)
- Incident events reported for both warn and disable actions
- Job handles zero matches gracefully (no errors on empty result sets)
- Settings can be overridden per-deployment

## Out of Scope

- Re-enabling disabled accounts automatically (manual action only)
- Deleting inactive accounts (disable only, no data loss)
- Per-group custom inactivity thresholds (use the global setting)
- SMS or push notification warnings (email only)
- UI/dashboard for viewing inactive accounts (use existing admin + assistant tools)

## Plan

**Status**: planned
**Planned**: 2026-04-05

### Objective

Add a nightly cron job that warns (7 days before) and then auto-disables users and groups after configurable inactivity, with exemptions, email notifications, and incident events.

### Steps

1. `mojo/apps/account/models/group.py` — Add `get_protected_metadata(key, default)` and `set_protected_metadata(key, value)` methods to Group, matching User's implementation (lines 259-268). Group already inherits `jsonfield_as_objict` from MojoModel. Keeps the `metadata.protected.*` pattern consistent across both models.

2. `mojo/apps/account/services/inactive.py` (new) — Core service with five functions:
   - `_clear_stale_warnings(Model, cutoff_field)` — find entities where `disable_warned = True` but `last_activity` is more recent than `disable_warn_date`. Clear both warning metadata fields. This prevents the bug where a warned user who logs back in never gets warned again on their next inactive cycle.
   - `warn_inactive_users()` — query `User.objects.filter(is_active=True, last_activity__lt=warn_cutoff, last_activity__isnull=False).exclude(is_superuser=True).exclude(is_staff=True).exclude(metadata__protected__no_disable=True).exclude(metadata__protected__disable_warned=True)`. Also exclude users where `last_activity is None AND last_login is None` (never logged in). Send `account_inactive_warning` email per user (try/except, fail-safe). Set `metadata.protected.disable_warned = True` + `disable_warn_date = <ISO date>`. Report `account:inactive_warning` event per user.
   - `disable_inactive_users()` — query same base filters but with `last_activity__lt=disable_cutoff`. Use atomic `User.objects.filter(pk=user.pk, last_activity__lt=disable_cutoff).update(is_active=False)` per user to avoid race conditions if user logs in during sweep. Only report event + audit log if update actually modified a row (returns 1). Clear warning metadata. Report `account:auto_disabled` event. `User.class_logit()` for audit trail.
   - `warn_inactive_groups()` — query `Group.objects.filter(is_active=True, last_activity__lt=warn_cutoff, last_activity__isnull=False).exclude(metadata__protected__no_disable=True).exclude(metadata__protected__disable_warned=True)`. For each group, find system-level `User.objects.filter(is_active=True).filter(Q(permissions__has_key="manage_groups") | Q(permissions__has_key="groups"))` and send each a `group_inactive_warning` email listing the group name, ID, and days until disable. These are platform-wide group admin users, NOT members of the specific group. Set warning metadata on the group. Report `group:inactive_warning` event.
   - `disable_inactive_groups()` — query groups past disable cutoff, same exemptions. Atomic update per group. Report `group:auto_disabled` event with member count. `Group.class_logit()` for audit trail. Clear warning metadata.

3. `mojo/apps/account/asyncjobs.py` — Add `inactive_sweep(job)` function:
   - Check `ACCOUNT_AUTO_DISABLE_ENABLED` flag → if True: `_clear_stale_warnings(User)`, `warn_inactive_users()`, `disable_inactive_users()`
   - Check `GROUP_AUTO_DISABLE_ENABLED` flag → if True: `_clear_stale_warnings(Group)`, `warn_inactive_groups()`, `disable_inactive_groups()`
   - Log summary counts (warned, disabled, warnings cleared) via `logit.info()`

4. `mojo/apps/account/cronjobs.py` — Add `@schedule(minutes="0", hours="3")` entry that publishes the `inactive_sweep` job to the `cleanup` channel. Runs at 3am daily.

5. `mojo/apps/aws/seeds/email_templates/account_inactive_warning.json` (new) — Email template. Context keys: `user`, `days_until_disable`, `inactive_days`. Subject: "Your account will be disabled in {days_until_disable} days". Body: explains inactivity period, instructs to log in to keep account active.

6. `mojo/apps/aws/seeds/email_templates/group_inactive_warning.json` (new) — Email template. Context keys: `group_name`, `group_id`, `days_until_disable`, `inactive_days`. Subject: "Group '{group_name}' will be disabled in {days_until_disable} days". Body: addressed to group admins, explains group inactivity.

### Design Decisions

- **Service layer in `services/inactive.py`**: Functions are independently testable and callable from management commands or the assistant in the future. Not inline in the async job.
- **Warning metadata cleared on reactivation**: `_clear_stale_warnings()` runs BEFORE the warn phase. Checks if `last_activity > disable_warn_date` — if so, the entity reactivated after being warned, so clear the flag. This ensures they get a fresh warning if they go inactive again later.
- **Atomic update for disable**: `Model.objects.filter(pk=pk, last_activity__lt=cutoff).update(is_active=False)` prevents race condition where user logs in between query and save. Only proceeds with event/audit if `update()` returns 1.
- **Group warnings go to system users, not group members**: `User.objects.filter(permissions__has_key="manage_groups") | User.objects.filter(permissions__has_key="groups")` finds platform-wide group admins. These are `account.User` records with system-level permissions, not `account.GroupMember` records. Every group warning goes to the same set of admin users.
- **Single cron → single async job**: One `@schedule` entry dispatches one `inactive_sweep` job. Warn phase always runs before disable phase. Simple and predictable.
- **Feature flags off by default**: Both `ACCOUNT_AUTO_DISABLE_ENABLED` and `GROUP_AUTO_DISABLE_ENABLED` default to `False`. Cron runs but sweep is a no-op until explicitly enabled.
- **Email failure doesn't block disable**: Warning emails are best-effort (try/except per user). Security action (disable) takes priority over notification delivery.
- **Email template auto-load**: Depends on `planning/requests/email-template-auto-load.md` for automatic seed loading. Without it, templates must be seeded manually before enabling.

### Edge Cases

- **Null `last_activity` + null `last_login`**: Skipped — these are pending invites, never logged in. Excluded by `last_activity__isnull=False` plus an explicit check for `last_login is None`.
- **Null `last_activity` + non-null `last_login`**: User logged in before `last_activity` tracking existed. Use `last_login` as fallback: query includes `Q(last_activity__lt=cutoff) | Q(last_activity__isnull=True, last_login__lt=cutoff, last_login__isnull=False)`.
- **Race condition — user logs in during sweep**: Atomic `filter(pk=pk, last_activity__lt=cutoff).update()` returns 0 if activity updated — disable is safely skipped.
- **Group with no members**: Irrelevant — warning emails go to system-level users with `manage_groups`/`groups` permission, not group members. Disable proceeds regardless.
- **No users with manage_groups/groups permission**: Group warning email has no recipients. Log a warning, but still set the warning metadata on the group (disable still proceeds on schedule).
- **Large deployments**: All queries use indexed fields (`last_activity`, `is_active`). Iterating stale accounts for individual saves is acceptable for a nightly batch job.
- **Warned user who reactivated then goes stale again**: `_clear_stale_warnings()` clears the `disable_warned` flag because `last_activity > disable_warn_date`. On next stale cycle they get a fresh warning.

### Testing

- User inactive 90+ days → disabled → `tests/test_account/test_inactive_sweep.py`
- User inactive 83-89 days → warned, not disabled → same file
- User warned who logs in → warning cleared, not disabled on next sweep → same file
- User warned, reactivated, goes stale again → gets fresh warning → same file
- Superuser/staff inactive 90+ days → NOT disabled → same file
- `metadata.protected.no_disable = True` → exempt from both phases → same file
- `last_activity = None, last_login = None` → exempt (never logged in) → same file
- Group inactive 90+ days → disabled → same file
- Group warning emails sent to system users with `manage_groups`/`groups` perm → same file
- Group `metadata.protected.no_disable = True` → exempt → same file
- Warning sent only once per cycle (idempotent) → same file
- Incident events fired for warn + disable → same file
- Feature flag off → no-op → same file
- Zero matches → no errors → same file

### Docs

- `docs/django_developer/account/README.md` — New "Auto-Disable Inactive Accounts" section: settings, exemptions, event categories, how to enable, warning metadata fields
- `docs/web_developer/account/README.md` — Note that `metadata.protected.no_disable` is settable via REST with `manage_users` permission

## Resolution

**Status**: resolved
**Date**: 2026-04-05

### What Was Built
Nightly cron job that warns (7 days before) and auto-disables users and groups after 90 days of inactivity. Feature-flagged off by default. Superusers, staff, and protected entities exempt. Atomic disable with race condition protection. All actions report incident events.

### Files Changed
- `mojo/apps/account/models/group.py` — Added `get_protected_metadata()` and `set_protected_metadata()` methods
- `mojo/apps/account/services/inactive.py` — Core service: warn/disable for users and groups, stale warning cleanup
- `mojo/apps/account/asyncjobs.py` — Added `inactive_sweep` job function
- `mojo/apps/account/cronjobs.py` — Added `@schedule(minutes="0", hours="3")` cron entry
- `mojo/apps/aws/seeds/email_templates/account_inactive_warning.json` — User warning email template
- `mojo/apps/aws/seeds/email_templates/group_inactive_warning.json` — Group warning email template

### Tests
- `tests/test_account/test_inactive_sweep.py` — 16 tests
- Run: `bin/run_tests -t test_account.test_inactive_sweep`

### Follow-up
- None
