# Group Audit Trail for LOG_CHANGES

**Type**: request
**Status**: resolved
**Date**: 2026-04-08
**Priority**: medium

## Description

When `LOG_CHANGES` is enabled on a model and the instance has a non-null `group` field, the log entry should also record the group ID. This enables querying all audit log entries for a specific group — a group-scoped audit trail.

Currently, `Log` entries track `model_name`, `model_id`, `uid`, and `username`, but have no concept of which group the changed record belongs to. This means there's no way to pull "all changes made to any record owned by Group X."

## Context

Multi-tenant systems need per-group audit trails. When a model instance belongs to a group (e.g., a project, a team asset, a shared resource), changes to that instance should be queryable by group. This is a framework-level enhancement — any model with `LOG_CHANGES = True` and a `group` FK automatically gets group audit logging with zero extra work from the developer.

## Acceptance Criteria

- Log model gains a `group_id` integer field (nullable, indexed)
- When `LOG_CHANGES` fires (create, update), `gid` is populated from: (1) `instance.group_id` if the model has a group field and it's not None, or (2) `request.group.id` if the request has a group context
- More broadly, `Log.logit()` always checks `request.group` so that ANY log call (not just LOG_CHANGES) captures group context when available
- Existing logs without group context continue to work (field is nullable)
- Log entries can be queried by `group_id` for group audit trails
- No behavior change for models without a `group` field

## Investigation

**What exists**:
- `mojo/models/rest.py` — LOG_CHANGES fires at 3 points:
  - Line 391: `create_from_dict()` — `instance.log(kind="model:created", ...)`
  - Line 400: `create_from_request()` — `instance.log(kind="model:created", ...)`
  - Line 1004: `on_rest_save()` — `self.log(kind="model:changed", log=self.get_changes(data_dict))`
- `mojo/models/rest.py:1294` — `log()` method: calls `self.class_logit(request, log, kind, self.id, level, **kwargs)`
- `mojo/models/rest.py:1309` — `class_logit()`: calls `Log.logit(request, log, kind, cls.get_model_string(), model_id, level, **kwargs)`
- `mojo/apps/logit/models/log.py:61` — `Log.logit()` classmethod creates the Log record. Accepts `**kwargs`.
- `mojo/apps/logit/models/log.py` — Log model fields: `id, created, level, kind, method, path, payload, ip, duid, uid, username, user_agent, log, model_name, model_id`. **No `group_id` field currently.**
- `mojo/models/rest.py:1324` — `has_field(field_name)` classmethod already exists for field detection.

**What changes**:
- `mojo/apps/logit/models/log.py` — Add `gid = models.IntegerField(default=0, db_index=True)` to Log model (matches `uid` naming convention)
- `mojo/apps/logit/models/log.py` — Update `Log.logit()` to accept and store `group_id` from kwargs
- `mojo/models/rest.py` — At each LOG_CHANGES call site, detect if instance has a non-null `group` field and pass `group_id=instance.group_id` to `log()`/`class_logit()`
- Migration for the new field

**Constraints**:
- Backwards compatible: `group_id` defaults to 0 (no group), existing log entries unaffected
- Use `IntegerField` (not FK) to match the existing pattern — Log model uses `uid` (int) not a User FK, and `model_id` (int) not a generic FK. This keeps Log lightweight and avoids cross-app FK dependencies.
- Must not break models that don't have a `group` field
- The `instance.log()` → `class_logit()` → `Log.logit()` chain already passes `**kwargs`, so `group_id` flows through naturally once `Log.logit()` handles it

**Related files**:
- `mojo/apps/logit/models/log.py`
- `mojo/models/rest.py` (lines 391, 400, 1004, 1294, 1309)

## Tests Required

- Model with `LOG_CHANGES = True` and a `group` FK: create and update, verify Log entry has correct `group_id`
- Model with `LOG_CHANGES = True` and no `group` field: create and update, verify Log entry has `group_id=0`
- Model with `LOG_CHANGES = True` and `group=None`: verify Log entry has `group_id=0`
- Query Log entries by `group_id` to confirm group audit trail works
- Verify existing log methods still work when `group_id` is not passed

## Out of Scope

- REST endpoint for querying group audit logs (can be added later via Log's RestMeta)
- Retroactively backfilling `group_id` on existing log entries
- Logging group changes when `group` FK itself is changed (e.g., reassigning a record to a different group)
- UI for viewing group audit trail

## Plan

**Status**: resolved
**Planned**: 2026-04-08

### Objective

Add a `gid` field to the Log model and auto-populate it from the model instance's group or request context, enabling per-group audit trail queries.

### Steps

1. `mojo/apps/logit/models/log.py` — Add `gid = dm.IntegerField(default=0)` field after `uid` (line 18). Add `dm.Index(fields=['gid'])` and `dm.Index(fields=['gid', 'kind'])` to Meta.indexes. Add `"gid"` to both `basic` and `default` GRAPHS.

2. `mojo/apps/logit/models/log.py` — Update `Log.logit()` (line 61) to resolve `gid`:
   - Pop `gid` from kwargs
   - If not provided and `request` has a `group` attribute with a non-None value, use `request.group.id`
   - Default to `0`
   - Pass `gid=gid` to `cls.objects.create()`

3. `mojo/models/rest.py:1294` — Update `log()` instance method to detect group on `self`:
   ```
   def log(self, log="", kind="model_log", level="info", **kwargs):
       if "gid" not in kwargs:
           group_id = getattr(self, "group_id", None)
           if group_id:
               kwargs["gid"] = group_id
       return self.class_logit(ACTIVE_REQUEST.get(), log, kind, self.id, level, **kwargs)
   ```
   This single change covers all 3 LOG_CHANGES call sites (lines 391, 400, 1004) since they all call `self.log()`.

4. Run `bin/create_testproject` to generate migration for the new `gid` field.

5. Add tests (see Testing section below).

6. Update docs for both tracks.

### Design Decisions

- **`gid` as IntegerField(default=0), not FK**: Matches existing `uid` and `model_id` pattern. Keeps Log lightweight, avoids cross-app FK dependencies, and allows logging even if the group is later deleted.
- **Resolution in `Log.logit()` for request.group**: Any code that calls `Log.logit(request, ...)` gets group context for free — not just LOG_CHANGES. Manual logit calls, model_logit calls, security logs — all benefit.
- **Resolution in `MojoModel.log()` for instance group**: The instance method is the right place because `self` has the group FK. Avoids touching the 3 LOG_CHANGES call sites individually.
- **Priority order**: Explicit `gid` kwarg > `instance.group_id` (from `log()`) > `request.group.id` (from `logit()`) > `0`. Callers can always override.
- **Composite index `[gid, kind]`**: The primary query pattern will be "all model:changed and model:created logs for group X" — this index serves that directly.

### Edge Cases

- **Model with no group field**: `getattr(self, "group_id", None)` returns None, `gid` not set in kwargs, falls through to `Log.logit()` which checks `request.group`. If neither exists, defaults to 0.
- **Model with group=None**: `group_id` is None (not truthy), same path as above.
- **group FK is deferred/not loaded**: `group_id` is always available on the instance (it's the DB column), no extra query needed.
- **request is None (system/background calls)**: `Log.logit()` skips the request.group check, `gid` stays 0 unless explicitly passed.
- **request.group not set**: Some request objects may not have the `group` attribute at all (e.g., unauthenticated). Use `getattr(request, "group", None)` defensively.

### Testing

- Model with `LOG_CHANGES = True` + group FK: create and update, verify Log.gid matches instance.group_id -> `tests/test_logit/test_group_audit.py`
- Model with `LOG_CHANGES = True` + no group field: verify Log.gid is 0 -> `tests/test_logit/test_group_audit.py`
- Model with `LOG_CHANGES = True` + group=None: verify Log.gid is 0 -> `tests/test_logit/test_group_audit.py`
- Manual `Log.logit(request, ...)` with request.group set: verify gid populated -> `tests/test_logit/test_group_audit.py`
- Manual `Log.logit(request, ..., gid=5)`: verify explicit gid takes precedence -> `tests/test_logit/test_group_audit.py`
- Query `Log.objects.filter(gid=group.id)` returns correct audit trail -> `tests/test_logit/test_group_audit.py`

### Docs

- `docs/django_developer/logit/` — Document gid field, auto-population behavior, and how to query group audit trails
- `docs/web_developer/logit/` — Document gid in Log REST response graphs
- `CHANGELOG.md` — Note new `gid` field on Log model for group audit trails

## Resolution

**Status**: resolved
**Date**: 2026-04-08

### What Was Built
Added `gid` (group ID) integer field to the Log model. Auto-populates from model instance's `group_id`, `request.group`, or explicit kwarg. Enables per-group audit trail queries.

### Files Changed
- `mojo/apps/logit/models/log.py` — Added `gid` field, indexes (`gid`, `gid+kind`), updated GRAPHS, updated `logit()` to resolve gid
- `mojo/models/rest.py` — Updated `MojoModel.log()` to inject `gid` from `self.group_id`
- `mojo/apps/logit/migrations/0007_log_gid.py` — Migration for new field and indexes
- `mojo/apps/logit/migrations/0008_rename_logit_log_gid_idx_...py` — Django auto-rename of index names

### Tests
- `tests/test_logit/group_audit.py` — 8 tests covering explicit gid, request.group, None/missing group, query by gid, model instance gid injection
- Run: `bin/run_tests -t test_logit`

### Docs Updated
- `docs/django_developer/logging/logit.md` — gid field, auto-population, query examples
- `docs/web_developer/logging/logs.md` — gid in response samples and filtering
- `CHANGELOG.md` — new entry

### Security Review
- No concerns introduced by this change
- Pre-existing: `logit/rest.py` missing `@md.uses_model_security(Log)` (endpoint returns 404) — separate fix needed
- `model_logit()` bypasses gid auto-injection (minor, callers can pass gid explicitly)

### Follow-up
- Fix `logit/rest.py` missing `@md.uses_model_security(Log)` decorator (pre-existing bug)
- Consider adding gid injection to `model_logit()` for consistency
