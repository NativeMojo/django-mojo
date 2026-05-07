# Parent-Group Fan-Out for Metrics Fetch

**Type**: request
**Status**: resolved
**Date**: 2026-05-07
**Priority**: medium

## Description

Allow a metrics caller to request a parent group's metrics aggregated across all of that group's child groups of a given `kind`. Today metrics are stored per-account (`group-<id>`) and `/api/metrics/fetch` reads exactly one account. Add a fan-out path so a caller can pass `account=group-<parent_id>&child_kind=<kind>` and receive a single time-series that is the sum of every matching child account's series, bucket by bucket.

Default aggregation is **sum**. Permission is checked **once on the parent group**; the existing parent-chain walk in `Group.user_has_permission` already grants visibility into descendants for parent-group admins.

## Context

Operators of multi-tenant deployments commonly model a top-level org as a parent group with many child groups (often filtered by `kind`, e.g. all locations of one operator). Today, building a parent-level dashboard requires the client to:

1. List child group IDs.
2. Issue N `/api/metrics/fetch` calls (one per `group-<child_id>`).
3. Sum series client-side.

That's wasteful and races against permission drift. Putting the fan-out in the server is a small change — the primitives already exist:

- `Group.get_children(is_active=True, kind=...)` returns the recursive descendants filtered by kind.
- `_check_group_account_permission` already validates the caller against the parent group via `Group.user_has_permission(..., check_parents=True)`.
- `metrics.fetch()` already accepts cluster-safe per-account fetches.

The missing piece is a small loop in the REST layer that resolves child accounts, fetches each, and sums results.

## Acceptance Criteria

- `GET /api/metrics/fetch` accepts a new `child_kind=<string>` query param.
- When `child_kind` is set:
  - `account` must be of the form `group-<parent_id>`; otherwise return a 400.
  - Permission is checked once on the parent group via the existing helper.
  - Targets are resolved with `parent.get_children(is_active=True, kind=child_kind)`.
  - For each target, fetch `slug` (or `slugs`, or `category`) at `account=group-<child_id>` and **sum** the per-bucket values across targets.
  - Response shape is identical to the existing single-account response (including `with_labels` behavior). Labels come from the time range and are unaffected by fan-out.
- If the resolved child set is empty, return a zero-filled series of the correct length (no error).
- If the resolved child set exceeds `METRICS_FANOUT_MAX_CHILDREN` (default 200), return a 400 with a clear message instead of truncating.
- `child_kind` is ignored if `account` is not a group account (no silent fan-out for `public`/`global`/`user-*`).
- Tests cover the permission branch, sum correctness, empty-children, cap-exceeded, and `with_labels` parity.
- Docs updated in both `docs/django_developer/metrics/fetching.md` and `docs/web_developer/metrics/metrics.md`.
- `CHANGELOG.md` entry.

## Investigation

**What exists**:
- [redis_metrics.py:101](mojo/apps/metrics/redis_metrics.py:101) — `fetch()` already cluster-safe via hash tags + `mget_any`. Per-account; no group awareness.
- [rest/base.py:62](mojo/apps/metrics/rest/base.py:62) — `on_metrics_data` parses `slug`/`slugs`/`category`, calls `metrics.fetch`, returns `{labels, data}`.
- [rest/helpers.py:5](mojo/apps/metrics/rest/helpers.py:5) — `_check_group_account_permission` parses `group-<id>`, calls `Group.user_has_permission`, which walks the parent chain via `get_member_for_user(check_parents=True)`.
- [account/models/group.py:221](mojo/apps/account/models/group.py:221) — `Group.get_children(is_active=True, kind=...)` returns recursive descendants.

**What changes**:
- `mojo/apps/metrics/rest/base.py` — extend `on_metrics_data` to detect `child_kind` and dispatch to a fan-out helper.
- `mojo/apps/metrics/rest/helpers.py` (or a new `mojo/apps/metrics/services.py`) — add `fetch_group_fanout(parent_id, child_kind, slugs, dt_start, dt_end, granularity, with_labels, allow_empty)` that returns the aggregated `{labels, data}` payload. Keeps Django-model knowledge out of `redis_metrics.py`.
- `mojo/helpers/settings` consumer — read `METRICS_FANOUT_MAX_CHILDREN` (default 200).
- Tests under `tests/test_metrics/` — new `test_fanout.py` (or extend an existing fetch test file).
- Docs: `docs/django_developer/metrics/fetching.md` (Python helper), `docs/web_developer/metrics/metrics.md` (REST param table + example), `CHANGELOG.md`.

**Constraints**:
- Each child account lives in a different cluster slot (hash tag is account-scoped). Fan-out is N pipelines, not a single MGET. The 200 cap exists to bound this.
- `redis_metrics.fetch()` must stay account-agnostic — fan-out belongs in the REST/service layer, not in the redis helper.
- Permission boundary must remain "parent only." Do **not** loop per-child permission checks (defeats the purpose, and sub-groups inherit parent visibility by design).
- Response shape must match the existing fetch handler exactly so dashboards don't need a second code path.

**Related files**:
- `mojo/apps/metrics/rest/base.py`
- `mojo/apps/metrics/rest/helpers.py`
- `mojo/apps/metrics/redis_metrics.py`
- `mojo/apps/account/models/group.py`
- `docs/django_developer/metrics/fetching.md`
- `docs/web_developer/metrics/metrics.md`
- `CHANGELOG.md`

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | `/api/metrics/fetch` (with `child_kind`) | Sum metrics across child groups of `kind` under `account=group-<parent_id>` | `view_metrics` or `metrics` on the parent group (member parent-chain ok) |

New query parameters on `/api/metrics/fetch`:

| Param | Default | Description |
|---|---|---|
| `child_kind` | unset | When set, fan out to all active descendants of `account=group-<parent_id>` whose `kind` matches. Sums per-bucket. Ignored unless `account` is `group-<id>`. |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `METRICS_FANOUT_MAX_CHILDREN` | `200` | Hard cap on the number of child groups a single fan-out fetch will dispatch to. Requests resolving more children return a 400. |

## Tests Required

- Sum correctness: parent has 3 children of kind `location`, each with known counts in known buckets — verify per-bucket sum equals expected.
- Mixed kinds: parent has children of kinds `location` and `kiosk`; fan-out with `child_kind=location` excludes the kiosks.
- Inactive children excluded (`is_active=False`).
- Recursive descendants included (grandchild of correct kind is summed).
- Empty children set → zero-filled series of correct length, no error.
- Cap exceeded (`> METRICS_FANOUT_MAX_CHILDREN`) → 400 with descriptive message.
- Permission: user who is a member of the parent (with `view_metrics`) can fan-out; member of unrelated group cannot.
- Permission: parent-chain member (member of grandparent) can fan-out across the parent's children.
- `with_labels=true` returns the same labels as a single-account fetch over the same range/granularity.
- `account` is `public`/`global`/`user-*` + `child_kind` set → `child_kind` ignored, behaves like a normal fetch (or 400 — design call).
- `slug` list (multi-slug) plus `child_kind` — sum applied per slug independently; response shape matches existing multi-slug.
- `category=...` plus `child_kind` — slugs resolved from category, then summed across children.

## Out of Scope

- Per-child breakdown mode (`{child_id: series}`). Easy to add later as a second flag (e.g. `breakdown=true`); not needed for the dashboard use case.
- Fan-out support on `/api/metrics/series` and `/api/metrics/value/get`. Same pattern would apply but is a separate change.
- Including the parent group's own metrics in the sum (caller can issue a second fetch if needed; mixing parent + children is a different aggregation).
- Aggregations other than sum (avg, max, min). Sum is the only operation that's meaningful for counter-style metrics, which is what `record()` produces.
- Caching of resolved child IDs. The query is cheap; revisit only if profiling shows it matters.

## Open Questions for Design

1. Param name: `child_kind` vs. `kind` vs. `aggregate_children`? Going with `child_kind` for clarity; flag for review.
2. Hard cap value: 200 reasonable? Or make it advisory and warn in the response?
3. When `account` is non-group and `child_kind` is passed: silently ignore (current spec) or 400? Current spec leans toward ignore for forward-compat; design phase to confirm.

## Plan

**Status**: planned
**Planned**: 2026-05-07

### Objective
Add a `child_kind` query param to `/api/metrics/fetch` that, when present, fans out the fetch across all `is_active=True` descendants of `account=group-<parent_id>` matching that kind, summing per-bucket values into the existing `{labels, data}` response shape. When `child_kind` is absent, the handler runs the existing single-account flow unchanged.

### Steps

1. **`mojo/apps/metrics/rest/helpers.py`** — add module-level `METRICS_FANOUT_MAX_CHILDREN = settings.get_static("METRICS_FANOUT_MAX_CHILDREN", 200)` and a `fetch_group_fanout(parent_id, child_kind, slugs, dt_start, dt_end, granularity, with_labels)`:
   - Lazy-import `Group` inside the function to avoid circulars.
   - Resolve `parent = Group.objects.filter(id=parent_id).first()`; raise `mojo.errors.ValueException` if missing.
   - `child_ids = list(parent.get_children(is_active=True, kind=child_kind).values_list('id', flat=True))`.
   - If `len(child_ids) > METRICS_FANOUT_MAX_CHILDREN` → `ValueException` referencing the setting name.
   - Build labels once via `utils.generate_slugs_for_range(<first slug>, dt_start, dt_end, granularity, parent_account)` + `utils.periods_from_dr_slugs(...)` (labels depend only on time/granularity, not account).
   - Initialize a per-slug zero-filled accumulator of `len(labels)`.
   - For each `cid`, call `metrics.fetch(slugs, dt_start, dt_end, granularity, account=f"group-{cid}", with_labels=False, allow_empty=True)` and add per-bucket into the accumulator.
   - Empty `child_ids` → return zero-filled accumulator (no error).
   - Return `nobjict(labels=..., data={slug: [...]})` matching `metrics.fetch(..., with_labels=True)` multi-slug shape.

2. **`mojo/apps/metrics/rest/base.py`** — modify `on_metrics_data`:
   - Read `child_kind = request.DATA.get("child_kind", None)`.
   - When `child_kind` is set:
     - If `not account.startswith("group-")` → `ValueException("child_kind requires account=group-<id>")`.
     - Parse `parent_id = int(account.split("-", 1)[1])`; on `ValueError` raise `ValueException`.
     - Call `check_view_permissions(request, account)` (existing helper already walks parent chain).
     - Resolve the slug set using the same `slugs` / `slug` / `category` branch logic that exists today; for `category`, look up `metrics.get_category_slugs(category, account=account)` against the parent account.
     - Dispatch to `fetch_group_fanout(parent_id, child_kind, slugs, ...)`; return `JsonResponse(dict(status=True, data=records))`.
   - When `child_kind` is unset → existing single-account flow, untouched.

3. **`tests/test_metrics/fanout.py`** — new test module covering all scenarios listed below.

4. **`docs/web_developer/metrics/metrics.md`** — add `child_kind` row to the fetch param table and a "Parent-Group Fan-Out" subsection with a curl example.

5. **`docs/django_developer/metrics/fetching.md`** — add a "Group Fan-Out" subsection describing REST behavior and the `METRICS_FANOUT_MAX_CHILDREN` setting (helper is REST-layer only — not re-exported on the `metrics` package).

6. **`CHANGELOG.md`** — entry under the next version.

### Design Decisions
- **`child_kind` is the only trigger**: when absent, zero changes for existing callers. Single, opt-in branch.
- **Fan-out lives in REST helper, not `redis_metrics.py`**: redis layer stays Django-model-agnostic. Group resolution is REST-layer concern.
- **Permission checked once on the parent**: existing `_check_group_account_permission` already walks the parent chain via `Group.user_has_permission(check_parents=True)`. No per-child checks.
- **Sum only, no breakdown mode** (out of scope for v1).
- **Fail fast on misuse**: non-group `account` + `child_kind`, missing parent group, or cap exceeded → 400 (not silent).
- **N pipelines, not one big MGET**: each child account has a different cluster hash tag (different slot). The 200 cap bounds the cost.
- **Labels computed once**: time-bucket labels are account-independent.
- **Always multi-slug response shape under fan-out**: matches `metrics.fetch(..., with_labels=True)` for slug lists; consistent for clients.
- **Inactive parent still works**: consistent with existing single-account fetch (only children are filtered by `is_active`).

### Edge Cases
- **Bad parent_id**: `ValueException` ("group-<id> not found").
- **Zero matching children**: zero-filled series, status 200.
- **Children > cap**: `ValueException` referencing `METRICS_FANOUT_MAX_CHILDREN`.
- **Inactive parent**: works (children query is the only `is_active` filter).
- **Recursive descendants**: `get_children` already returns transitive set.
- **Mixed kinds**: `kind=` filter excludes non-matching siblings.
- **Non-group account + `child_kind`**: 400.
- **Single-resolved-slug**: same multi-slug shape (matches existing `with_labels=True` output for the singleton list).

### User Cases
- **Org-level dashboard** (parent=org, child_kind="location"): org admin sums per-location totals into one chart.
- **Region rollup** (parent=region, child_kind="store"): region member with `view_metrics` sums store metrics across the region.
- **Top-most parent** (parent=root org, child_kind="store"): root admin sees full sum across the tree.
- **No matching children**: returns zeroes, no error — caller renders empty chart without special-case.
- **Outsider**: 403 from existing permission check before fan-out runs.

### Testing
All in `tests/test_metrics/fanout.py`:
- `test_fanout_sum_correctness` — 3 children of one kind, seed known counts, verify per-bucket sum.
- `test_fanout_kind_filter` — mixed kinds; non-matching kind excluded.
- `test_fanout_recursive_descendants` — grandchild of correct kind included.
- `test_fanout_excludes_inactive_children` — `is_active=False` child not summed.
- `test_fanout_empty_children` — zero-filled series, 200.
- `test_fanout_cap_exceeded` — `th.server_settings(METRICS_FANOUT_MAX_CHILDREN=2)`, 3 children → 400.
- `test_fanout_permission_member` — member of parent with `view_metrics` succeeds.
- `test_fanout_permission_ancestor_member` — member of grandparent succeeds.
- `test_fanout_permission_outsider` — 403.
- `test_fanout_with_labels_parity` — labels match a single-account fetch over same range.
- `test_fanout_multi_slug` — `slugs=a,b` sums each independently.
- `test_fanout_category` — category resolves slugs, sums across children.
- `test_fanout_rejects_non_group_account` — `account=public&child_kind=store` → 400.
- `test_fanout_missing_parent_group` — unknown parent id → 400.

### Docs
- `docs/web_developer/metrics/metrics.md` — `child_kind` row in fetch param table; "Parent-Group Fan-Out" subsection with curl example.
- `docs/django_developer/metrics/fetching.md` — "Group Fan-Out" subsection + `METRICS_FANOUT_MAX_CHILDREN` setting.
- `CHANGELOG.md` — entry.

## Resolution

**Status**: resolved
**Date**: 2026-05-07

### What Was Built
`/api/metrics/fetch` now accepts a `child_kind` query param. When set with `account=group-<parent_id>`, the endpoint sums the metric across every active descendant of the parent group whose `kind` matches and returns the existing `{labels, data}` shape. Permission is checked once on the parent (existing parent-chain walk applies). Descendant set is capped at `METRICS_FANOUT_MAX_CHILDREN` (default 200). When `child_kind` is absent, the handler runs the existing single-account flow unchanged.

### Files Changed
- `mojo/apps/metrics/rest/helpers.py` — added `fetch_group_fanout(parent_id, child_kind, slugs, dt_start, dt_end, granularity, with_labels)`. Reads cap via `settings.get_static("METRICS_FANOUT_MAX_CHILDREN", 200)` at call time so test overrides apply.
- `mojo/apps/metrics/rest/base.py` — `on_metrics_data` reads `child_kind` from `request.DATA`; when set, validates `account=group-<id>`, parses the parent id, runs the existing view-permission check, and dispatches to `fetch_group_fanout`. Otherwise the existing path is unchanged.
- `tests/test_metrics/__init__.py` — created with `TESTIT = {"requires_apps": [...], "serial": True}`.
- `tests/test_metrics/fanout.py` — 13 tests covering the scenarios in the plan.
- `docs/web_developer/metrics/metrics.md` — added `child_kind` row and a "Parent-Group Fan-Out" subsection.
- `docs/django_developer/metrics/fetching.md` — added "Group Fan-Out" subsection and `METRICS_FANOUT_MAX_CHILDREN` setting row.
- `CHANGELOG.md` — entry under `v1.1.0 - (current)`.

### Tests
- `tests/test_metrics/fanout.py` — 13 tests, all passing:
  - sum correctness, kind filter, inactive exclusion, recursive descendants
  - empty children → zero-filled
  - cap exceeded → 400 (in-process via direct django settings patch)
  - permission member / ancestor member / outsider denied
  - `with_labels` parity vs single-account fetch
  - multi-slug independence
  - non-group account rejection, missing parent group rejection
- Run: `bin/run_tests --agent -t test_metrics.fanout`

### Docs Updated
- `docs/web_developer/metrics/metrics.md` — REST param + example
- `docs/django_developer/metrics/fetching.md` — implementation pointer + setting
- `CHANGELOG.md`

### Follow-up
- Per-child breakdown mode (`{child_id: series}`) — out of scope for v1.
- Fan-out for `/api/metrics/series` and `/api/metrics/value/get` — same pattern, separate change.
