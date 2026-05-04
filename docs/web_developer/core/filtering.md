# Filtering, Searching & Sorting — REST API Reference

All list endpoints support these query parameters.

> **Reserved prefix.** Query parameters starting with `_` are reserved for the framework (aggregation, pagination controls, etc.) and are silently skipped by the field-filter parser. Use the bare param name without `_` for field filters. See [Aggregation](aggregation.md) for the full `_mode` surface.

## Field Filters

Filter by exact field value:

```
GET /api/myapp/book?status=published
GET /api/myapp/book?author=5
```

### Filter Operators

Append an operator suffix to the field name:

| Suffix | Example | Behavior |
|---|---|---|
| *(none)* | `?status=active` | Exact match |
| `__in` | `?status__in=active,pending` | Matches any value in comma-separated list |
| `__not` | `?status__not=deleted` | Excludes this value |
| `__not_in` | `?status__not_in=deleted,archived` | Excludes all values in list |
| `__isnull` | `?author__isnull=true` | NULL check |

```
GET /api/myapp/book?status__in=published,draft
GET /api/myapp/book?author__isnull=false
GET /api/myapp/book?status__not=deleted
```

### Null Values

```
GET /api/myapp/book?author=null
```

### Relation / Foreign Key Filters

Filter through related fields using `__` notation:

```
GET /api/myapp/book?author__id=5
GET /api/myapp/book?author__username=alice
```

## Date Range Filter

```
GET /api/myapp/book?dr_start=2024-01-01&dr_end=2024-12-31
GET /api/myapp/book?dr_field=modified&dr_start=2024-06-01
```

| Param | Description |
|---|---|
| `dr_field` | Field to filter on (default: `created`) |
| `dr_start` | Start datetime (inclusive) |
| `dr_end` | End datetime (inclusive) |

Dates accept ISO 8601: `2024-01-01` or `2024-01-01T00:00:00Z`.

### Partial Dates in `dr_start` / `dr_end`

`dr_start` and `dr_end` also accept partial dates — `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` — and expand them to the start / end of the period:

```
GET /api/myapp/book?dr_start=2026-04&dr_end=2026-04   # all of April 2026
GET /api/myapp/book?dr_start=2026                     # from 2026-01-01 onward
GET /api/myapp/book?dr_start=2026-04-02&dr_end=2026-04-02   # that whole day
```

| Input | `dr_start` expands to | `dr_end` expands to |
|---|---|---|
| `2026` | `2026-01-01T00:00:00` | `2026-12-31T23:59:59.999999` |
| `2026-04` | `2026-04-01T00:00:00` | `2026-04-30T23:59:59.999999` |
| `2026-04-02` | `2026-04-02T00:00:00` | `2026-04-02T23:59:59.999999` |

Bounds are anchored in the request's timezone (see [Timezone](#timezone) below). Out-of-range months / days return `400`.

## Date-Component Filters

Filter by individual components of a `DateField` or `DateTimeField` using Django's standard component lookups:

```
GET /api/myapp/book?created__year=2026
GET /api/myapp/book?created__month=4
GET /api/myapp/book?created__year=2026&created__month=4   # AND
GET /api/myapp/book?created__quarter=2
GET /api/myapp/book?created__day=15
```

Supported lookup suffixes:

| Suffix | Range | Notes |
|---|---|---|
| `__year` | any | calendar year |
| `__iso_year` | any | ISO 8601 week-numbering year |
| `__month` | 1–12 | |
| `__day` | 1–31 | day of month |
| `__week` | 1–53 | ISO week number |
| `__week_day` | 1–7 | DB-dependent (1=Sunday on most backends) |
| `__iso_week_day` | 1–7 | 1=Monday |
| `__quarter` | 1–4 | |
| `__hour` | 0–23 | |
| `__minute` | 0–59 | |
| `__second` | 0–59 | |

Component lookups compose with `__in` and `__not`:

```
GET /api/myapp/book?created__month__in=4,5,6   # April, May, OR June
GET /api/myapp/book?created__month__not=12     # exclude December
```

Invalid values (`?created__month=foo`) return `400`.

> **Timezone caveat.** Component lookups (`__year`, `__month`, …) run in the database connection's timezone (typically UTC). For a user-local "all of April" filter, prefer the **partial-date shorthand** below, which expands to a tz-aware UTC range.

### Partial-Date Shorthand

On a `DateField` / `DateTimeField`, the bare exact-match operator accepts a partial date and expands to a tz-aware range:

```
GET /api/myapp/book?created=2026-04        # all of April 2026 (in request tz)
GET /api/myapp/book?created=2026           # all of 2026
GET /api/myapp/book?created=2026-04-02     # that whole day
```

Internally this becomes `created__gte=...&created__lte=...` with the bounds anchored to the request timezone, so a user in `America/Los_Angeles` filtering `created=2026-04` gets PT-April, not UTC-April.

The shorthand only applies to the bare exact-match key. Composing with `__in` / `__not` is not supported in v1 — use component lookups for those cases.

CharFields and other non-date fields keep their existing exact-match behavior even when the value looks like a date.

### Timezone

`dr_start` / `dr_end` partial-date expansion and the partial-date field shorthand both pick a timezone in this order:

1. `?timezone=<tz>` (or in POST body) — IANA name, e.g. `America/Los_Angeles`
2. The request group's timezone (`request.group.timezone`)
3. UTC

The `timezone` param uses the same key already used for CSV download localization.

## Text Search

```
GET /api/myapp/book?search=django
```

### Advanced Search Syntax

| Syntax | Example | Behavior |
|---|---|---|
| Single term | `search=django` | Matches any search field containing "django" |
| Multiple terms | `search=django rest` | AND: both terms must match |
| Quoted phrase | `search="django rest"` | Exact phrase match |
| Exclusion | `search=django -python` | Contains "django" but not "python" |
| Field-specific | `search=title:django` | Search only in `title` field |

```
GET /api/myapp/book?search="machine learning" -python author:alice
```

## Sorting

```
GET /api/myapp/book?sort=title
GET /api/myapp/book?sort=-created    # descending (prefix with -)
```

Default sort is `-id` (newest first) unless the model specifies otherwise.

## Combining Parameters

```
GET /api/myapp/book?status=published&sort=-created&search=django&dr_start=2024-01-01&start=0&size=20
```
