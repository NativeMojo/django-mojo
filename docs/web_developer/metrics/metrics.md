# Metrics API — REST API Reference

## Permissions

- Configurable per account namespace (can be `"public"` for open access)
- Defaults to `view_metrics` or `manage_users`

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/metrics/fetch` | Fetch time-series data |
| GET | `/api/metrics/series` | Fetch current values for multiple slugs (alias: `/api/metrics/value/get`) |
| GET | `/api/metrics/value/get` | Fetch current values for slugs |
| GET | `/api/metrics/categories` | List metric categories |

## Fetch Time-Series

**GET** `/api/metrics/fetch`

```
GET /api/metrics/fetch?slug=page_views&granularity=days&dr_start=2024-01-01&dr_end=2024-01-31
```

| Param | Default | Description |
|---|---|---|
| `slug` | required | Metric name (or comma-separated list) |
| `granularity` | `hours` | `minutes`, `hours`, `days`, `weeks`, `months`, `years` |
| `dr_start` | auto | Start datetime |
| `dr_end` | auto | End datetime |
| `account` | `global` | Account namespace |
| `with_labels` | `false` | Include time labels in response |

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
