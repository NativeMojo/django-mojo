# List endpoint aggregation modes (`_mode=count|top|distinct|summary|histogram`)

**Type**: request
**Status**: planned
**Date**: 2026-04-27
**Originating client**: web-mojo Security Dashboard build (`planning/requests/security-dashboard.md`)

## Description

Add a generic aggregation surface to **every** mojo CRUD list endpoint
via a `_mode` query parameter. The existing list endpoint (`GET
/api/incident/event`, `GET /api/incident/incident`, `GET
/api/system/geoip`, etc.) keeps its current paged-records behavior
when `_mode` is absent. With `_mode` set, the same endpoint returns
aggregated rows or scalars instead of records — same filters, same
permissions, different output shape.

This unblocks the Security Dashboard's Top Sources panel + every
"top N" / "current count" pattern across the framework, replaces the
existing `size=0` "give me the count" hack, and gives every model in
the system a consistent aggregation API for free as it's added.

## Why this rather than purpose-built endpoints

The Security Dashboard initially asked for two bespoke endpoints
(`/api/incident/event/top_source_ips`,
`/api/incident/event/top_categories`). That works for one panel, but
every future feature will want similar shapes against different
resources — top failed-job runners, top file uploaders, top firewall
countries, top SMS-bounce reasons, etc. Bespoke endpoints don't
scale; a generic surface does.

The Mojo metrics API already has aggregated shapes
(`/api/metrics/series` for point-in-time, `/api/metrics/fetch` for
time-series buckets). Bringing the same idea to CRUD list endpoints
keeps the API consistent across the whole framework.

## Why the `_` prefix

Today filter params on list endpoints come bare (`?status=new`,
`?priority__gte=8`). New aggregation params are `_`-prefixed so they
can never collide with a model field name. A model that legitimately
has a `mode` or `field` column would otherwise become un-filterable
on those columns under the proposed change.

**Existing framework-reserved params (`size`, `start`, `sort`,
`dr_start`, `dr_end`, `graph`) stay bare** — they've been in
production for a long time, and renaming them is churn for no real
benefit. Only the brand-new aggregation surface uses the `_` prefix.

## Modes

| `_mode` | Returns | Replaces |
|---|---|---|
| `list` (default — equivalent to omitting the param) | Current paged-records response | (no change) |
| `count` | `{count: N}` only — no rows, no `data` array | The existing `size=0` / `size=1` "ignore the rows, read `count`" pattern |
| `top` | `[{key, value, ...}]` — top-N grouped by `_field`, sorted by `value` desc | Bespoke "top X by Y" endpoints |
| `distinct` | `[{key, value}]` for ALL distinct values of `_field` (no top-N cap) | Filter-dropdown population, small-cardinality groupings |
| `summary` | A scalar object — `{value, min, max, n}` for an aggregate function over `_agg_field` | "Average response time", "Total bytes blocked" |
| `histogram` | Time-bucketed counts — `[{ts, value}]` | Activity sparklines, KPI tile trails (where the existing metrics API isn't a fit) |

## Request parameters

All `_`-prefixed except where noted:

| Param | Type | Used by | Description |
|---|---|---|---|
| `_mode` | enum | all | Switches response shape. Required to enable any aggregation mode. |
| `_field` | string | `top`, `distinct`, `summary`, `histogram` | Column to group/aggregate on |
| `_agg` | enum: `count` (default) / `sum` / `avg` / `min` / `max` | `top`, `summary` | Aggregation function applied per group (or per response in summary) |
| `_agg_field` | string | `top` (when `_agg` ≠ `count`), `summary` (always) | Field the agg function operates on. Ignored for `_agg=count`. |
| `_bucket` | enum: `minute` / `hour` / `day` / `week` / `month` | `histogram` | Bucket size for time-series aggregation |
| `_size` | int (default 10, hard cap 100) | `top` | Max rows to return. Bare `size` stays for `_mode=list`. |
| `_min_count` | int (default 1) | `top`, `distinct` | Drop rows below this count to filter long-tail noise |
| All existing filters | — | every mode | Pre-filter records BEFORE aggregating. `?_mode=top&_field=source_ip&category__in=invalid_password,login:unknown` is one round-trip — this is the killer feature. |
| All existing pagination/sort/window params (`size`, `start`, `sort`, `dr_start`, `dr_end`) | — | `list` only | Stay bare, unchanged behavior |

### `_size` vs `size`

`_size` and `size` are different params. The bare `size` only makes
sense for `_mode=list` (page size). `_size` only makes sense for
`_mode=top` (max top-N rows). Server should ignore the irrelevant
one based on `_mode`. If both are sent (caller mistake), prefer
`_size` when `_mode != list`.

## Response shapes

### `_mode=count`

```json
{
    "status": true,
    "count": 151
}
```

No `data`, no `size`, no `start`, no `graph`. Pure scalar.

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
        { "key": "45.155.205.233", "value": 62,  "first_seen": 1777180000, "last_seen": 1777260000 },
        { "key": "194.26.135.18",  "value": 41,  "first_seen": 1777190000, "last_seen": 1777265000 }
    ]
}
```

- `value` (not `count`) so the shape is identical across `_agg=count`/`sum`/`avg`/`min`/`max`.
- `data` sorted descending by `value`.
- `key` is always a string (numeric/datetime fields stringified) so clients can use it as a `<select>` value or a URL filter param without typecasting.
- **Optional but very useful:** `first_seen` / `last_seen` (unix seconds) when grouping records that have a timestamp column. Lets the dashboard show "X events from this IP since 2 days ago" without a second fetch. Skip the fields entirely when the model has no datetime to surface.

### `_mode=distinct`

Same shape as `top` but:
- Sorted alphabetically by `key` (not by `value`).
- No `_size` cap — returns every distinct value (server-side hard cap of e.g. 1000 to prevent runaway queries; cap exceeded → 400 error rather than silent truncation).
- `graph: "distinct"`.

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
    "n": 151
}
```

- `value` is the result of `_agg` over `_agg_field` (or `_field` if `_agg_field` is omitted).
- `min` / `max` / `n` always included for context, regardless of `_agg`.
- `n` is the count of records matched by filters (NOT the result of `_agg=count`).

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
        { "ts": 1777180800, "value": 18 },
        { "ts": 1777267200, "value": 9 }
    ]
}
```

- `ts` is the bucket-start unix-seconds.
- Mirrors `/api/metrics/fetch` shape — same chart components can render either.
- Empty buckets in the window MUST be present with `value: 0` — clients shouldn't have to fill gaps client-side.

## Permission model

Whoever can `GET` the list endpoint can call any `_mode` against it.
No new permissions needed.

The aggregation runs over the same record set the user would see in
`_mode=list`, so per-row visibility filters (group scope, ownership,
status filters) automatically apply to the aggregation. A user who
can only see their own incidents should get aggregations over their
own incidents, not the global set.

## Concrete examples (Security Dashboard)

```
# Top source IPs in the last 7d
GET /api/incident/event?_mode=top&_field=source_ip&_size=10&dr_start=...

# Top categories — same endpoint, just different field
GET /api/incident/event?_mode=top&_field=category&_size=10&dr_start=...

# Top auth-failure source IPs — pre-filter then group
GET /api/incident/event?_mode=top&_field=source_ip&_size=10
  &category__in=invalid_password,login:unknown,totp:login_failed
  &dr_start=...

# New incidents count (replaces size=0 hack)
GET /api/incident/incident?_mode=count&status=new

# Active firewall blocks (replaces size=0 hack)
GET /api/system/geoip?_mode=count&is_blocked=true

# Status distribution for the donut (replaces fetching 200 rows + client agg)
GET /api/incident/incident?_mode=top&_field=status&_size=20

# Priority bucket distribution
GET /api/incident/incident?_mode=top&_field=priority&_size=20

# Filter dropdown options for the events table
GET /api/incident/event?_mode=distinct&_field=category
```

The Security Dashboard's Top Sources, Distributions, and several KPI
tiles all collapse onto this one consistent surface.

## Acceptance Criteria

- [ ] `_mode=count` works on every list endpoint. Returns
  `{status, count}` only. Respects all existing filters.
- [ ] `_mode=top` works on every list endpoint. Required: `_field`.
  Sorts by `value` desc. Honors `_size` (default 10, cap 100) and
  `_min_count` (default 1). Respects all existing filters.
- [ ] `_mode=distinct` works on every list endpoint. Required:
  `_field`. Sorts by `key` asc. Hard-caps server-side at 1000 with
  a 400 error if exceeded.
- [ ] `_mode=summary` works on every list endpoint. Required:
  `_field` (or `_agg_field` when `_agg` ≠ `count`). Returns
  `{value, min, max, n}` per the shape above.
- [ ] `_mode=histogram` works on every list endpoint that has a
  datetime column. Required: `_field` (the datetime column),
  `_bucket`. Empty buckets in the window are present with `value: 0`.
- [ ] `_mode` defaults to `list`; existing API behavior unchanged
  when `_mode` is absent. **Backwards compatibility is mandatory.**
- [ ] All existing filter params (`category__in`, `status`,
  `priority__gte`, `dr_start`, etc.) compose with every `_mode`.
  Same filter parser, same precedence.
- [ ] `_field` and `_agg_field` are validated against the model's
  filterable field list — invalid field returns 400, never an
  internal error.
- [ ] Permission gating mirrors the existing list endpoint exactly —
  no new permission rules, no leaked records via aggregation.
- [ ] Per-row visibility filters (group scope, ownership) apply to
  the record set BEFORE aggregation.
- [ ] Existing tests for list-mode endpoints continue to pass.
- [ ] New test coverage for each `_mode` × at least one representative
  endpoint (`/api/incident/event`, `/api/incident/incident`,
  `/api/system/geoip`).
- [ ] Doc page added to `docs/web_developer/` covering the
  aggregation surface, with the example calls above.

## Constraints

- **Backwards compatible.** Every endpoint behaves identically when
  `_mode` is omitted. No callers should need updates.
- **Same permission model.** No new permission tables or scopes.
- **No new endpoints.** Aggregation lives on the existing list URL.
- **`_`-prefix is reserved.** The framework reserves all `_`-prefixed
  query params for itself going forward; downstream apps and
  consumers must not invent their own `_`-prefixed names.
- **Existing bare params unchanged.** `size`, `start`, `sort`,
  `dr_start`, `dr_end`, `graph` keep their current names. Only the
  brand-new aggregation params use the `_` prefix.
- **Server-side caps to prevent runaway queries.** `_size` capped at
  100 for `_mode=top`. `_mode=distinct` capped at 1000 distinct
  values (400 error if exceeded). `_mode=histogram` capped at e.g.
  10000 buckets per response (caller picks a coarser `_bucket` if
  they hit the cap).
- **Numeric `_field` for non-numeric agg.** Asking for
  `_agg=avg` on a non-numeric `_field` returns 400 with a clear
  error.
- **`_mode=histogram` requires a datetime field.** Asking on a model
  without one returns 400.
- **Aggregation MUST be index-friendly.** Document which fields are
  indexed per model so the dashboard team knows which `_field`
  values will be fast vs. slow.

## Stretch (optional, if the same pass can swallow them)

- **`_format=csv`** on `_mode=top` / `distinct` / `histogram` for
  one-shot exports.
- **`_having`** filter applied AFTER aggregation, e.g. `top` IPs
  with `_having=value__gte=10` to drop low-volume noise alongside
  `_min_count`.
- **Multi-field `_field`** (`_field=source_ip,category`) for
  `_mode=top` returning composite keys —
  `[{key: ["1.2.3.4", "invalid_password"], value: 42}, ...]`. Useful
  for "top IP+category combinations" without two round-trips.

## Open questions

1. Should `_mode=count` accept `_agg=sum` for weighted-sum semantics
  (e.g., total bytes blocked)? My recommendation: **no**. Keep
  `_mode=count` strict at "row count" and route any sum through
  `_mode=summary&_agg=sum`. Clearer mental model, separate code
  paths.
2. Is there a concern with the `_` prefix breaking existing
  middleware/proxies/log parsers? Verify before locking it in.
3. Should the response include the wall-clock time the aggregation
  took (`took_ms`)? Useful for dashboard performance budgets and
  catching slow queries. Probably yes — adds nothing for clients
  that ignore it.
4. For `_mode=histogram`, should the bucket alignment be
  caller-controllable (e.g., `_bucket_align=local` vs `utc`)?
  Defaulting to UTC is correct; document it.

## Notes

- The Security Dashboard build (`web-mojo`) is currently **blocked**
  on this for the Top Source IPs and Top Categories panels. Those
  panels show a "backend not ready" empty state until the endpoint
  ships. Once it does, the dashboard's `TopSourcesPanel` will be a
  thin consumer (~30 lines).
- KPIStrip's REST count tiles currently use `?size=0` as a "give me
  the count" hack. Once `_mode=count` lands, we'll migrate them.
- The Distributions panel currently fetches 200 incidents and
  aggregates client-side for the Status donut and Priority buckets.
  Both replace cleanly with `_mode=top&_field=status` and
  `_mode=top&_field=priority`.
- This is an additive change — no deprecations, no migration burden
  on downstream apps.

---

<!-- Fill in when the request is resolved, then move the file to planning/done/ -->
## Resolution
**Status**: Resolved — YYYY-MM-DD

**Files changed**:
- `mojo/...`

**Tests run**:
- `...`

**Docs updated**:
- `docs/web_developer/...`

**Validation**:
[How the contract was verified — list the curl examples that should
return the documented shapes for each `_mode`]

---

## Plan

**Status**: planned
**Planned**: 2026-04-26

### Objective

Add a `_mode` query parameter to `MojoModel.on_rest_list` that branches
the response into one of five aggregation shapes (`count`, `top`,
`distinct`, `summary`, `histogram`) computed against the same
permission-scoped + filter-scoped queryset, leaving today's `list`
behavior identical when `_mode` is absent.

### Architecture

The aggregation surface is implemented as a single new module
(`mojo/models/rest_aggregation.py`) plus one branch point in
`mojo/models/rest.py`. There are no new endpoints, no new permission
plumbing, and no migrations. The aggregation helper receives the
already-filtered queryset and returns a `JsonResponse` — it never
re-implements filtering, scoping, or pagination.

```
on_rest_list (rest.py)
  ├─ apply group filter            (existing)
  ├─ apply field filters           (existing — on_rest_list_filter)
  ├─ apply date-range filter       (existing — on_rest_list_date_range_filter)
  ├─ if request.DATA.get("_mode") and _mode != "list":
  │     return on_rest_list_aggregate(request, queryset)   # NEW
  ├─ apply sort                    (existing)
  └─ on_rest_list_response         (existing — paged list)
```

Sort is intentionally skipped for aggregation modes — each mode
defines its own ordering (`top` by value desc, `distinct` by key asc,
`histogram` by ts asc, `summary`/`count` are scalar).

### Steps

1. **`mojo/models/rest.py`** — in `on_rest_list` (lines 608–636), after
   `on_rest_list_date_range_filter` and before `on_rest_list_sort`,
   add:

   ```python
   mode = request.DATA.get("_mode")
   if mode and mode != "list":
       from mojo.models.rest_aggregation import on_rest_list_aggregate
       return on_rest_list_aggregate(cls, request, queryset)
   ```

   Lazy import to avoid circular-import risk.

2. **`mojo/models/rest.py`** — extend `reserved_keys` in
   `on_rest_list_filter` (line 767) to skip every `_`-prefixed key:

   ```python
   for key, value in request.QUERY_PARAMS.items():
       if key.startswith("_"):
           continue
       ...
   ```

   This frees the entire `_*` namespace as framework-reserved without
   having to enumerate each new param.

3. **`mojo/models/rest_aggregation.py`** (NEW) — single module
   containing the aggregation entry point and one helper per mode.
   Public surface:

   ```python
   def on_rest_list_aggregate(cls, request, queryset):
       mode = request.DATA.get("_mode")
       if mode == "count":     return _agg_count(cls, request, queryset)
       if mode == "top":       return _agg_top(cls, request, queryset)
       if mode == "distinct":  return _agg_distinct(cls, request, queryset)
       if mode == "summary":   return _agg_summary(cls, request, queryset)
       if mode == "histogram": return _agg_histogram(cls, request, queryset)
       raise me.ValueException(f"unknown _mode: {mode}")
   ```

   Each `_agg_*` returns a `JsonResponse` built via
   `mojo.helpers.response.JsonResponse` to keep the envelope shape
   consistent with the rest of the framework.

4. **`mojo/models/rest_aggregation.py`** — `_agg_count`:
   `count = queryset.count()` → `{"status": True, "count": count}`.
   No `_field` required. No other params consulted.

5. **`mojo/models/rest_aggregation.py`** — `_agg_top`:
   - Validate `_field` (required) via `_validate_field(cls, name)`.
   - Resolve `_agg` (`count` default; allowed: `count|sum|avg|min|max`).
   - If `_agg != "count"`, validate `_agg_field` is numeric.
   - `_size` (default 10, hard cap 100).
   - `_min_count` (default 1).
   - Build with `queryset.values(_field).annotate(value=<Agg>)`.
   - Optionally annotate `first_seen=Min(ts)` / `last_seen=Max(ts)`
     when the model has a datetime column (`created`, or the first
     `DateTimeField` discovered on the model).
   - `.filter(value__gte=_min_count).order_by("-value")[:_size]`.
   - Stringify `key` (numeric/datetime → str), epoch-seconds the
     timestamps.
   - Response: `{status, graph: "top", field, agg, size, data: [...]}`.

6. **`mojo/models/rest_aggregation.py`** — `_agg_distinct`:
   - Validate `_field` (required).
   - `_min_count` (default 1).
   - Same `.values().annotate(value=Count("id"))` pipeline as `top`,
     but `.order_by(_field)` (alpha asc by key) and **no `_size` cap**.
   - Hard-cap server-side at `MOJO_REST_AGG_DISTINCT_CAP` (default
     1000). If `len(rows) > cap`, return 400
     (`me.ValueException("distinct cardinality exceeded cap")`).
   - Response: `{status, graph: "distinct", field, data: [...]}`.

7. **`mojo/models/rest_aggregation.py`** — `_agg_summary`:
   - Required: `_field` OR `_agg_field` (when `_agg != "count"`).
   - `_agg` (`count|sum|avg|min|max`; default `count`).
   - Single `.aggregate(value=<Agg>, min=Min(...), max=Max(...),
     n=Count("id"))` over the filtered queryset.
   - Validate the aggregated field is numeric for `sum/avg/min/max`.
   - Response: `{status, graph: "summary", field, agg, value, min,
     max, n}`.

8. **`mojo/models/rest_aggregation.py`** — `_agg_histogram`:
   - Required: `_field` (must be a `DateTimeField` or `DateField` on
     the model; else 400), `_bucket` (`minute|hour|day|week|month`).
   - Resolve `dr_start` / `dr_end` from the request (already-applied
     by `on_rest_list_date_range_filter`, but we need the bounds for
     gap-filling). If absent, use `Min(_field)` / `Max(_field)` from
     the queryset; if the queryset is empty, return
     `{status, graph: "histogram", field, bucket, data: []}`.
   - Use Django ORM `Trunc*` (`TruncMinute`, `TruncHour`, `TruncDay`,
     `TruncWeek`, `TruncMonth`) to bucket; `.values("ts")
     .annotate(value=Count("id")).order_by("ts")`.
   - Post-process: walk from `dr_start` to `dr_end` in `_bucket`
     steps, fill missing buckets with `value: 0`, emit `ts` as
     unix-seconds (UTC).
   - Bucket-count cap: `MOJO_REST_AGG_HISTOGRAM_CAP` (default 10000).
     Computed window / bucket > cap → 400.
   - Response: `{status, graph: "histogram", field, bucket, agg:
     "count", data: [{ts, value}, ...]}`.

9. **`mojo/models/rest_aggregation.py`** — shared helpers:
   - `_validate_field(cls, name)`: ensure `name in cls.__rest_field_names__`
     (populated by `on_rest_list_filter` already; guard with the same
     `if not hasattr` initializer). Reject:
     - relation fields without `__id` suffix (force explicit FK PK)
     - any name containing `__` other than the FK `__id` form (blocks
       `metadata__some_key` JSON-path drilling)
     - `TextField` (unbounded cardinality, perf footgun)
     - `EmailField` (PII / inference risk on small querysets)
     - `JSONField` (cardinality + DB-portability)
     - any field listed in `RestMeta.SENSITIVE_FIELDS` if the model
       defines it (existing convention; honor it for aggregation).
     - any field NOT listed in `RestMeta.AGGREGATION_FIELDS` if the
       model defines that opt-in allow-list (lets a model author be
       stricter than the type-based default).
     Raise `me.ValueException` with HTTP 400 on failure.
   - `_resolve_agg(name, allowed)`: enum guard.
   - `_numeric_field(cls, name)`: returns True only for
     `IntegerField`, `BigIntegerField`, `FloatField`, `DecimalField`,
     `PositiveIntegerField`, etc.
   - `_datetime_field(cls, name)`: True for `DateTimeField` /
     `DateField`.
   - `_first_datetime_field(cls)`: returns `"created"` if present,
     else the first `DateTimeField` on the model, else `None`.
   - `_stringify_key(value)`: bytes/None/datetime/int → str (datetime
     → epoch-seconds).

10. **`mojo/models/__init__.py`** — no change required; the module is
    imported lazily from `rest.py`. Nothing else needs to import it.

11. **`mojo/helpers/settings.py`** — no code change. New settings
    (`MOJO_REST_AGG_DISTINCT_CAP`, `MOJO_REST_AGG_TOP_CAP`,
    `MOJO_REST_AGG_HISTOGRAM_CAP`) are read via
    `settings.get_static(...)` at module load with sane defaults
    (1000 / 100 / 10000) — no settings file change required for
    consumers.

### Design Decisions

- **One module, not per-mode files.** Five small functions sharing
  helpers belong together. Splitting buys nothing.
- **Skip `on_rest_list_sort` for aggregation modes.** Each mode owns
  its own ordering; `?sort=created` would silently no-op which is
  worse than just skipping.
- **`_*` prefix is a global skip in the filter parser**, not an
  enumerated reserved-keys list. Adding a new aggregation param later
  doesn't risk re-introducing the field-collision bug the prefix is
  supposed to prevent.
- **Use Django ORM aggregates, not raw SQL.** `Count`, `Sum`, `Avg`,
  `Min`, `Max`, and `Trunc*` cover every mode and remain DB-portable
  (mojo runs against PostgreSQL today; the testproject runs against
  SQLite — both support these). No raw SQL means no injection
  surface beyond Django's own parameter binding.
- **Field validation rejects relation and text fields.**
  `metadata` (JSONField) and `details` (TextField) are obvious foot-
  guns for `_field=...` (unbounded cardinality, no index). Allow-list
  via field type rather than naming so future model authors don't
  have to opt into protection.
- **Stringify `key` server-side.** Per the request, every `key` is a
  string so the dashboard can use it directly as a `<select>` value
  or URL filter. Ints, bools, datetimes, decimals all coerce to str
  in `_stringify_key`.
- **`first_seen` / `last_seen` are best-effort.** Only emitted when
  the model exposes a datetime column. We pick `"created"` first
  (every mojo model has it per the models rule), and fall back to
  the first declared `DateTimeField` for non-conforming models.
- **Histogram gap-fill happens in Python**, not via a CTE / generate_
  series. Portable across PostgreSQL and SQLite, and the cap (10000
  buckets) bounds the loop.
- **`took_ms` (open question 3): YES, rounded.** Cheap to compute,
  free for callers that ignore it, and the dashboard genuinely
  needs the signal. Wrap each `_agg_*` body with a
  `time.perf_counter()` pair. Round to the nearest 10ms before
  emitting to blunt timing-oracle inference on filter-match counts.
- **`_mode=count` is strict row-count (open question 1).** No `_agg`
  honored. Sums route through `_mode=summary&_agg=sum`.
- **Histogram `_bucket_align` (open question 4): UTC, not
  configurable in v1.** Document it. Adding `_bucket_align=local`
  later is additive.
- **`_` prefix middleware concern (open question 2): not blocking.**
  Standard query-string semantics; no known mojo middleware /
  proxy / WAF strips `_*` keys. Document the reservation; if a
  downstream proxy turns out to mangle them, we can revisit.
- **Stretch items deferred.** `_format=csv`, `_having`, multi-field
  `_field` are all additive on top of this design. Land the core
  surface first, layer them in follow-up requests once consumed.

### User Cases

| Caller | Call | Why this works |
|---|---|---|
| Security Dashboard — Top Source IPs | `GET /api/incident/event?_mode=top&_field=source_ip&_size=10&dr_start=...` | Indexed `source_ip`; standard `top` path. |
| Security Dashboard — Top Categories | `GET /api/incident/event?_mode=top&_field=category&_size=10&dr_start=...` | Same path, different field. |
| Security Dashboard — Top auth-failure IPs | `GET /api/incident/event?_mode=top&_field=source_ip&category__in=invalid_password,login:unknown,totp:login_failed&_size=10` | Pre-filter via existing `category__in`, then aggregate. Single round-trip. |
| KPI strip — replace `?size=0` | `GET /api/incident/incident?_mode=count&status=new` | Pure scalar response, no record serialization cost. |
| KPI strip — firewall blocks | `GET /api/system/geoip?_mode=count&is_blocked=true` | Same path; `is_blocked` is indexed. |
| Distributions — Status donut | `GET /api/incident/incident?_mode=top&_field=status&_size=20` | Replaces "fetch 200 + client agg". |
| Distributions — Priority buckets | `GET /api/incident/incident?_mode=top&_field=priority&_size=20` | `priority` is numeric; `_agg=count` (default) groups by value. |
| Filter dropdown population | `GET /api/incident/event?_mode=distinct&_field=category` | Bounded set; alpha sort; under the cap. |
| Sparkline — events per day | `GET /api/incident/event?_mode=histogram&_field=created&_bucket=day&dr_start=...&dr_end=...` | Empty buckets gap-filled to `value: 0`. |
| Owner-scoped aggregation | `GET /api/files/file?_mode=top&_field=user_id` (caller has only `owner` perm) | `on_rest_handle_list` already pre-scopes the queryset to `owner_field=request.user`; aggregation runs over the same scoped queryset. |
| Group-scoped aggregation | `GET /api/incident/event?_mode=top&_field=category` (caller is in group X) | `on_rest_handle_list` filters to `group__in=groups_with_perms`; aggregation inherits the scope. |

### Edge Cases

- **`_field` = relation FK without `__id`** → 400 with explanatory
  error. (Otherwise `.values("group")` returns FK PKs but the user
  may have meant the FK display name; safer to force the explicit
  `__id` form.)
- **`_field` = TextField (`details`, `title`)** → 400. Unbounded
  cardinality, no index. Caller wants `_field=category` or similar.
- **`_field` = JSONField (`metadata`)** → 400. Same reason, plus
  GROUP BY semantics on JSON columns vary per DB.
- **`_field=metadata__some_key`** (JSON-path drilling) → 400. Only
  `<relation>__id` is allowed as a `__`-containing field name.
- **`_field` = EmailField or in `RestMeta.SENSITIVE_FIELDS`** → 400.
  Prevents inference attacks on small querysets where listing
  distinct emails / SSNs / phone numbers leaks the row even if the
  user's `graph` excludes the field.
- **Model defines `RestMeta.AGGREGATION_FIELDS = [...]`** — only
  those fields are aggregatable; everything else 400s regardless of
  type. Opt-in allow-list for model authors who want stricter-than-
  default behavior.
- **`_agg=avg` on a non-numeric field** → 400.
- **`_mode=histogram` on a model with no DateTimeField** → 400.
- **`_mode=histogram` with no `dr_start`/`dr_end`** → fall back to
  the queryset's `Min/Max` of `_field`. If those are also None
  (empty queryset), return `data: []` with the envelope intact.
- **`_mode=histogram` window/bucket > cap** → 400 with the computed
  bucket count and the cap so the caller knows what `_bucket` to
  pick.
- **`_mode=distinct` cardinality > cap** → 400, not silent
  truncation. Per the request acceptance criteria.
- **Caller sends both `size` and `_size`** → ignore the irrelevant
  one based on `_mode`. `_size` wins for `top`; `size` wins (and
  `_size` is ignored) for `list`. No 400.
- **Permission denial path** (`on_rest_handle_list` returns
  `cls.objects.none()`) — aggregation runs over the empty queryset
  and returns the correct empty-shape response. No leakage.
- **`request.group` set** — `on_rest_list` already filters to that
  group BEFORE we branch into aggregation; nothing extra needed.
- **`_mode=list`** (explicit) — treated identically to omitting
  `_mode`. Falls through to the existing list path.
- **Unknown `_mode` value** → 400 with the list of valid modes.
- **`_field` is `id`** — allowed; useful for `_mode=count` no-op
  semantics. (Top/distinct on `id` is degenerate but not harmful;
  cap protects.)
- **NULL keys in `top`/`distinct`** — present as `key: "null"`
  string (so JSON shape stays uniform). Document this.

### Testing

All new tests use `testit` per `.claude/rules/testing.md`. Each test
imports the model under test inside the function body and uses
`opts.client` for the HTTP path so permission/scope behavior is
exercised end-to-end.

- `tests/test_incident/test_event_aggregation.py` — NEW
  - `test_mode_count_returns_scalar` → `_mode=count` envelope shape, no
    `data` field, count matches `Event.objects.count()` after filters.
  - `test_mode_count_respects_filters` → `_mode=count&category=foo`
    matches the filtered count, not the total.
  - `test_mode_top_basic` → `_mode=top&_field=source_ip&_size=5`,
    asserts sort desc, length ≤ 5, key is str.
  - `test_mode_top_with_min_count` → seeds rows where some IPs have
    count=1, asserts they're dropped at `_min_count=2`.
  - `test_mode_top_size_capped_at_100` → `_size=500` clamps to 100.
  - `test_mode_top_includes_first_last_seen` → assert epoch-seconds
    timestamps present and ordered.
  - `test_mode_distinct_alpha_sort` → `_field=category`, asserts keys
    are ordered ascending.
  - `test_mode_distinct_cap_exceeded_returns_400` → seed > cap rows
    with `MOJO_REST_AGG_DISTINCT_CAP=5` via `th.server_settings`,
    assert 400.
  - `test_mode_summary_avg_priority` → on a numeric column, asserts
    `value`, `min`, `max`, `n` shape.
  - `test_mode_summary_rejects_avg_on_text_field` → 400.
  - `test_mode_histogram_day_buckets` → seed events across 3 days,
    `_bucket=day`, assert empty middle days appear with `value: 0`.
  - `test_mode_histogram_requires_datetime_field` → `_field=category`
    → 400.
  - `test_mode_unknown_returns_400` → `_mode=garbage` → 400.
  - `test_mode_field_relation_requires_id_suffix` → `_field=incident`
    → 400; `_field=incident__id` → 200.
  - `test_mode_field_textfield_rejected` → `_field=details` → 400.
  - `test_mode_field_jsonpath_rejected` → `_field=metadata__rule_id`
    → 400.
  - `test_mode_field_email_rejected` → seed a model with an
    EmailField; `_field=email` → 400.
  - `test_mode_field_sensitive_rejected` → model with
    `RestMeta.SENSITIVE_FIELDS=["secret_col"]`; `_field=secret_col`
    → 400 even if the column is otherwise an aggregatable type.
  - `test_mode_field_aggregation_allowlist` → model with
    `RestMeta.AGGREGATION_FIELDS=["category"]`; `_field=category`
    → 200, `_field=source_ip` → 400.
  - `test_took_ms_rounded` → assert response `took_ms` is a multiple
    of 10.
  - `test_mode_list_default_when_absent` → omit `_mode`, assert
    response shape matches today's list response (regression guard).

- `tests/test_incident/test_incident_aggregation.py` — NEW
  - `test_mode_top_status` → distribution donut path.
  - `test_mode_top_priority` → priority buckets path.
  - `test_mode_count_status_new` → `_mode=count&status=new` matches
    the KPI tile path.

- `tests/test_account/test_geolocated_ip_aggregation.py` — NEW
  - `test_mode_count_is_blocked` → KPI tile path
    (`_mode=count&is_blocked=true`).

- `tests/test_account/test_aggregation_permissions.py` — NEW
  - `test_aggregation_respects_owner_scope` → user with `owner`
    permission only sees aggregates over their own rows.
  - `test_aggregation_respects_group_scope` → user in group X gets
    aggregates over group X's rows; cross-group rows are excluded.
  - `test_aggregation_respects_perm_deny` → user with no view perm
    and `MOJO_REST_LIST_PERM_DENY=True` → 403, no aggregation.

- Existing `tests/test_incident/test_*.py` and
  `tests/test_account/test_group_list_no_perms.py` — must continue
  to pass unchanged (regression guard for the `_mode` absent path).

After model schema changes (none expected here, but in case the
testproject scaffold needs a refresh): `bin/create_testproject &&
bin/run_tests --agent -t test_incident.test_event_aggregation`.

### Docs

- **`docs/django_developer/core/mojo_model.md`** — add a new
  "Aggregation modes" section after the existing "List response"
  documentation. Cover: how to opt out (currently you can't —
  aggregation is universal once a model has a `RestMeta` list
  endpoint); which `_field` types are accepted/rejected; the
  `__rest_field_names__` validation; the index-friendliness note
  from the request's Constraints section.
- **`docs/django_developer/README.md`** — add an entry under the
  REST docs index linking to the new aggregation section.
- **`docs/web_developer/core/aggregation.md`** — NEW. Full client-
  facing spec mirroring the request's "Modes", "Request parameters",
  "Response shapes", and "Concrete examples" sections, with curl
  examples against `/api/incident/event`. Document the `_*` prefix
  reservation and the server-side caps.
- **`docs/web_developer/README.md`** — add the new aggregation page
  to the index, cross-link from the existing "List endpoints" doc.
- **`CHANGELOG.md`** — under the next release: "Add `_mode=...`
  aggregation modes (count, top, distinct, summary, histogram) to
  every CRUD list endpoint. The `_*` query-param prefix is now
  reserved for the framework. Existing list behavior unchanged when
  `_mode` is absent."
- **`planning/requests/security-dashboard.md`** — once this lands,
  unblock the noted Top Sources / Top Categories / KPI count tiles
  and reference this request in the Resolution section.


