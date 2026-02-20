# Recording Metrics via the MOJO REST API

This guide covers how to record event and activity metrics directly from any client or integration using the `/api/metrics/record` endpoint.

---

## Endpoint

**POST** `/api/metrics/record`

Records (increments) a metric for a given slug, category, and account, at one or more time granularities.

---

## Request Parameters

Send as JSON in the request body.

| Name             | Type      | Required | Default   | Description                                                                  |
|------------------|-----------|----------|-----------|------------------------------------------------------------------------------|
| `slug`           | string    | YES      | —         | Unique name for this metric (e.g., `page_views`, `user_signups`).            |
| `account`        | string    | NO       | `public`  | Logical account/namespace (`public`, `global`, `group_<id>`, etc).           |
| `count`          | integer   | NO       | 1         | Amount by which the counter should be incremented.                           |
| `min_granularity`| string    | NO       | `hours`   | Minimum time granularity (`minutes`, `hours`, `days`, etc.).                 |
| `max_granularity`| string    | NO       | `years`   | Maximum time granularity.                                                    |
| `category`       | string    | NO       | —         | Optional grouping/category for this metric.                                  |

---

## Example Requests

### Basic Public Metric (no auth needed)

```http
POST /api/metrics/record
Content-Type: application/json

{
  "slug": "page_views"
}
```

### Custom Count, Group/Account, and Granularity

```http
POST /api/metrics/record
Content-Type: application/json

{
  "slug": "login_success",
  "account": "group_42",
  "count": 2,
  "min_granularity": "minutes",
  "max_granularity": "days",
  "category": "auth"
}
```
> Note: group/global accounts may require authentication and write permissions.

---

## Response

```json
{
  "status": true
}
```
- If permission is denied, a relevant error response is returned (see [errors.md](errors.md)).

---

## Parameter Details

- **Slug**: Choose a memorable string for the event/action you are measuring.
- **Account**: For tenant isolation or per-group stats (`group_123`). `"public"` is open, `"global"` and others may need permissions.
- **Count**: Normally 1 (for a single event); you can record multiple in a single call.
- **Granularity**: Most use cases only need the defaults. Only change these if you need ultra-fine or coarse metrics.
- **Category**: (optional) Helps you group related metrics for easy aggregate queries.

---

## Security, Permissions, and Rate Limits

- Anyone can write to the `public` account.
- Writing to a custom or group account requires that you are authenticated and have write permission for that account.
- All writes are atomic and idempotent per call.
- Avoid excessive high-rate updates to a single metric to prevent Redis bottlenecks.

---

## Best Practices

- Always use consistent slug/account naming conventions for easy analysis.
- Only record what's needed—avoid spamming unnecessary increment events.
- Document custom metric categories and their meaning for other API users.

---

See [metrics_fetch.md](metrics_fetch.md) for info on how to retrieve and analyze metrics you've recorded.
