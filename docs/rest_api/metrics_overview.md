# MOJO REST API: Metrics Overview

The MOJO Metrics API allows you to record, retrieve, and analyze time-series metrics (counters, events, activity stats, etc.) from any client—programmatic, dashboard, or integration. All data is recorded in Redis, with support for granularities, categories, and per-tenant accounts.

---

## What You Can Do

- **Record metrics:** Log page views, feature usage, custom event counts, and more via `/api/metrics/record`.
- **Fetch metrics:** Query historical counters or aggregates by slug, category, date/time window, or granularity via `/api/metrics/fetch`.
- **Multi-tenant/account support:** All metrics can be segmented by logical account (public/global, per-group, etc.), with permission controls for privacy.
- **Highly configurable:** Control over time-granularity, expiry, access rights, and label generation.

---

## Authentication & Permissions

- **Default "public" account:** Open for anonymous and integration use; no authentication required.
- **Custom accounts (e.g., "global", "group_\<id\>"):** Requires authentication and appropriate view/write permissions. Only authorized users can view or record.
- **Set permissions in backend as needed** for each account using the backend API or utilities.

---

## Main Endpoints

- **POST `/api/metrics/record`**  
  Record/increment a metric slug. Specify account, category, count, and granularity.

- **GET `/api/metrics/fetch`**  
  Retrieve one or more metrics by slug, across time slices. Support for date ranges, categories, accounts, and output labelling.

---

## Basic API Workflow

1. **Choose your metric slug** (e.g., "page_views" or "user_signups").
2. **Record a data point** via the record endpoint (typically increment by 1).
3. **Fetch data** by slug, date window, and granularity to get a time series or aggregate.

---

## Example: Page View Recording and Fetch

1. **Record a new view:**
    ```http
    POST /api/metrics/record
    {
      "slug": "page_views",
      "count": 1,
      "account": "public"
    }
    ```

2. **Fetch recent views (hourly):**
    ```http
    GET /api/metrics/fetch?slugs=page_views&granularity=hours
    ```

---

## Topics Covered in Detail

- [How to Record Metrics (parameters, workflow)](metrics_record.md)
- [How to Fetch Metrics (aggregation, time windows, categories)](metrics_fetch.md)
- [Permissions, Accounts, and Best Practices](overview.md)

---

**Questions or advanced patterns? The backend developer team can add custom endpoints, retention, or event flows as needed. Happy measuring!**