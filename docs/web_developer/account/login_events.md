# Login Events — REST API Reference

Admin-only endpoints for querying login history with geolocation data. Designed for map visualizations and anomaly detection in admin portals.

**Permissions required**: `manage_users` + `security` + `users`

---

## List Login Events

```
GET /api/account/logins
```

Paginated list of individual login events. Supports filtering and search.

### Query Parameters

| Param | Type | Description |
|---|---|---|
| `user` | int | Filter by user ID |
| `country_code` | string | ISO 3166 country code (e.g., `US`, `BR`) |
| `region` | string | Region/state name |
| `is_new_country` | bool | Only logins flagged as first-time country for that user |
| `is_new_region` | bool | Only logins flagged as first-time region for that user |
| `source` | string | Login method: `password`, `magic`, `sms`, `totp`, `oauth` |
| `dr_start` / `dr_end` | datetime | Date range filter on `created` |
| `search` | string | Searches `ip_address`, `country_code`, `region`, `city` |
| `sort` | string | Sort field (default: `-created`). Prefix `-` for descending |
| `start` / `size` | int | Pagination (default: `0` / `10`) |
| `graph` | string | Response shape: `list` (default) or `default` (full detail) |

### Response — `graph=list`

```json
{
  "status": true,
  "count": 1842,
  "start": 0,
  "size": 10,
  "data": [
    {
      "id": 5012,
      "user": {"id": 42, "username": "jdoe", "display_name": "Jane Doe"},
      "ip_address": "203.0.113.45",
      "country_code": "US",
      "region": "California",
      "city": "San Francisco",
      "latitude": 37.7749,
      "longitude": -122.4194,
      "source": "password",
      "is_new_country": false,
      "is_new_region": false,
      "created": "2026-03-29T14:22:00Z"
    }
  ]
}
```

### Response — `graph=default`

Adds `user_agent_info`, `device`, and `modified` fields:

```json
{
  "id": 5012,
  "user": {"id": 42, "username": "jdoe", "display_name": "Jane Doe"},
  "ip_address": "203.0.113.45",
  "country_code": "US",
  "region": "California",
  "city": "San Francisco",
  "latitude": 37.7749,
  "longitude": -122.4194,
  "source": "password",
  "is_new_country": false,
  "is_new_region": false,
  "user_agent_info": {
    "user_agent": {"family": "Chrome", "major": "120", "minor": "0", "patch": null},
    "os": {"family": "macOS", "major": "14", "minor": "3", "patch": null},
    "device": {"family": "Mac", "brand": "Apple", "model": "Mac"}
  },
  "device": {"id": 88, "duid": "a1b2c3", "muid": "m9x8y7"},
  "created": "2026-03-29T14:22:00Z",
  "modified": "2026-03-29T14:22:00Z"
}
```

---

## Get Login Event Detail

```
GET /api/account/logins/<id>
```

Returns a single login event. Uses `graph=default` shape.

---

## System-Wide Geo Summary

```
GET /api/account/logins/summary
```

Aggregated login counts by country, with centroid coordinates for map pin placement.

### Query Parameters

| Param | Type | Description |
|---|---|---|
| `region` | string | If provided, drill down into regions within this country_code |
| `country_code` | string | Required when `region` is set — the country to drill into |
| `dr_start` / `dr_end` | datetime | Date range filter |

### Response — Country Level (default)

```json
{
  "status": true,
  "data": [
    {
      "country_code": "US",
      "count": 1204,
      "latitude": 37.0902,
      "longitude": -95.7129,
      "new_country_count": 12
    },
    {
      "country_code": "BR",
      "count": 89,
      "latitude": -14.235,
      "longitude": -51.9253,
      "new_country_count": 3
    }
  ]
}
```

`latitude`/`longitude` are average centroids of all login events for that country. `new_country_count` is how many of those logins were flagged `is_new_country=True`.

### Response — Region Drill-Down (`?country_code=US&region=true`)

```json
{
  "status": true,
  "data": [
    {
      "country_code": "US",
      "region": "California",
      "count": 542,
      "latitude": 36.7783,
      "longitude": -119.4179,
      "new_region_count": 5
    },
    {
      "country_code": "US",
      "region": "New York",
      "count": 318,
      "latitude": 40.7128,
      "longitude": -74.006,
      "new_region_count": 2
    }
  ]
}
```

---

## Per-User Geo Summary

```
GET /api/account/logins/user
```

Same aggregation as system-wide summary, but scoped to a single user. Intended for per-user login maps.

### Query Parameters

| Param | Type | Required | Description |
|---|---|---|---|
| `user_id` | int | **yes** | The user to summarize |
| `region` | string | no | If truthy, drill down into regions (requires `country_code`) |
| `country_code` | string | no | Country to drill into for region view |
| `dr_start` / `dr_end` | datetime | no | Date range filter |

### Response

Same shape as system-wide summary, filtered to the specified user.

```json
{
  "status": true,
  "data": [
    {
      "country_code": "US",
      "count": 87,
      "latitude": 37.5,
      "longitude": -122.1,
      "new_country_count": 1
    }
  ]
}
```

---

## Anomaly Filtering

To find logins from new/unusual locations across the system:

```
GET /api/account/logins?is_new_country=true&sort=-created&size=50
```

To find new-country logins for a specific user:

```
GET /api/account/logins?user=42&is_new_country=true
```

---

## Metrics (Time-Series)

These metrics are recorded automatically on each login and available via the metrics API:

| Metric Slug | Category | Description |
|---|---|---|
| `login:country:{CC}` | `logins` | Login count by country code (e.g., `login:country:US`) |
| `login:region:{CC}:{region}` | `logins` | Login count by country + region (e.g., `login:region:US:California`) |
| `login:new_country` | `logins` | Count of first-time-country logins |
| `login:new_region` | `logins` | Count of first-time-region logins |

Query these via `GET /api/metrics/query?slug=login:country:US&granularity=days&range=30d`.

---

## Settings

These settings are read at startup. Changes require a server restart.

| Setting | Default | Description |
|---|---|---|
| `LOGIN_EVENT_TRACKING_ENABLED` | `true` | Master toggle. When `false`, no events are created |
| `LOGIN_EVENT_FLAG_NEW_COUNTRY` | `true` | Enable first-time-country detection |
| `LOGIN_EVENT_FLAG_NEW_REGION` | `true` | Enable first-time-region detection |
