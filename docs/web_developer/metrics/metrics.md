# Metrics API — REST API Reference

## Permissions

- Configurable per account namespace (can be `"public"` for open access)
- Defaults to `view_metrics` or `manage_users`

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/metrics/fetch` | Fetch time-series data |
| GET | `/api/metrics/value/get` | Fetch current values for slugs |
| GET | `/api/metrics/categories` | List metric categories |
| GET | `/api/metrics/categories` | List categories |

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
