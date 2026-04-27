# Aggregation — REST API Reference

Every list endpoint exposes a generic aggregation surface via the
`_mode` query parameter. The same URL that returns paged records also
returns `count`, top-N grouped rows, distinct values, scalar
summaries, or time-bucketed histograms — same filters, same
permissions, different output shape.

When `_mode` is **omitted**, the endpoint behaves identically to
before — paged records keyed by `data` / `count` / `size` / `start`.
Add `_mode=...` to switch to one of the aggregation responses below.

> **Reserved namespace.** Every query parameter starting with `_` is
> reserved for the framework. Do not invent your own `_*` filters —
> they will be silently ignored by the field-filter parser.

## Modes at a glance

| `_mode` | Returns | Replaces |
|---|---|---|
| `list` (default) | Current paged-records response | (no change) |
| `count` | `{count: N}` only — no rows, no `data` | The legacy `?size=0` "give me the count" hack |
| `top` | `[{key, value, ...}]` — top-N grouped by `_field`, sorted by `value` desc | "top X by Y" patterns |
| `distinct` | `[{key, value}]` for ALL distinct values of `_field` | Filter dropdown population |
| `summary` | A scalar object — `{value, min, max, n}` for an aggregate over `_agg_field` | "Average X", "Total Y" |
| `histogram` | Time-bucketed counts — `[{ts, value}]` | Activity sparklines, KPI tile trails |

## Request parameters

All filter parameters that work on `_mode=list` (e.g. `category=foo`,
`status__in=new,open`, `priority__gte=8`, `dr_start=...`) compose with
every aggregation mode. Aggregation runs *after* filtering, so a
single round-trip can pre-filter and aggregate at the same time.

| Param | Type | Used by | Description |
|---|---|---|---|
| `_mode` | enum | all | Switches response shape. Required to enable aggregation. |
| `_field` | string | `top`, `distinct`, `summary`, `histogram` | Column to group/aggregate on. |
| `_agg` | enum: `count` (default) / `sum` / `avg` / `min` / `max` | `top`, `summary` | Aggregation function applied per group. |
| `_agg_field` | string | `top` (when `_agg` ≠ `count`), `summary` (always when `_agg` ≠ `count`) | Numeric field the agg function operates on. Ignored for `_agg=count`. |
| `_bucket` | enum: `minute` / `hour` / `day` / `week` / `month` | `histogram` | Bucket size for time-series aggregation. |
| `_size` | int (default 10, hard cap 100) | `top` | Max rows. Bare `size` stays for `_mode=list`. |
| `_min_count` | int (default 1) | `top`, `distinct` | Drop rows whose `value` is below this. Filters long-tail noise. |

## Response shapes

Every aggregation response carries a `took_ms` field (rounded to the
nearest 10ms) for performance budgets. Every response also carries
`status: true` and the standard `code`/`server` envelope fields.

### `_mode=count`

```json
{
    "status": true,
    "count": 151,
    "took_ms": 0
}
```

No `data`, no `size`, no `start`. Pure scalar.

### `_mode=top`

```json
{
    "status": true,
    "graph": "top",
    "field": "source_ip",
    "agg": "count",
    "size": 10,
    "data": [
        { "key": "185.220.101.7",  "value": 84,  "first_seen": 1777200000, "last_seen": 1777267000 },
        { "key": "45.155.205.233", "value": 62,  "first_seen": 1777180000, "last_seen": 1777260000 }
    ],
    "took_ms": 10
}
```

- `value` (not `count`) so the shape is identical across `_agg=count`/`sum`/`avg`/`min`/`max`.
- `data` sorted descending by `value`.
- `key` is **always a string** (numeric/datetime fields stringified) so clients can use it as a `<select>` value or URL filter param without typecasting.
- `first_seen` / `last_seen` (unix seconds) appear when the model has a datetime column (`created` is preferred). Skipped entirely when the model has no datetime to surface.
- NULL keys appear as `key: "null"` (string).

### `_mode=distinct`

Same shape as `top` but:

- Sorted alphabetically by `key`.
- No `_size` cap — returns every distinct value.
- Hard server cap of 1000. **Cardinality > 1000 returns 400** (not silent truncation). Add filters to narrow the set.

```json
{
    "status": true,
    "graph": "distinct",
    "field": "category",
    "data": [
        { "key": "invalid_password", "value": 41 },
        { "key": "lockout",          "value":  9 },
        { "key": "login:unknown",    "value":  8 }
    ],
    "took_ms": 0
}
```

### `_mode=summary`

```json
{
    "status": true,
    "graph": "summary",
    "field": "priority",
    "agg": "avg",
    "value": 6.34,
    "min": 1,
    "max": 12,
    "n": 151,
    "took_ms": 0
}
```

- `value` is the result of `_agg` over `_agg_field` (or `_field` if `_agg_field` is omitted).
- `min` / `max` accompany numeric aggregates; `n` is always the row count of the filtered set (not the result of `_agg=count`).

### `_mode=histogram`

```json
{
    "status": true,
    "graph": "histogram",
    "field": "created",
    "bucket": "day",
    "agg": "count",
    "data": [
        { "ts": 1777094400, "value": 12 },
        { "ts": 1777180800, "value": 0 },
        { "ts": 1777267200, "value": 9 }
    ],
    "took_ms": 10
}
```

- `ts` is the UTC bucket-start in unix-seconds.
- Empty buckets in the window are present with `value: 0`. Clients never have to fill gaps client-side.
- Bucket alignment is UTC. Local-timezone alignment is not currently configurable.
- Request must include either explicit `dr_start`/`dr_end` or have a non-empty queryset; otherwise `data: []`.
- Window-relative cap: requests whose computed bucket count exceeds 10000 return 400. Pick a coarser `_bucket` and retry.

## `_size` vs `size`

`_size` (with leading underscore) only applies to `_mode=top`. The
bare `size` only applies to `_mode=list`. They never collide — the
server picks the relevant one based on `_mode`. Sending both is not
an error.

## Field validation

The server rejects aggregation requests that point at fields likely
to leak data or break the database with HTTP 400:

- `_field` referencing a relation FK without `__id` (use `incident__id`, not `incident`).
- `_field` containing `__` for non-relation fields (blocks JSON-path drilling like `metadata__rule_id`).
- `_field` of type `TextField`, `JSONField`, or `EmailField` (unbounded cardinality, PII risk).
- `_field` listed in the model's `RestMeta.SENSITIVE_FIELDS`.
- `_field` not in the model's `RestMeta.AGGREGATION_FIELDS` allow-list, when the model defines one.
- `_agg=avg` (or `sum`/`min`/`max`) on a non-numeric `_agg_field`.
- `_mode=histogram` on a non-datetime field.

## Concrete examples

```
# Top source IPs in the last 7d
GET /api/incident/event?_mode=top&_field=source_ip&_size=10&dr_start=...

# Top auth-failure source IPs — pre-filter then group
GET /api/incident/event?_mode=top&_field=source_ip&_size=10
  &category__in=invalid_password,login:unknown,totp:login_failed
  &dr_start=...

# New incidents count (replaces ?size=0)
GET /api/incident/incident?_mode=count&status=new

# Active firewall blocks
GET /api/system/geoip?_mode=count&is_blocked=true

# Status distribution donut
GET /api/incident/incident?_mode=top&_field=status&_size=20

# Filter dropdown options
GET /api/incident/event?_mode=distinct&_field=category

# Average incident priority (numeric summary)
GET /api/incident/incident?_mode=summary&_field=priority&_agg=avg&_agg_field=priority

# Events-per-day sparkline
GET /api/incident/event?_mode=histogram&_field=created&_bucket=day
  &dr_start=2026-01-01&dr_end=2026-01-31
```

## Permission model

Aggregation inherits the *exact* same permissions and per-row scoping
as `_mode=list`. There are no new permission tables, no new scopes,
and no new endpoints. A user who can only see their own incidents
gets aggregations over their own incidents — never the global set.
A user with no view permission on the resource gets the same denial
they would see on the list path.
