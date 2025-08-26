# Fetching Metrics via the MOJO REST API

This guide explains how to retrieve metrics data using the `/api/metrics/fetch` endpoint. You'll learn to get time series counts, aggregate data, and analytical breakdowns for any metric slug or category.

---

## Endpoint

**GET** `/api/metrics/fetch`

Returns metrics for given slug(s) or category, in any time window and granularity.

---

## Request Parameters

Pass parameters as standard query params (for GET), or in JSON (if POST is supported).

| Name           | Type      | Required | Default   | Description                                                        |
|----------------|-----------|----------|-----------|--------------------------------------------------------------------|
| `slugs`        | string or array | YES      | —         | One or more metric slugs (comma-separated or as array).            |
| `dt_start`     | string (date/datetime) | NO | —       | Beginning of date range (inclusive), format: `YYYY-MM-DD`, RFC3339 |
| `dt_end`       | string (date/datetime) | NO | —       | End of date range (inclusive), format as above                     |
| `granularity`  | string    | NO       | `hours`   | Time granularity (`minutes`, `hours`, `days`, etc.)                |
| `account`      | string    | NO       | `public`  | Account namespace: `public`, `global`, `group_<id>`, etc.          |
| `category`     | string    | NO       | —         | Fetch all slugs in this category (see below).                      |
| `with_labels`  | boolean   | NO       | false     | If true, returns human-readable labels for the time periods.       |

---

## Example: Basic Usage

Fetch hourly values for a single slug in the public account:

```
GET /api/metrics/fetch?slugs=page_views&granularity=hours
```

_Response:_
```json
{
  "status": true,
  "data": {
    "labels": ["09:00", "10:00", "11:00"],
    "data": {
      "page_views": [15, 27, 8]
    }
  }
}
```

---

## Example: Date Range

Fetch user signups for January 2024 by day:

```
GET /api/metrics/fetch?slugs=user_signups&dt_start=2024-01-01&dt_end=2024-01-31&granularity=days
```
_Response contains one integer per day in range._

---

## Example: Multiple Slugs

Fetch and compare several metrics at once:

```
GET /api/metrics/fetch?slugs=login_success,login_failure&page=home_page&granularity=hours
```
_Returns arrays for each slug keyed by slug name._

---

## Example: Fetch by Category

If metrics are grouped by category (e.g., "activity"), request all in one call:

```
GET /api/metrics/fetch?category=activity&dt_start=2024-03-01&dt_end=2024-03-31&granularity=days
```
_Response data dict contains each slug in the category with its series._

---

## Example: Per-Account Fetch

To isolate analytics for a specific group or tenant:

```
GET /api/metrics/fetch?slugs=page_views&account=group_42&granularity=days
```
_Requires permissions for the specified account._

---

## All Parameters: Descriptions

- **slugs**: Accepts a single slug or comma-separated list. Must be present unless using category.
- **dt_start / dt_end**: If omitted, fetches the most recent data as possible for the granularity.
- **granularity**: Use one of `minutes`, `hours`, `days`, `weeks`, `months`, `years`. Choose to control "how coarse" the result buckets are.
- **account**: Public is open (no auth required). Other accounts need permissions.
- **category**: If specified, fetches all slugs belonging to the category.
- **with_labels**: If true, provides a `labels` array with time-format strings matching each value.

---

## Security and Permissions

- **Public account:** Anyone can fetch.
- **Private/custom accounts:** Require authentication and view permission.
    - If permission denied, error response as in [errors.md](errors.md).

---

## Response Structure

- **labels (optional):** Array of date/times for each bucket.
- **data:** Keyed by slug (or per-category), each is an array of counts.
- **status:** Always true if successful.
- **Error:** Returns a structured error response on permission denied, invalid, or not found (see [errors.md](errors.md)).

---

## Best Practices

- Use the coarsest granularity that meets your needs—it's more efficient.
- For reporting dashboards, cache or aggregate on the client if possible.
- Use consistent slugs and categories for easier long-term analytics.

---

## Troubleshooting & Errors

- If you request too large a date range or too fine a granularity, the response may be slow or truncated.
- Make sure you have permission for the account requested.
- Invalid slugs are ignored; missing ones return empty arrays.
- For deeply nested or complex analytics, fetch category-wide and process on the client.

---

**Happy metrics analysis! See [metrics_overview.md](metrics_overview.md) for more on metric system design and usage.**