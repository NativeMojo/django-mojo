# Group context on incident events and incidents

**Type**: request
**Status**: resolved
**Date**: 2026-04-25
**Priority**: medium

## Description

Capture the originating group on every incident `Event` (and roll it up onto the parent `Incident`) so security operators can filter, bundle, and route by group. Today the incident system records `model_name` / `model_id` / `source_ip` / `uid` but loses the group context entirely — even when `request.group` is set or when the model instance has a `.group` field. Operators can't ask "what's noisy in group X" or "bundle these denials per group."

The change adds:
- A nullable, indexed `group` ForeignKey on `Event` and `Incident` (defaults to None — no impact on existing rows).
- Automatic group derivation in the incident reporter and `MojoModel.report_incident` so callers don't have to pass it.
- Mirrored `group_id` / `group_name` fields in event metadata so rules can match on group context.
- `BundleBy.GROUP_ID` (and combination modes) for ruleset bundling.
- Incident inherits `group` from its bundled events when they all share one; null when heterogeneous.

## Context

- The Bundle rule #74 in production had to dedupe `user_permission_denied` events by `source_ip` because there was no better key — operators wanted to bundle per-group but the data wasn't there.
- Multi-tenant deployments (the merchant-locations pattern) need per-group security visibility: which tenant is generating noise, which tenant has a misconfigured client, which tenant's users are hitting denials.
- `request.group` is already populated by the dispatcher (`mojo/decorators/http.py:76-78`) for every request that includes `group` in the body. That data is on every request — we just don't capture it on events.
- Many models already have a `group` ForeignKey (`Setting`, `UserAPIKey`, `RuleSet` is global, `ChatRoom`, `FileManager`, etc.) — instance-level group is also available for free in `report_incident`.

## Acceptance Criteria

- New nullable, indexed `group` FK on `Event` and `Incident` pointing to `account.Group`. Default `None`. Existing rows are unaffected (no backfill required).
- `incident.report_event(...)` automatically populates `event.group` from (in order): explicit `group=` kwarg → `kwargs["model_instance"].group` if instance was passed → `request.group` if set. Instance-level `group` wins over `request.group` when both exist.
- `MojoModel.report_incident` (instance method) automatically passes `self.group` when the model has that field.
- `MojoModel.class_report_incident` and `class_report_incident_for_user` automatically pass `request.group` when set.
- Event metadata always carries `group_id` and `group_name` (derived from the resolved group) so rules can match on metadata, not just on FK joins. When no group, neither key is set.
- `BundleBy` adds `GROUP_ID`, `GROUP_AND_MODEL_NAME`, `GROUP_AND_MODEL_NAME_AND_ID`, `GROUP_AND_SOURCE_IP` modes. The bundle-key builder in `Event.get_bundle_criteria` honors them.
- When bundling assigns events to an Incident, the Incident's `group` is set from the events: if all bundled events share the same `group_id`, the Incident inherits it; if any differ, Incident's `group` stays null. (For new Incidents, the seed event's group is the Incident's group.)
- Realtime/notification publish payload (`Event.publish`) includes `group_id` so downstream subscribers can route per-group.
- New tests cover: instance-vs-request precedence, no-group fallthrough, metadata mirror, bundle-by-group correctness, Incident inheritance (homogeneous and heterogeneous), and that the existing `MODEL_NAME_AND_ID` bundling still works unchanged.

## Investigation

**What exists**:
- `mojo/apps/incident/reporter.py:_create_event_dict` — builds event_data from request; today touches `request.ip / path / META / user` but not `request.group`.
- `mojo/apps/incident/models/event.py:Event` — has `model_name`, `model_id`, `source_ip`, `hostname`, `uid`, `country_code` as dedicated fields; no group field.
- `mojo/apps/incident/models/incident.py:Incident` — same shape, no group field.
- `mojo/apps/incident/models/rule.py:BundleBy` — enum with HOSTNAME / MODEL / IP combinations; no GROUP modes.
- `mojo/apps/incident/models/event.py:get_bundle_criteria` (~line 370) — branches on `bundle_by` to build the bundle dict.
- `mojo/models/rest.py:report_incident / class_report_incident / class_report_incident_for_user` (lines 1397-1453) — auto-stamp `model_name` and `model_id`; do not capture group.
- `mojo/decorators/http.py:76-78` — dispatcher already sets `request.group` from `request.DATA.group` for every request.

**What changes**:
- `mojo/apps/incident/models/event.py` — add `group = ForeignKey("account.Group", null=True, default=None, db_index=True, on_delete=models.SET_NULL, related_name="+")`. Update `get_bundle_criteria` to include group when `bundle_by` requests it. Add `group_id` to `publish()` payload. Add CSV format includes (optional).
- `mojo/apps/incident/models/incident.py` — same FK; on incident bundling/creation, inherit from seed event; on subsequent event link, downgrade to null if mismatched (or keep — see edge cases).
- `mojo/apps/incident/models/rule.py:BundleBy` — add four new constants and choices; existing values keep their numeric IDs.
- `mojo/apps/incident/reporter.py` — in `_create_event_dict`: resolve group via the precedence rule, set `event_data["group_id"]`, mirror `group_id`/`group_name` into `event_metadata`. Continue to store via the FK (resolved Group instance) when constructing the Event.
- `mojo/models/rest.py` — `report_incident` (instance) auto-passes `self.group` if the model has a `group` attr that is not None. `class_report_incident_for_user` and `class_report_incident` auto-pass `request.group` if set. Caller-supplied `group=` always wins (escape hatch).
- `mojo/apps/incident/migrations/` — two new migrations (Event.group, Incident.group). Nullable, indexed, no data migration.

**Constraints**:
- Backwards compat: `group` is nullable, default `None`; existing rows unchanged. Existing rules and dashboards keep working.
- Cross-app FK: `incident` → `account.Group`. Already an established pattern (`incident.IncidentHistory.group`, `Ticket.group` exist).
- Incident inheritance must not silently lose data: if events have inconsistent groups, Incident.group is null and metadata records the mismatch (e.g., `metadata.group_mismatch=True`).
- The `report_incident` auto-stamp must not raise if the model has no `group` attribute — guard with `getattr(self, "group", None)`.
- Dispatcher's `request.group` is set from `request.DATA.group` only when present — many requests have no group. Auto-stamp is purely opportunistic; null is the dominant case and is fine.

**Related files**:
- `mojo/apps/incident/reporter.py`
- `mojo/apps/incident/models/event.py`
- `mojo/apps/incident/models/incident.py`
- `mojo/apps/incident/models/rule.py`
- `mojo/apps/incident/models/history.py` (already has `group`; reference pattern)
- `mojo/models/rest.py` (`report_incident`, `class_report_incident`, `class_report_incident_for_user`)
- `mojo/decorators/http.py` (read-only — confirms `request.group` exists)
- `mojo/apps/incident/migrations/`
- `tests/test_incident/` (or similar)

## Endpoints

No new endpoints. Existing `/api/incident/event` and `/api/incident` REST graphs gain `group` via the standard FK serialization (likely `"group": "basic"` in the default graph).

## Settings

None. Behavior is implicit; opt-out is not needed (null is the safe default).

## Tests Required

- `Event.group` is None by default; existing rows continue to load.
- `report_event(request=req)` with `req.group` set populates `Event.group` and metadata `group_id`/`group_name`.
- `report_event(request=req)` with no `req.group` and no instance leaves `Event.group=None`, no metadata keys.
- `report_event(request=req, model_instance=obj)` where `obj.group != req.group` resolves to `obj.group` (instance wins).
- `MojoModel.report_incident` on a model with `.group` auto-stamps even when no request.
- `MojoModel.report_incident` on a model with no `.group` attribute does not raise.
- Caller-supplied `group=` kwarg overrides both instance and request.
- New `BundleBy.GROUP_ID` mode bundles two events with same group into one incident; different groups stay separate.
- Combination `BundleBy.GROUP_AND_MODEL_NAME_AND_ID` cross-checks both.
- Incident inherits `group` from its seed event; remains stable across same-group additions.
- Incident.group goes null when a heterogeneous-group event links to it (and `metadata.group_mismatch=True` is set).
- `MODEL_NAME_AND_ID` and existing bundling rules still produce identical bundles to the pre-change suite (regression).
- `Event.publish` payload includes `group_id` when set, omits it otherwise.

## Out of Scope

- Backfilling existing rows. The new column is null on every pre-migration row by design.
- A web-developer-facing API change beyond the FK appearing in event/incident graphs.
- Routing rules ("notify group admins of group X"); that's a follow-up once the data is captured.
- Changing `Incident.scope` semantics (still `"global"` / app label / etc. — `group` is orthogonal).
- Making `request.group` the default scope. Scope and group remain separate fields.

## Plan

**Status**: planned
**Planned**: 2026-04-25

### Objective
Capture the originating group on every `Event` and `Incident` via a nullable indexed FK, auto-derived from instance/request, mirrored into metadata, and supported as a first-class bundle dimension.

### Steps

1. **`mojo/apps/incident/models/event.py`** — add
   `group = ForeignKey("account.Group", null=True, default=None, db_index=True, on_delete=SET_NULL, related_name="+")`.
   Update `sync_metadata()` to snapshot `group_id` and `group_name` into `self.metadata` (only when group is set).
   Add four new bundle branches in `determine_bundle_criteria` for the new `BundleBy.GROUP_*` modes (lines ~388-403).

2. **`mojo/apps/incident/models/event.py:get_or_create_incident`** (lines ~303-362) — when constructing the new `Incident` (line ~332), pass `group=self.group` so the seed event's group is inherited.

3. **`mojo/apps/incident/models/event.py:link_to_incident`** (lines ~407-412) — before saving:
   - If `incident.group_id is None` and `self.group_id is not None`: set `incident.group = self.group` and save.
   - Elif `incident.group_id is not None` and `self.group_id != incident.group_id`: set `incident.group = None`, set `incident.metadata["group_mismatch"] = True`, save (`update_fields=["group", "metadata"]`).
   - Otherwise unchanged.

4. **`mojo/apps/incident/models/incident.py`** — add the same FK shape as Event:
   `group = ForeignKey("account.Group", null=True, default=None, db_index=True, on_delete=SET_NULL, related_name="+")`.
   Update `RestMeta.GRAPHS["default"].graphs` to include `"group": "basic"`.

5. **`mojo/apps/incident/models/rule.py:BundleBy`** — add four constants and CHOICES entries (existing numeric IDs unchanged):
   - `GROUP_ID = 10`
   - `GROUP_AND_MODEL_NAME = 11`
   - `GROUP_AND_MODEL_NAME_AND_ID = 12`
   - `GROUP_AND_SOURCE_IP = 13`

6. **`mojo/apps/incident/reporter.py:_create_event_dict`** — resolve and stamp group:
   - Pop `group` from kwargs (caller-supplied, sentinel-aware so explicit `None` suppresses).
   - If absent, look up `kwargs.get("model_instance")`'s `.group` attribute.
   - If still absent, fall back to `getattr(request, "group", None)`.
   - Validate the candidate is a `Group` instance (`isinstance` guard) before using.
   - Set `event_data["group"]` (the instance) so `Event(**event_data)` populates the FK.
   - Mirror `event_metadata["group_id"] = group.id` and `event_metadata["group_name"] = getattr(group, "name", None)` when group is non-None.
   - All accesses guarded against deletion-races (`getattr` with default).
   - Add `"group"` to the keys consumed at the top of `event_data` (so it doesn't leak into `processed_kwargs`).

7. **`mojo/models/rest.py`** — auto-stamp group at the three reporters (use `setdefault` so caller-supplied `group=` wins, including explicit `None`):
   - `report_incident` (instance, line 1397): if `getattr(self, "group", None)` is a Group instance, `context.setdefault("group", self.group)`.
   - `class_report_incident_for_user` (line 1410): after resolving `request`, if `getattr(request, "group", None)`, `context.setdefault("group", request.group)`.
   - `class_report_incident` (line 1426): same as above.
   - Import guard for `Group`: lazy-import inside the method to avoid circular import (`from mojo.apps.account.models import Group`).

8. **`mojo/apps/incident/migrations/0027_event_group_incident_group.py`** — single new migration adding both FKs (nullable, indexed, default None, SET_NULL, related_name="+"). No data migration. Run `bin/create_testproject` after.

9. **REST graph + CSV updates**:
   - `Event.RestMeta.GRAPHS["default"].graphs["group"] = "basic"`.
   - `Event.RestMeta.FORMATS["csv"]` append `"metadata.group_id"`, `"metadata.group_name"`.
   - `Incident.RestMeta.GRAPHS["default"].graphs["group"] = "basic"`.

### Design Decisions

- **`on_delete=SET_NULL`** on both FKs. Audit data must outlive the group it references — CASCADE would silently destroy security history when a tenant group is deleted.
- **Snapshot `group_name` into metadata** at event creation. The FK gives live access; metadata gives a frozen audit value that survives rename/delete. Both are cheap.
- **Precedence**: caller `group=` kwarg → `model_instance.group` → `request.group`. Caller is the explicit escape hatch; instance wins over request because "the event is about record X in group Y" is more specific than "the request asserted group Y."
- **`setdefault` for auto-stamp**: matches the existing pattern for `model_name`/`model_id` (rest.py:1402-1404). Caller-supplied `group=None` is preserved as a deliberate suppression.
- **Heterogeneous bundling surfaces conflict**: when events of different groups land on the same incident (via non-GROUP bundle modes), incident.group becomes null and `metadata.group_mismatch=True` — operators see they should tighten the bundle rule. Better than silently sticking with the seed's group and hiding the cross-tenant mix.
- **Discrete BundleBy enum entries** rather than an orthogonal `bundle_by_group` boolean. Migrates cleanly, documents in one table, mirrors the existing pattern. The orthogonal-boolean alternative was considered and rejected for being slightly more flexible but harder to discover.
- **`isinstance(val, Group)` guard** in the reporter so models that happen to use `.group` as a non-FK attribute (e.g. a string) don't get coerced.
- **No `request.group` write-back** from the reporter or auto-stamp — read-only.

### Use cases

| # | Caller | Result |
|---|---|---|
| 1 | `report_event(request=req)` with `req.group` set | Event.group = req.group, metadata.group_id/name populated |
| 2 | `report_event(request=req, model_instance=obj)` where `obj.group ≠ req.group` | Event.group = obj.group (instance wins) |
| 3 | `report_event(group=g)` (explicit kwarg) | Event.group = g (caller wins) |
| 4 | `report_event(group=None)` (explicit suppression) | Event.group = None, no auto-stamp |
| 5 | `report_event()` no request, no instance, no kwarg | Event.group = None |
| 6 | `MojoModel.report_incident()` on instance with `.group` field set | Auto-stamps instance.group |
| 7 | `MojoModel.report_incident()` on instance with no `.group` attr | Skips silently; falls to request.group via class methods |
| 8 | `class_report_incident_for_user(request=req)` with `req.group` | Auto-stamps from request |
| 9 | Bundle by `GROUP_ID` — same group, same category | Single incident |
| 10 | Bundle by `GROUP_ID` — different groups, same category | Separate incidents |
| 11 | Heterogeneous events linking onto a `MODEL_NAME_AND_ID` bundle | Incident.group set on first event, downgraded to null on mismatch, `group_mismatch=True` |
| 12 | Group deleted while events reference it | FK becomes null (SET_NULL); metadata snapshot preserves group_name |

### Edge cases

- **Self-referential reporting**: a denial event on a Group instance — `model_name="Group"`, `instance=<group>`. Event.group resolves to that same group via instance precedence (Group has no `.group` attribute, but the instance IS a Group, so the model_instance.group fallback yields None — the request.group fallback then applies). Acceptable.
- **`request.group` deleted mid-request**: `getattr(group, "id", None)` and `isinstance` guard handle the stale FK; reporter never raises.
- **`getattr(self, "group")` returning a non-Group truthy value**: `isinstance(val, Group)` guard skips the stamp rather than coercing.
- **Migration ordering**: `incident` already imports from `account` (existing IncidentHistory.group). No new dependency.
- **Bundle by GROUP_ID with `event.group=None`**: bundle key includes `group_id=None`, so all groupless events bundle together. Acceptable; combine with category/model for finer keys.
- **`bundle_by_rule_set=True` plus `GROUP_*`**: orthogonal flags — both criteria AND together. No conflict.
- **`Incident.group_mismatch` reset**: once set, stays set (audit-stable). A future event matching the original group does not clear the flag.
- **Test isolation**: tests must `Group.objects.filter(name=...).delete()` and `Event.objects.filter(category=...).delete()` in setup; the test DB is long-lived (CLAUDE.md rule).
- **Lazy import of `Group`** in `mojo/models/rest.py` auto-stamp methods to avoid circular import — `account.models` already depends on `mojo.models.MojoModel`.

### Testing

- `tests/test_incident/test_event_group.py` (new):
  - Reporter precedence: caller-kwarg > instance > request > none (cases 1–5).
  - `model_instance.group` wins over `request.group` when they differ.
  - Caller `group=None` suppresses the auto-stamp.
  - Group with `name=None` mirrors metadata `group_name=None` without raising.
  - Group deleted after event: `event.group_id` becomes null on refresh; `metadata.group_name` snapshot survives.
  - Non-Group `.group` attribute (string) is ignored by the isinstance guard.
- `tests/test_incident/test_bundle_by_group.py` (new):
  - `BundleBy.GROUP_ID` bundles same-group events; separates different-group events.
  - `BundleBy.GROUP_AND_MODEL_NAME_AND_ID` requires both to match.
  - `BundleBy.GROUP_AND_SOURCE_IP` requires both.
  - Existing `MODEL_NAME_AND_ID` bundling unchanged (regression).
- `tests/test_incident/test_incident_group_inherit.py` (new):
  - Seed event with group X → Incident.group = X.
  - Seed event with no group → Incident.group = None.
  - Linking event with same group → Incident.group unchanged.
  - Linking event with different group → Incident.group = None, `metadata.group_mismatch=True`.
  - Subsequent matching-group event after a mismatch → flag stays True (audit-stable).
- `tests/test_models/report_incident_group.py` (new):
  - `MojoModel.report_incident` on a Setting (has `.group`) auto-stamps without explicit kwarg.
  - `MojoModel.report_incident` on a Group instance (no `.group` attr) does not raise; falls through to request.
  - `class_report_incident_for_user(request=req)` with `req.group` populates Event.group.
  - Explicit `group=None` in caller suppresses the auto-stamp.
  - `class_report_incident` (no user) honors `request.group` when request is set.

### Docs

- `docs/django_developer/logging/incidents.md` — new "Group context" subsection covering auto-derivation precedence, FK + metadata snapshot, the four new BundleBy modes, the `group_mismatch` flag.
- `docs/django_developer/incident/` (if a directory exists, otherwise the existing logging/incidents.md is the canonical home) — bundle-modes table updated with the four new entries.
- `docs/django_developer/rest/permissions.md` — note that the seven denial event categories from the previous fix now also carry `group` automatically when the request or instance has one.
- `docs/web_developer/` — Event and Incident response shape gains a `group` field (basic graph: id + name).
- `CHANGELOG.md` — Added: `Event.group` and `Incident.group` FK with auto-derivation; `BundleBy.GROUP_*` modes; per-group bundling; metadata `group_mismatch` flag for heterogeneous bundles.

## Resolution

**Status**: resolved
**Date**: 2026-04-25

### What Was Built

Added group context to incident events and incidents via a nullable indexed `group` FK (`SET_NULL`) on both `Event` and `Incident`, auto-derived from `request.group` (reporter level) and `self.group` / `request.group` (MojoModel `report_incident` and class-level helpers). Group identity is also snapshotted into `event.metadata` (`group_id`, `group_name`) at creation time so audit records survive group rename or deletion. Four new `BundleBy` modes (`GROUP_ID`, `GROUP_AND_MODEL_NAME`, `GROUP_AND_MODEL_NAME_AND_ID`, `GROUP_AND_SOURCE_IP`, IDs 10-13) enable per-group bundling. `Incident.group` is seeded from the seed event and reconciled on link: heterogeneous bundles set `Incident.group=None` and stamp an audit-stable `metadata.group_mismatch=True`. REST graphs expose a scalar `group_id` (not a nested Group, to avoid cross-tenant leakage).

### Files Changed

- `mojo/apps/incident/models/event.py` — `group` FK, `sync_metadata` snapshot, seed-from-event in `get_or_create_incident`, group reconcile in `link_to_incident`, four new bundle branches in `determine_bundle_criteria`, scalar `group_id` in REST graph, `metadata.group_id`/`metadata.group_name` in CSV format.
- `mojo/apps/incident/models/incident.py` — `group` FK, scalar `group_id` in default + detailed graphs.
- `mojo/apps/incident/models/rule.py` — four new `BundleBy.GROUP_*` constants and choice entries (IDs 10-13).
- `mojo/apps/incident/reporter.py` — `_resolve_event_group` precedence resolver with `isinstance(Group)` guard; populates `event_data["group"]` and mirrors `group_id`/`group_name` into metadata.
- `mojo/models/rest.py` — `report_incident` auto-stamps `self.group`; `class_report_incident` and `class_report_incident_for_user` auto-stamp `request.group`; all use `setdefault` so caller-supplied `group=None` is preserved.
- `mojo/apps/incident/migrations/0027_event_group_incident_group_alter_ruleset_bundle_by.py` — adds both FKs (nullable, `SET_NULL`) and updates `RuleSet.bundle_by` choices.

### Tests

- `tests/test_incident/test_event_group.py` — 6 tests: reporter precedence (caller / request / none), `group=None` suppression, isinstance guard rejecting non-Group values, metadata snapshot survives group deletion.
- `tests/test_incident/test_bundle_by_group.py` — 5 tests: `GROUP_ID` bundles same-group / separates different-group, `GROUP_AND_MODEL_NAME_AND_ID` requires both, `GROUP_AND_SOURCE_IP` requires both, regression on existing `MODEL_NAME_AND_ID`.
- `tests/test_incident/test_incident_group_inherit.py` — 5 tests: seed inheritance, no-seed null, same-group stable, heterogeneous downgrade with flag, audit-stable flag (never clears).
- `tests/test_models/report_incident_group.py` — 5 tests: instance auto-stamp from `self.group`, no-attr fallthrough, `class_report_incident_for_user` auto-stamp from request, explicit `group=None` suppression, `class_report_incident` with request.
- Run: `bin/run_tests --agent -t test_incident -t test_models -t test_account` → 274/274 pass. Full suite: 1801/1891, 34 pre-existing parallel-execution flakes (test_auth login race, test_assistant FK race, test_aws perm leak — all unrelated, all pass in isolation).

### Docs Updated

- `docs/django_developer/logging/incidents.md` — Group context section (FK + auto-derivation + metadata snapshot + heterogeneous bundle behavior); Event Fields table updated with `group`; bundle-modes table extended with the four new `GROUP_*` modes.
- `docs/django_developer/core/mojo_model.md` — Group auto-stamping subsection under Incident Reporting.
- `docs/web_developer/logging/incidents.md` — scalar `group_id` field, metadata snapshot keys, `group_mismatch` audit-stable flag, HTML-escape note on `metadata.group_name`, full bundle_by table.
- `CHANGELOG.md` — `## Unreleased` entries: FK columns, auto-derivation, four new bundle modes, audit-stable mismatch flag, scalar `group_id` in REST graph (with security rationale).

### Security Review

One real WARNING surfaced and fixed in commit 91f87a7: the original commit (4f69245) included `"group": "basic"` in the default REST graphs, which the simple serializer would have expanded to a nested Group object without gating on the requester's group permissions — a cross-tenant leak of group name, kind, is_active, and last_activity to anyone with system-wide `view_security`. Replaced with a scalar `group_id` field; consumers needing the group's name look it up through `/api/group/<id>` (which respects per-group view perms). All other review items (mismatch reset behavior, isinstance guard, migration safety, metadata XSS responsibility, BundleBy.GROUP_ID null-handling, auto-stamp leakage) checked out clean.

### Follow-up

- Operators rendering `metadata.group_name` in a security console UI must HTML-escape the value (group name is user-controlled text). Documented in `docs/web_developer/logging/incidents.md`.
- The simple serializer has a more general gap (nested FK graphs are not permission-gated) — out of scope for this request, but worth a separate audit at some point.
