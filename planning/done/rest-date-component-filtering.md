# REST Date-Component Filtering (year / month / day / quarter / week)

**Type**: request
**Status**: resolved
**Date**: 2026-05-04
**Priority**: medium

## Description

Expand `mojo/models/rest.py` list filtering to support filtering by date *components* (year, month, day, quarter, week, hour) on `DateTimeField` / `DateField` columns — without forcing callers to compute a full ISO datetime range.

Two surfaces are on the table; only one will be implemented (see **Decision** below):

1. **Standard Django lookups** — `?period_start__year=2026&period_start__month=4`
2. **Sugar params on top of `dr_field`** — `?dr_field=period_start&dr_start_year=2026&dr_start_month=4&dr_end_year=2026&dr_end_month=4`

Today **neither** works:
- `dr_start` / `dr_end` only accept full datetimes (parsed by `dates.parse_datetime`); there are no `dr_*_month` / `dr_*_year` keys.
- `period_start__month=4` reaches `on_rest_list_filter` but `normalize_rest_value` sees the field's internal type is `DateTimeField` / `DateField` and runs `dates.parse_datetime("4")` against the string value, corrupting it. Even if normalization were skipped, the lookup is never explicitly allowlisted, so behavior depends on Django's ORM accepting whatever it ends up with.

## Context

A consumer (e.g. a billing/period UI) wants "show me everything in April 2026" without having to compute the start/end of the month on the client. Django's ORM has first-class `__year`, `__month`, `__day`, `__week`, `__quarter`, `__hour`, `__minute`, `__second` lookups; we just need to let them through cleanly.

The framework already exposes other Django operator suffixes (`__in`, `__not`, `__not_in`, `__isnull`) via [docs/web_developer/core/filtering.md](docs/web_developer/core/filtering.md), so surfacing the date-component lookups is a natural extension and matches the existing mental model.

## Decision (recommended)

**Go with option 1: standard Django lookups (`field__year`, `field__month`, …).**

Reasons:
- Composable with `__in`, `__not`, `__isnull`, `__gte`, `__lte` (e.g. `period_start__month__in=4,5`).
- Composable with the existing `dr_field` / `dr_start` / `dr_end` range filter (a caller can pin a year and an explicit start/end inside it).
- Zero new vocabulary to document or keep in sync — same semantics as Django docs.
- The fix is small: teach `normalize_rest_value` (and the dispatch in `on_rest_list_filter`) about numeric date-component lookups so they aren't routed through `parse_datetime`.

The `dr_*_month` / `dr_*_year` sugar is **out of scope** for this request. If a future request shows real ergonomic value we can revisit, but it's reinventing Django built-ins.

## Acceptance Criteria

- `GET /api/<resource>?period_start__year=2026` returns rows where `EXTRACT(YEAR FROM period_start) = 2026`.
- Supported lookup suffixes (must all work on both `DateTimeField` and `DateField`):
  - `__year`, `__month`, `__day`, `__week`, `__week_day`, `__iso_week_day`, `__quarter`, `__hour`, `__minute`, `__second`
- Composes with existing operators:
  - `period_start__month__in=4,5,6` (multiple months)
  - `period_start__month__not=12` (exclusion)
  - `period_start__year=2026&period_start__month=4` (AND of components)
  - `dr_field=period_start&dr_start=2026-01-01&period_start__month=4` (range + component)
- Values are coerced to `int` (1-based for month/quarter/week/day, 0-23 for hour, etc.); strings like `"4"` are accepted.
- Invalid integers (`?period_start__month=foo`) return a clean 400, not a stack trace.
- Bare numeric-component requests on a non-date field still work (e.g. `?count__gte=5` continues to behave as before — no regression on existing integer / boolean / FK filtering).
- `dr_start` / `dr_end` continue to behave exactly as today (no change to that surface).
- New behavior is documented in [docs/web_developer/core/filtering.md](docs/web_developer/core/filtering.md) and a short note in [docs/django_developer/core/mojo_model.md](docs/django_developer/core/mojo_model.md).

## Investigation

**What exists**:
- `on_rest_list_date_range_filter` ([mojo/models/rest.py:711-738](mojo/models/rest.py:711)) — `dr_field` / `dr_start` / `dr_end` with `dates.parse_datetime`.
- `on_rest_list_filter` ([mojo/models/rest.py:765-848](mojo/models/rest.py:765)) — generic field filter with operator suffixes (`__in`, `__not`, `__not_in`, `__isnull`).
- `normalize_rest_value` ([mojo/models/rest.py:741-762](mojo/models/rest.py:741)) — coerces values per field internal type. **This is the piece that currently breaks `__month` / `__year`** by force-parsing the value as a datetime when the underlying field is a date/datetime.

**What changes**:
- `mojo/models/rest.py`:
  - In `on_rest_list_filter`, after `key_parts = key.split('__')`, detect when the trailing lookup is one of the date-component suffixes (`year`, `month`, `day`, `week`, `week_day`, `iso_week_day`, `quarter`, `hour`, `minute`, `second`) and route normalization to **integer** coercion instead of date parsing.
  - In `normalize_rest_value`, accept an optional "lookup hint" (or just accept the full key) so it knows when the value should be parsed as int even though the field is `DateTimeField`/`DateField`. Compose with `__in` (split on comma → list of ints) and `__not` / `__not_in` (route to excludes dict, same as today).
  - Wrap int coercion in a try/except that returns a 400 via `cls.rest_error_response` (matches existing error-style for malformed filters; see how `dr_start` parse failures behave today).
- `docs/web_developer/core/filtering.md` — add a "Date-Component Filters" subsection under "Date Range Filter".
- `docs/django_developer/core/mojo_model.md` — one-line callout next to the existing list of reserved query params.
- `CHANGELOG.md` — note the new supported lookups.

**Constraints**:
- Backwards compatibility: existing `dr_start` / `dr_end` and the standard operator suffixes must keep behaving identically. The fix targets a code path that currently fails — no working request should change result.
- Permissions: this is purely a filter-parser change; permission and group-scoping logic ([mojo/models/rest.py:619-652](mojo/models/rest.py:619)) is untouched.
- Performance: date-component lookups in Postgres can bypass plain B-tree indexes on the column. Document that for very large tables a functional index (e.g. `CREATE INDEX ... ON (EXTRACT(MONTH FROM period_start))`) may be needed, but **don't** auto-create indexes — leave that to the model author. See `.claude/rules/performance.md`.
- Reserved-prefix rule (`_`-prefixed keys are framework-only) is unaffected.

**Related files**:
- [mojo/models/rest.py](mojo/models/rest.py) — `on_rest_list_filter`, `normalize_rest_value`, `on_rest_list_date_range_filter`
- [docs/web_developer/core/filtering.md](docs/web_developer/core/filtering.md)
- [docs/django_developer/core/mojo_model.md](docs/django_developer/core/mojo_model.md)
- [CHANGELOG.md](CHANGELOG.md)

## Endpoints

No new endpoints. Behavior change to every list endpoint that goes through `MojoModel.on_rest_request` / `on_rest_list`.

## Settings

None.

## Tests Required

In `tests/test_models/` (or wherever existing rest-filter tests live — confirm during build):

- `period_start__year=2026` returns only rows in 2026.
- `period_start__month=4` returns only rows in April (any year).
- `period_start__year=2026&period_start__month=4` AND-composes correctly.
- `period_start__month__in=4,5` returns rows in April or May.
- `period_start__month__not=12` excludes December rows.
- `period_start__quarter=2`, `period_start__week=15`, `period_start__day=1`, `period_start__hour=14` each work.
- Works on a `DateField` and a `DateTimeField`.
- Composes with existing `dr_start` / `dr_end` (range × component).
- `period_start__month=foo` → 400 with a useful message, not a 500.
- Regression: existing `dr_start`/`dr_end` behavior on the same model is unchanged (snapshot a few existing tests).
- Regression: existing `__in` / `__not` / `__isnull` on non-date fields still works.

## Out of Scope

- `dr_start_month` / `dr_start_year` / `dr_end_month` / `dr_end_year` sugar params — explicitly rejected in favor of the standard Django lookups (see **Decision**).
- Auto-creation of functional indexes for date-component filters — model authors handle their own indexing.
- Aggregation surface (`_mode=...`) — unrelated; date-component grouping there is its own feature.
- Partial-date with `__in` / `__not` operators (e.g. `?created__not=2026-04`) — see [planning/requests/rest-date-partial-operator-compose.md](rest-date-partial-operator-compose.md) for the future request.

## Plan

**Status**: planned
**Planned**: 2026-05-04

### Objective

Add three composable date-filter surfaces to `MojoModel.on_rest_list_*`: (A) standard Django component lookups (`__year`, `__month`, …), (B) partial-date field shorthand (`?created=2026-04`), and (C) partial-date support inside the existing `dr_start`/`dr_end` range filter — sharing one `parse_partial_date` helper and honoring the existing `timezone` request param for correct local-time semantics.

### Steps

1. **`mojo/helpers/dates.py`** — add `parse_partial_date(value)`:
   - Regex: `^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$` — 4-digit year required, 1- or 2-digit month/day.
   - Returns `objict({year, month, day})` with `None` for missing components, or `None` if no match (anything with a `T`, time component, or non-digit chars falls through).
   - Add `partial_date_to_range(parts, timezone=None)` returning `(start_utc, end_utc)` datetimes:
     - `{year:2026}` → `2026-01-01T00:00:00 .. 2026-12-31T23:59:59.999999`
     - `{year:2026,month:4}` → `2026-04-01T00:00:00 .. 2026-04-30T23:59:59.999999` (use `calendar.monthrange`)
     - `{year:2026,month:4,day:2}` → `2026-04-02T00:00:00 .. 2026-04-02T23:59:59.999999`
     - When `timezone` provided, build the bounds in that local zone, then convert to UTC.

2. **`mojo/models/rest.py`** — module-level constant:
   ```python
   _DATE_COMPONENT_LOOKUPS = {
       "year","iso_year","month","day","week","week_day","iso_week_day",
       "quarter","hour","minute","second"
   }
   ```

3. **`mojo/models/rest.py:741` `normalize_rest_value`** — accept new `lookup=None` kwarg:
   - When `lookup in _DATE_COMPONENT_LOOKUPS`: coerce value to `int(value)` (or `[int(v) for v in value]` if list); on failure raise `me.ValueException(f"Invalid value for {field_name}__{lookup}: {value!r}", code=400, status=400)`.
   - Skip the existing datetime-parse branch in this case.
   - Existing callers pass no `lookup` → behavior unchanged.

4. **`mojo/models/rest.py:765` `on_rest_list_filter`** — non-relation branch (line ~823):
   - Compute `trailing = key_parts[-1] if len(key_parts) > 1 else None`.
   - **Component-lookup path**: if `trailing in _DATE_COMPONENT_LOOKUPS`, route through `normalize_rest_value(..., lookup=trailing)`. `__in` / `__not` / `__not_in` compose normally (split on comma → list-of-int).
   - **Partial-date shorthand path**: if there is no operator suffix AND field is `DateField`/`DateTimeField` AND `parse_partial_date(value)` returns a match → call `partial_date_to_range(parts, timezone=request.DATA.get("timezone") or getattr(request.group, "timezone", None))` and add `{f"{field_name}__gte": start, f"{field_name}__lte": end}` to `filters` dict; skip the normal exact-match path. (Range form, not component lookups, so it's TZ-correct.)
   - All other paths unchanged.

5. **`mojo/models/rest.py:711` `on_rest_list_date_range_filter`** — before each `dates.parse_datetime` call:
   - `parts = dates.parse_partial_date(dr_start)`. If matched: use `partial_date_to_range(parts, timezone=...)[0]` for `dr_start` (start-of-period) and `[1]` for `dr_end` (end-of-period). The timezone preference order: `request.DATA.get("timezone")` → `request.group.timezone` → UTC.
   - If `parse_partial_date` returns `None` → existing `dates.parse_datetime` path unchanged.
   - Drop the existing `request.group.get_local_time(dr_start)` call when partial-date matched (the helper already produces UTC); keep it on the full-datetime branch for backwards compat.

6. **`tests/test_models/date_filtering.py`** — new test file (see Testing).

7. **`docs/web_developer/core/filtering.md`** — add "Date-Component Filters" subsection under "Date Range Filter" with the lookup table, partial-date shorthand, `dr_start`/`dr_end` partial behavior, and the timezone caveat for `__month`/`__year` lookups.

8. **`docs/django_developer/core/mojo_model.md`** — one-line callout next to the existing reserved-query-params list.

9. **`CHANGELOG.md`** — entry under unreleased.

10. **`planning/requests/rest-date-partial-operator-compose.md`** — stub future-request: support `?created__not=2026-04` / `?created__in=2026-04,2026-05` (requires Q-object dispatch, not the flat filters/excludes dict pattern).

### Design Decisions

- **One regex, one helper**: `parse_partial_date` is the only place that decides "is this a partial date." All three surfaces call it. Cheap to test, impossible to get out of sync.
- **Partial-date shorthand expands to range, not component lookups**: `?created=2026-04` becomes `__gte=2026-04-01T00:00:00&__lte=2026-04-30T23:59:59` (in user TZ → UTC). This sidesteps Django's DB-TZ-only `__month`/`__year` lookups and gives correct results when the DB is UTC but the user is filtering in their own zone. Matches the semantics callers naturally expect.
- **Explicit `__month=4` stays DB-TZ**: Documented limitation. Users who need local-time month buckets use the shorthand. Adding TZ support to component lookups would require Django 4.0 `Trunc(tzinfo=)` rewrites — invasive, out of scope.
- **`timezone` request param reused**: Already an established convention for CSV localization at [mojo/models/rest.py:688](mojo/models/rest.py:688). No new vocabulary.
- **Bad component value → `ValueException(code=400)`**: Matches how malformed `dr_start` already fails today (parse error bubbles up). Fail loudly so callers see typos, not silently empty results.
- **`__in`/`__not` partial-date deferred to future request**: Composing `__not=2026-04` (= "exclude April 2026") cleanly requires Q-object dispatch in the excludes path, not the flat `excludes` dict. Not worth the refactor in v1; explicit `__year=2026&__month__not=4` works today.
- **Two-digit year rejected**: Regex requires `\d{4}`. `"26-04"` → `None` → falls through to existing parser → fails noisily. Avoids "20" vs "19" century-guessing.

### User Cases

| Use case | Request |
|---|---|
| All of April 2026 | `?period_start=2026-04` |
| All of 2026 | `?period_start=2026` or `?period_start__year=2026` |
| April or May 2026 | `?period_start__year=2026&period_start__month__in=4,5` |
| Anything except December | `?period_start__month__not=12` |
| Q2 2026 | `?period_start__quarter=2&period_start__year=2026` |
| Specific day | `?period_start=2026-04-02` |
| Mixed-precision range | `?dr_field=period_start&dr_start=2026-04-15&dr_end=2026-04` |
| Local-time April for PT user | `?period_start=2026-04&timezone=America/Los_Angeles` |

### Edge Cases

- **CharField that looks like a date** (e.g. `?slug=2026-04`): partial-date expansion gated on `field.get_internal_type() in ("DateField","DateTimeField")`. Unchanged exact match.
- **Full ISO datetime** (`?created=2026-04-02T10:00:00Z`): regex doesn't match (`T` present) → existing datetime-parse path unchanged.
- **Bad component int** (`?created__month=foo`): `int("foo")` → `ValueException(code=400)`.
- **Out-of-range component** (`__month=13`): pass through to Django; returns empty result. Not our job to range-validate.
- **Leap years**: `calendar.monthrange(2024,2)` returns 29; `(2025,2)` returns 28. Handled.
- **Two-digit year** (`"26-04"`): regex rejects → falls through → noisy parse error.
- **Partial-date with `__in` or `__not`**: not matched by shorthand path (only fires on bare exact-match key). Falls through to existing logic, which on a date field would currently misbehave — log and document as v2.
- **Group timezone vs request timezone**: precedence is `request.DATA.timezone` → `request.group.timezone` → UTC. Matches CSV localization precedence.
- **Existing `dr_start` with full ISO + `request.group`**: backwards-compat preserved on the non-partial branch — still applies `request.group.get_local_time`.

### Testing

New file `tests/test_models/date_filtering.py`. Use `shortlink.ShortLink` (existing test host with a `created` `DateTimeField`) plus a `description` `CharField` for the regression case. Setup creates ~6 fixture rows and `ShortLink.objects.filter(pk__in=[...]).update(created=...)` to backdate them across years/months/days. Cleanup deletes test rows by `code__startswith="datefilt"`.

| Scenario | Expectation |
|---|---|
| `created__year=2026` | only 2026 rows |
| `created__month=4` | April rows across years |
| `created__year=2026&created__month=4` | AND-composes |
| `created__month__in=4,5` | April + May rows |
| `created__month__not=12` | excludes December |
| `created__quarter=2` / `__week=15` / `__day=1` / `__hour=14` | each works |
| `created=2026-04` shorthand | same result set as `__year=2026&__month=4` |
| `created=2026` shorthand | full year |
| `created=2026-04-02` shorthand | single day inclusive |
| `dr_start=2026-04&dr_end=2026-04` | April 2026 inclusive |
| `dr_start=2026` | covers full year start |
| `dr_start=2026-04-02&dr_end=2026-04-02` | that day inclusive |
| `created__month=foo` | 400 with descriptive message |
| `created=2026-04&timezone=America/Los_Angeles` | range bounds reflect PT |
| Regression: `dr_start=2026-04-15T10:00:00Z` | full ISO still works |
| Regression: `created=2026-04-15T10:00:00Z` | exact-match still works |
| Regression: `description=2026-04` (CharField) | unchanged exact match |

### Docs

- `docs/web_developer/core/filtering.md` — new "Date-Component Filters" subsection (component lookup table, partial-date shorthand, `dr_start`/`dr_end` partial-date expansion, timezone caveat for explicit `__month`/`__year`).
- `docs/django_developer/core/mojo_model.md` — one-line addition to the reserved-query-params surface.
- `CHANGELOG.md` — unreleased entry.

## Resolution

**Status**: resolved
**Date**: 2026-05-04

### What Was Built

Three composable date-filter surfaces on `MojoModel.on_rest_list_*`, sharing one `parse_partial_date` helper:

1. **Standard Django component lookups** — `?created__year=2026`, `?created__month=4`, `__quarter`, `__day`, `__week`, `__hour`, etc. Compose with `__in` / `__not`.
2. **Partial-date field shorthand** — `?created=2026-04` (or `2026`, or `2026-04-02`) expands to a tz-aware UTC `__gte` / `__lte` range. Only on `DateField` / `DateTimeField`; CharFields are unchanged.
3. **Partial-date `dr_start` / `dr_end`** — same forms expand to start/end of period. Full-ISO behavior preserved.

Timezone resolution: `request.DATA.timezone` → `request.group.timezone` → UTC. Reuses the `timezone` request param already used for CSV localization. Invalid components / out-of-range partials raise `me.ValueException(code=400)`.

### Files Changed

- `mojo/helpers/dates.py` — added `parse_partial_date()` and `partial_date_to_range()` plus `_PARTIAL_DATE_RE` regex; imported `calendar` and `re`.
- `mojo/models/rest.py` — added `_DATE_COMPONENT_LOOKUPS` constant, `_DATE_FIELD_TYPES` constant, `_resolve_filter_timezone()` classmethod; `normalize_rest_value()` now accepts a `lookup=` kwarg and skips datetime parsing when the lookup is a date component; `on_rest_list_filter()` detects partial-date shorthand on date fields and component lookups in any operator-suffix position; `on_rest_list_date_range_filter()` accepts partial dates and expands them tz-aware.
- `tests/test_models/date_filtering.py` — new test module, 19 tests.
- `docs/web_developer/core/filtering.md` — new "Date-Component Filters" + "Partial-Date Shorthand" + "Timezone" subsections; partial-date documented under "Date Range Filter".
- `docs/django_developer/core/mojo_model.md` — added `timezone` to reserved bare params; one-paragraph callout pointing to the consumer-facing reference.
- `CHANGELOG.md` — entry under v1.1.0.

### Tests

- `tests/test_models/date_filtering.py` — 19 tests covering: each component lookup, AND-composition, `__in`/`__not` composition, partial-date shorthand at all three precisions, tz-aware expansion (PT user, April-1 UTC row excluded), `dr_start`/`dr_end` partial dates, full-ISO regression, CharField regression, exact-match-with-time regression, 400 on bad component value, 400 on out-of-range partial.
- Run: `bin/run_tests --agent -t test_models.date_filtering` — 19/19 pass.

### Docs Updated

- `docs/web_developer/core/filtering.md` — new sections.
- `docs/django_developer/core/mojo_model.md` — reserved-params update + cross-link.

### Follow-up

- [planning/requests/rest-date-partial-operator-compose.md](rest-date-partial-operator-compose.md) — partial-date `__in` / `__not` / `__not_in` composition (deferred to a separate request; needs Q-object dispatch, not the flat filters/excludes dict).
