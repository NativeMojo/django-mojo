# Fetching Metrics — Django Developer Reference

## fetch()

```python
metrics.fetch(
    slug_or_slugs,       # str or list[str]
    dt_start=None,       # datetime
    dt_end=None,         # datetime
    granularity="hours", # time bucket size
    account="global",
    with_labels=False    # include time labels in response
)
```

## Single Slug

```python
# Returns list[int] — one value per bucket
values = metrics.fetch("page_views", granularity="hours")

# With date range
values = metrics.fetch(
    "page_views",
    dt_start=datetime(2024, 1, 1),
    dt_end=datetime(2024, 1, 31),
    granularity="days"
)
```

## Multiple Slugs

```python
# Returns {slug: list[int]}
data = metrics.fetch(["page_views", "user_signups"], granularity="days")

# With labels — returns {"labels": [...], "data": {slug: [...]}}
series = metrics.fetch(
    ["page_views", "user_signups"],
    dt_start=datetime(2024, 1, 1),
    dt_end=datetime(2024, 1, 31),
    granularity="days",
    with_labels=True
)
# series == {"labels": ["2024-01-01", "2024-01-02", ...], "data": {"page_views": [...], "user_signups": [...]}}
```

## fetch_values()

Fetch current values for multiple slugs at a single point in time:

```python
result = metrics.fetch_values(
    "page_views,user_signups",   # comma-separated or list
    when=datetime(2024, 1, 15),
    granularity="days"
)
# result == {"data": {"page_views": 1500, "user_signups": 42}, "slugs": [...], "when": "...", ...}
```

### with_delta option

Pass `with_delta=True` to also fetch the previous bucket's values and compute per-slug deltas. Used by the REST `/api/metrics/series` endpoint for KPI tiles.

```python
result = metrics.fetch_values(
    ["page_views", "signups"],
    when=datetime(2024, 1, 15, 15),
    granularity="hours",
    with_delta=True
)
# result["prev_data"]  == {"page_views": 20, "signups": 0}
# result["prev_when"]  == "2024-01-15T14:00:00"
# result["deltas"]     == {
#     "page_views": {"delta": 27, "delta_pct": 135.0},
#     "signups":    {"delta": 3}          # delta_pct omitted when prev==0
# }
```

`delta_pct` is only included when `prev_value > 0` — avoids Infinity in JSON output. The base response keys (`data`, `slugs`, `when`, `granularity`, `account`) are always present regardless of `with_delta`.

## Category Fetch

Fetch all slugs in a category:

```python
data = metrics.fetch_by_category(
    "auth",
    granularity="days",
    with_labels=True
)
```

## Category Management

```python
cats = metrics.get_categories()                  # set of category names
slugs = metrics.get_category_slugs("auth")       # set of slugs in category
metrics.delete_category("old_category")          # remove category (not the data)
```

## Account Management

```python
accounts = metrics.list_accounts()
metrics.add_account("group-123")
metrics.delete_account("old_group")             # removes from index, not time-series keys

# Permissions
perms = metrics.get_accounts_with_permissions()
metrics.set_view_perms("group-123", "view_metrics")
metrics.set_write_perms("group-123", "record_metrics")
```

## REST Discovery Catalog

`GET /api/metrics/discover` exposes three progressively scoped name registries:

- `resource=accounts` reads the maintained `mets:_accounts_` set, unions the
  canonical `public` and `global` names, and filters every candidate through
  the same `check_view_permissions()` helper used by fetches.
- `resource=categories&account=<name>` reads `mets:<account>:cats` only after
  that account's view check succeeds.
- `resource=slugs&account=<name>` reads `mets:<account>:slugs`, or
  `mets:<account>:c:<category>` when an exact category is supplied, only after
  the same account check.

`add_metrics_slug()` now adds its account to `mets:_accounts_` as well as its
slug to the account registry. Consequently normal `record()` calls and direct
slug registration maintain discovery without a backfill or cluster scan.
Configured accounts already enter the same set through the permission setters.
One atomic Redis Lua operation checks the account set with `SCARD` and returns
`SMEMBERS` only while the set contains at most 1,000 maintained candidates.
Consequently concurrent growth cannot materialize or authorize an oversized
set. Discovery never calls the historical data-key scan or gauge helpers; old
unindexed accounts enter on their next time-series record and remain usable
through exact account entry meanwhile.

All resources sort unique names lexicographically, apply optional
case-insensitive substring search, then calculate total `count` and slice by
`start`/`size`. The requested size defaults to 50 and is capped at 500.
`page_count` describes only the returned page and `next_start` is the next
numeric offset or null.

Slug strings are returned exactly as registered. A dimensional slug such as
`api:request:status:200` must be passed whole to `/api/metrics/series` or
`/api/metrics/fetch`; discovery consumers must never normalize it or keep only
the last colon-delimited segment. `fetch_values()` and `/series` preserve full
keys. The older `fetch()` response currently labels returned series by the last
segment, so do not use its response keys as catalog identifiers.

## Group Fan-Out

`/api/metrics/fetch` supports a `child_kind` query param that sums a metric across all active descendants of a parent group whose `kind` matches:

```
GET /api/metrics/fetch?slug=visits&account=group-42&child_kind=location
```

The fan-out is implemented in `mojo.apps.metrics.rest.helpers.fetch_group_fanout` (REST-layer only — not re-exported on the `metrics` package). Permission is checked once on the parent via `_check_group_account_permission`, which walks the parent chain via `Group.user_has_permission(check_parents=True)`. The descendant set comes from `Group.get_children(is_active=True, kind=child_kind)`.

Constraints:
- `account` must be `group-<parent_id>`; other accounts combined with `child_kind` return 400.
- The descendant set is capped at `METRICS_FANOUT_MAX_CHILDREN` (default 200). Exceeding the cap returns 400.
- An empty descendant set returns a zero-filled series, not an error.

### Per-Child Breakdown

Pass `breakdown=true` (or call `fetch_group_fanout(..., breakdown=True)`) to return one series per child instead of the sum. Single-slug only — multi-slug + breakdown raises `ValueException`.

Response keys are child `name`; when two children share a name both keys become `name#<id>` to avoid silent merging. The response includes a `groups` map of `key -> id`.

## Range Cap

Every read path reaches Redis through `utils.generate_slugs_for_range`, which
materialises **one key string per bucket** (and `fetch` then builds a second,
tagged copy before the MGET). A request that supplies both `dt_start` and
`dt_end` used to be unbounded: `dt_start=1900-01-01&dt_end=2100-01-01&granularity=minutes`
is ~105M buckets — many gigabytes of strings from a single request, on
endpoints that can be reached anonymously when the account is `public`.

`utils.get_date_range` now calls `utils.check_range_bounds` after resolving
both bounds and **before** any key is generated. A range spanning more than
`METRICS_MAX_RANGE_BUCKETS` buckets raises `ValueException` (400):

```
date range too wide: 86401 minutes buckets exceeds METRICS_MAX_RANGE_BUCKETS (10000)
```

Because the check lives in the one shared helper, every read endpoint inherits
it — `fetch`, `series`, `value/get` (category branch), `categories`,
`category_slugs`, `category_fetch` and `discover`. Set the setting to `0` to
disable the cap entirely (not recommended on any internet-reachable
deployment). `utils.count_buckets(dt_start, dt_end, granularity)` is the public
estimator, if you want to size a range before issuing a read.

## Settings

| Setting | Default | Description |
|---|---|---|
| `METRICS_TIMEZONE` | `"America/Los_Angeles"` | Default timezone for metric recording |
| `METRICS_TRACK_USER_ACTIVITY` | `False` | Auto-record per-user activity metrics |
| `METRICS_FANOUT_MAX_CHILDREN` | `200` | Hard cap on the number of child groups a single fan-out fetch will dispatch to. Requests resolving more children return 400. |
| `METRICS_MAX_RANGE_BUCKETS` | `10000` | Hard cap on how many time buckets one read may span, checked before any Redis key is generated. Wider ranges return 400. 10,000 buckets is ~6.9 days at `minutes`, ~416 days at `hours`, ~27 years at `days`. `0` disables the cap. |
