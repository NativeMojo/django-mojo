# Partial-Date Composition with `__in` / `__not` / `__not_in`

**Type**: request
**Status**: open
**Date**: 2026-05-04
**Priority**: low

## Description

Extend the partial-date shorthand introduced in [rest-date-component-filtering.md](rest-date-component-filtering.md) to compose with the framework's exclusion / multi-value operators:

- `?created__not=2026-04` — exclude all of April 2026
- `?created__in=2026-04,2026-05` — April OR May 2026
- `?created__not_in=2026,2025` — exclude all of 2026 and 2025

Today (post-v1) only the bare exact-match form is supported (`?created=2026-04`). Adding operator composition turns each value into a date range, which means the flat `filters` / `excludes` dict pattern in `on_rest_list_filter` is no longer sufficient — multiple ranges must be OR'd, then the whole expression negated for `__not`/`__not_in`.

## Context

Came up while designing the v1 component-filtering surface. Deferred because:
- The `excludes` dict is a flat `**kwargs` to `.exclude()`. Multi-range expansion needs `Q()` objects.
- v1 covers the high-frequency cases (`?created=2026-04` for "show me April", `__month__in=4,5` for multi-month) without requiring this refactor.

Worth picking up if a real consumer asks for "exclude these N months" or "show me records from these N months" using the more compact partial-date syntax.

## Acceptance Criteria

- `?created__not=2026-04` excludes the April-2026 date range (inclusive bounds, timezone-aware via the existing `timezone` request param).
- `?created__in=2026-04,2026-05` returns rows in April OR May 2026 (inclusive bounds).
- `?created__not_in=2026-04,2026-05` excludes rows in those ranges.
- Mixed precision in a single list: `?created__in=2026,2026-04-15` works (full year OR single day).
- Regressions: existing `__in` / `__not` / `__not_in` on integer / string / FK fields unchanged. Existing partial-date exact match unchanged.

## Investigation

**What exists** (after v1 lands):
- `parse_partial_date` and `partial_date_to_range` helpers in `mojo/helpers/dates.py`.
- `on_rest_list_filter` builds flat `filters` and `excludes` dicts; `partial_date_to_range` is currently called only on the bare-key path.

**What changes**:
- `mojo/models/rest.py` `on_rest_list_filter`: when the key has `__in`/`__not`/`__not_in` AND the field is `DateField`/`DateTimeField` AND each comma-split value matches `parse_partial_date`, build a `Q()` expression instead of writing to the flat dict. Apply via `queryset.filter(Q(...))` / `queryset.exclude(Q(...))`.
- Mixed populations (some values partial-date, some not) on the same key — error or fall through? Probably error with a 400.

**Constraints**:
- Don't slow down the common path. Only build `Q()` objects when partial-date is detected.
- Keep `__in` / `__not` semantics intact for non-date fields.

**Related files**:
- `mojo/models/rest.py` — `on_rest_list_filter` (post-v1 layout)
- `mojo/helpers/dates.py` — `parse_partial_date`, `partial_date_to_range`

## Tests Required

- `created__not=2026-04` excludes April 2026 inclusive
- `created__in=2026-04,2026-05` returns April + May rows
- `created__not_in=2025,2026` excludes both years
- Mixed precision: `created__in=2026-04,2027-01-15` → April 2026 OR Jan 15 2027
- Timezone-aware: `created__not=2026-04&timezone=America/Los_Angeles` excludes PT-April
- Regression: `status__in=open,closed` still works on a CharField
- Regression: `author__not=5` still works on an FK

## Out of Scope

- New operator suffixes beyond `__in` / `__not` / `__not_in`.
- Changes to component lookups (`__year`, `__month`, …) — those already compose with `__in`/`__not` in v1.
- Cross-field date math.
