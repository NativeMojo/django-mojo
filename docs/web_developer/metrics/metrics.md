# Metrics API — REST API Reference

## Permissions

- Configurable per account namespace (can be `"public"` for open access)
- Defaults to `view_metrics` or `manage_users`
- **Reads** of the `public` account are anonymous. **Writes** (`POST
  /api/metrics/record`, `/api/metrics/value/set`) are not: they require
  `write_metrics`/`metrics`, unless an operator explicitly opted the account
  into anonymous writes server-side (`set_write_perms("public", "public")`).
  Anonymous clients that need to count something should go through an
  app-specific collect endpoint, not raw metric writes.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/metrics/fetch` | Fetch time-series data |
| GET | `/api/metrics/series` | Fetch current values for multiple slugs (alias: `/api/metrics/value/get`) |
| GET | `/api/metrics/value/get` | Fetch current values for slugs |
| GET | `/api/metrics/categories` | List metric categories |
| GET | `/api/metrics/discover` | Discover permission-filtered accounts, categories, or full time-series slugs |

## Discover Accounts, Categories, and Slugs

**GET** `/api/metrics/discover`

The endpoint returns one resource per request so an explorer can progressively
select an account, category, and exact time-series slug.

| `resource` | Required filters | Optional filters |
|---|---|---|
| `accounts` | none | `search` |
| `categories` | `account` | `search` |
| `slugs` | `account` | exact `category`, `search` |

Every request also accepts `start` (default 0, minimum 0) and `size` (default
50, range 1–500). `account` and `category` are capped at 256 characters;
`search` is capped at 128 and is a case-insensitive substring filter. Names are
sorted lexicographically after filtering and before slicing. Unknown,
duplicate, bracketed, dotted, non-scalar, or invalid resource/filter parameters
return 400; generic list controls such as `sort` and `graph` are not accepted.

```http
GET /api/metrics/discover?resource=slugs&account=group-42&category=http&size=50
```

```json
{
  "status": true,
  "resource": "slugs",
  "filters": {"account": "group-42", "category": "http", "search": ""},
  "data": ["api:request:status:200", "api:request:status:500"],
  "start": 0,
  "size": 50,
  "count": 2,
  "page_count": 2,
  "next_start": null
}
```

`count` is the total visible match count before paging; `page_count` is the
number of names in this page. `next_start` is the next numeric offset when
more names remain, otherwise null. An offset at or beyond `count`, a missing
category, or an unmatched search returns an empty successful page while
retaining the correct visible total.

Account enumeration requires authenticated global `view_metrics` or `metrics`
access and then filters every candidate through that account's live view
policy. Hidden accounts never affect counts. In particular, a global metrics
grant does not bypass a custom account's configured permission. Direct
category/slug discovery uses the selected account's normal policy, so
`public` and custom-public registries remain anonymously readable. A denied
account returns the same generic 403 as fetch and is checked before its names
are read.

Slugs are exact opaque identifiers. Preserve the complete colon-delimited
string and send it unchanged to `/api/metrics/series` or `/api/metrics/fetch`.
`/series` preserves full response keys. The older multi-slug `/fetch` response
currently labels each series with only the last colon segment, so never derive
catalog identifiers from those response keys. Gauge names (`value/get` and
`value/set`) are not part of discovery.

## Fetch Time-Series

**GET** `/api/metrics/fetch`

```
GET /api/metrics/fetch?slug=page_views&granularity=days&dt_start=2024-01-01&dt_end=2024-01-31
```

| Param | Default | Description |
|---|---|---|
| `slug` | required | Metric name (or comma-separated list) |
| `granularity` | `hours` | `minutes`, `hours`, `days`, `weeks`, `months`, `years` |
| `dt_start` | auto | Start datetime |
| `dt_end` | auto | End datetime |
| `account` | `global` | Account namespace |
| `with_labels` | `false` | Include time labels in response |
| `child_kind` | unset | When set with `account=group-<parent_id>`, sums the metric across all active descendants of the parent whose `kind` matches. See [Parent-Group Fan-Out](#parent-group-fan-out). |
| `breakdown` | `false` | When `true` (with `child_kind`), returns one series per child group instead of summing. Single-slug only. |

**Response (single slug):**

```json
{
  "status": true,
  "data": {
    "slug": "page_views",
    "granularity": "days",
    "values": [150, 230, 180, 420, 310],
    "labels": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
  }
}
```

**Response (multiple slugs with labels):**

```json
{
  "status": true,
  "data": {
    "labels": ["2024-01-01", "2024-01-02", "2024-01-03"],
    "data": {
      "page_views": [150, 230, 180],
      "user_signups": [5, 8, 3]
    }
  }
}
```

## Fetch Current Values

**GET** `/api/metrics/value/get`

```
GET /api/metrics/value/get?slugs=page_views,user_signups&granularity=hours
```

```json
{
  "status": true,
  "data": {
    "page_views": 47,
    "user_signups": 2,
    "when": "2024-01-15T10:00:00Z",
    "granularity": "hours"
  }
}
```

## Fetch Point-in-Time Values with Delta (`/api/metrics/series`)

**GET** `/api/metrics/series`

Returns the current-bucket value for one or more slugs. Optionally includes the previous bucket's value and a per-slug delta map for KPI tiles.

| Param | Default | Description |
|---|---|---|
| `slugs` | required | Comma-separated slug names |
| `when` | current time | Point in time (ISO 8601 datetime) |
| `granularity` | `hours` | Bucket size — see Granularity Reference |
| `account` | `public` | Account namespace |
| `with_delta` | `false` | When `true`, include `prev_data`, `prev_when`, and `deltas` |

**Request without delta (default):**

```
GET /api/metrics/series?slugs=page_views,signups&granularity=hours
```

**Response:**

```json
{
  "status": true,
  "data": {"page_views": 47, "signups": 3},
  "slugs": ["page_views", "signups"],
  "when": "2026-04-26T15:00:00",
  "granularity": "hours",
  "account": "public"
}
```

**Request with delta (KPI tile use-case):**

```
GET /api/metrics/series?slugs=page_views,signups&granularity=hours&with_delta=true
```

**Response:**

```json
{
  "status": true,
  "data": {"page_views": 47, "signups": 3},
  "slugs": ["page_views", "signups"],
  "when": "2026-04-26T15:00:00",
  "granularity": "hours",
  "account": "public",
  "prev_data": {"page_views": 20, "signups": 0},
  "prev_when": "2026-04-26T14:00:00",
  "deltas": {
    "page_views": {"delta": 27, "delta_pct": 135.0},
    "signups": {"delta": 3}
  }
}
```

Notes:
- `delta_pct` is omitted when `prev_value` is 0 (avoids Infinity).
- The response shape is identical to the non-delta response when `with_delta` is absent or `false` — fully backwards compatible.
- `prev_when` is one bucket back from `when` at the given granularity.

## Parent-Group Fan-Out

When a metric is recorded per child group (`account=group-<child_id>`) and you want a parent-level rollup, pass `child_kind` to fan out across every active descendant of the parent group that matches the kind. Per-bucket values are summed across children.

```
GET /api/metrics/fetch?slug=visits&account=group-42&child_kind=location&granularity=days&with_labels=true
```

- `account` must be of the form `group-<parent_id>`. Other accounts (`public`, `global`, `user-*`) return 400 when combined with `child_kind`.
- Permission is checked once on the parent group. Members of the parent or any of its ancestor groups (via `Group.user_has_permission` parent-chain walk) are authorized — no per-child permission check.
- The descendant set is recursive: grandchildren of the matching kind are summed too.
- Inactive children are excluded.
- An empty descendant set returns a zero-filled series of the correct length, not an error.
- The fan-out is capped at `METRICS_FANOUT_MAX_CHILDREN` (default 200). Requests resolving more children return 400.

The response shape matches the multi-slug shape of the regular fetch:

```json
{
  "status": true,
  "data": {
    "labels": ["2024-01-01", "2024-01-02", "2024-01-03"],
    "data": {
      "visits": [320, 415, 380]
    }
  }
}
```

`slug`, `slugs`, and `category` all work with `child_kind` — slugs are resolved once (from the parent's category index when `category` is used) and then summed per slug across children.

### Per-Child Breakdown

Pass `breakdown=true` to return one series per child group instead of the sum. Useful for stacked charts and per-location dashboards.

```
GET /api/metrics/fetch?slug=visits&account=group-42&child_kind=location&breakdown=true&granularity=days&with_labels=true
```

Constraints:
- Single slug only. Multi-slug + breakdown returns 400 (the flat shape can't carry both slug-keyed and child-keyed data).
- Same permission, descendant, and cap rules as sum-mode fan-out.

The response uses the same flat `{labels, data: {<key>: [int]}}` shape, but `<key>` is the child group's `name`. When two children share a name, both keys become `name#<id>` to avoid silent merging. A top-level `groups` map gives `key -> id`:

```json
{
  "status": true,
  "data": {
    "labels": ["2026-04-07", "2026-04-08", "2026-04-09"],
    "data": {
      "Downtown": [10, 20, 30],
      "Uptown":   [5, 15, 25]
    },
    "groups": {
      "Downtown": 12,
      "Uptown":   13
    }
  }
}
```

If two children share the name `Downtown`, both keys become `Downtown#12` / `Downtown#15`; other unique-named children keep their bare name.

## Fetch by Category

```
GET /api/metrics/fetch?category=auth&granularity=days&with_labels=true
```

## Granularity Reference

| Value | Bucket Size |
|---|---|
| `minutes` | 1-minute buckets |
| `hours` | 1-hour buckets |
| `days` | 1-day buckets |
| `weeks` | 1-week buckets |
| `months` | 1-month buckets |
| `years` | 1-year buckets |
