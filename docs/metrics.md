# Mojo Metrics (Redis-backed) for Django

A tiny, pragmatic metrics module for recording and fetching counters over time using Redis.

- Time-series metrics at multiple granularities (minutes → years)
- Optional categories to group slugs
- Multi-tenant “accounts” with view/write permissions
- Simple key/value storage (non time-series)
- HTTP REST endpoints included

Works great for page views, signups, background jobs, feature usage, etc.


## Requirements

- Redis (standalone or cluster) reachable by your Django project
- Mojo’s Redis settings configured (so `mojo.helpers.redis.get_connection()` works)

No models or migrations required.


## Install and import

Add/keep the Mojo package in your project, then import the metrics facade:

- Module: `mojo.apps.metrics`
- Import: `from mojo.apps import metrics`


## Quickstart

Record counters (time-series). Each call increments the current period(s) at the granularities you choose.

    from datetime import datetime
    from mojo.apps import metrics

    # 1) Increment "page_views" now by 1 (default granularity hours→years)
    metrics.record("page_views")

    # 2) Increment at a specific time, by a specific amount, under a custom account
    metrics.record("user_signups", when=datetime(2023, 10, 10), count=5, account="users")

    # 3) Track at finer resolution (minutes up to days)
    metrics.record("app_usage", min_granularity="minutes", max_granularity="days")

    # 4) Record using a specific timezone (affects which day/hour the event lands in)
    metrics.record("daily_jobs", timezone="America/Los_Angeles")

Fetch time-series back:

    from datetime import datetime
    from mojo.apps import metrics

    # Values for one slug; hourly buckets over a date range → returns a list[int]
    values = metrics.fetch(
        "page_views",
        dt_start=datetime(2023, 10, 1),
        dt_end=datetime(2023, 10, 10),
        granularity="hours",
    )

    # Fetch multiple slugs with labels → returns {"labels":[...], "data":{slug:[...]}}
    series = metrics.fetch(
        ["page_views", "user_signups"],
        dt_start=datetime(2023, 10, 1),
        dt_end=datetime(2023, 10, 10),
        granularity="days",
        with_labels=True,
    )


## Core Python API

Import all functions from `mojo.apps.metrics`.

### record(slug, when=None, count=1, category=None, account="global", min_granularity="hours", max_granularity="years", timezone=None)

- slug: string identifier for the metric (e.g., "page_views")
- when: datetime to attribute the count to; if omitted, “now” (see Timezone below)
- count: integer increment (default 1)
- category: optional category name to group this slug
- account: logical tenant/namespace; default "global"
- min_granularity / max_granularity: any of "minutes", "hours", "days", "weeks", "months", "years"
- timezone: e.g., "America/Los_Angeles" (overrides global setting for this call)

Notes:
- One call increments multiple series (e.g., the hour bucket, the day bucket, the month, etc.) bounded by your min/max.
- Slugs are normalized internally (":" is replaced with "|") to keep Redis keys simple.

Examples:

    # Track at hours, days, months (omit years)
    metrics.record("searches", min_granularity="hours", max_granularity="months")

    # Track and assign the slug to a category for later group fetching
    metrics.record("user_login", category="auth")

    # Multi-tenant partition
    metrics.record("page_views", account="site_42")


### fetch(slug_or_slugs, dt_start=None, dt_end=None, granularity="hours", account="global", with_labels=False)

- slug_or_slugs: a string slug, or a list of slugs
- dt_start / dt_end: datetimes; if omitted, a sensible window is chosen based on granularity
- granularity: one of "minutes", "hours", "days", "weeks", "months", "years"
- account: which account namespace to read from
- with_labels:
  - False (default):
    - For a single slug, returns a list of integers (one per bucket).
    - For multiple slugs, returns a dict {slug: [int, ...]}.
  - True:
    - Always returns a dict: {"labels": [...], "data": {slug: [int, ...]}}

Examples:

    # Single series, default window for hours
    vals = metrics.fetch("page_views", granularity="hours")

    # Multi-series with labels
    out = metrics.fetch(["page_views", "user_signups"], granularity="days", with_labels=True)
    # out == {"labels": ["2025-08-01", "2025-08-02", ...], "data": {"page_views": [...], "user_signups": [...]}}


### fetch_values(slugs, when=None, granularity="hours", account="global", timezone=None)

Fetch values for multiple slugs at a single point in time (one bucket).

- slugs: comma-separated string or list (e.g., "a,b,c" or ["a","b","c"])
- when: datetime (defaults to “now” in the configured timezone)
- granularity, account, timezone: same semantics as `record`

Returns:

    {
      "data": {"slugA": int, "slugB": int, ...},
      "slugs": [...],
      "when": "<iso8601>",
      "granularity": "<granularity>",
      "account": "<account>"
    }

Example:

    metrics.fetch_values("page_views,user_signups", when=datetime(2025, 8, 1), granularity="days")


### Categories

Attach a slug to a category by passing `category=` to `metrics.record`. Manage/read categories:

- `get_categories(account="global") -> set[str]`
- `get_category_slugs(category, account="global") -> set[str]`
- `delete_category(category, account="global") -> None`
- `fetch_by_category(category, dt_start=None, dt_end=None, granularity="hours", account="global", with_labels=False)`
  - Internally calls `fetch` for all slugs in the category.

Example:

    metrics.record("user_signup", category="auth")
    metrics.record("user_login", category="auth")
    cats = metrics.get_categories()  # {"auth", ...}
    slugs = metrics.get_category_slugs("auth")  # {"user_signup", "user_login"}

    data = metrics.fetch_by_category(
        "auth",
        granularity="days",
        with_labels=True,
    )


### Simple values (non time-series)

Store and read small global values that aren’t time-series (e.g., config flags, thresholds).

- `set_value(slug, value, account="global")`
- `get_value(slug, account="global", default=None) -> str | None`

Example:

    metrics.set_value("maintenance_mode", "off", account="public")
    mode = metrics.get_value("maintenance_mode", account="public", default="off")


### Accounts and permissions

Use “accounts” to segment metrics by tenant or domain: `account="public"`, `"global"`, `"group_123"`, etc.

- `list_accounts() -> list[str]`
- `add_account(account) -> bool`
- `delete_account(account) -> int` (removes account from the accounts index; does not delete time-series keys)
- `set_view_perms(account, perms)` / `get_view_perms(account)`
- `set_write_perms(account, perms)` / `get_write_perms(account)`
  - `perms` can be a string (single permission) or a list (comma-joined)
  - Use `"public"` to allow unauthenticated read/write via REST
- `get_accounts_with_permissions() -> list[{"account": str, "view_permissions": str|list|None, "write_permissions": str|list|None}]`

Notes:

- Python API does not enforce permissions; the REST layer does.
- Defaults differ:
  - Python API defaults `account="global"`.
  - REST endpoints default `account="public"`.


## REST API (optional)

If you expose the included views, you get a small JSON API:

Base route names below are illustrative; wire them under your desired URL prefixes.

- POST `record`
  - Body: slug (required), account="public", count=1, min_granularity="hours", max_granularity="years"
  - Increments a metric
  - Permissions: if account != "public", requires appropriate write permission

- GET `fetch`
  - Query: slugs=[...], dt_start, dt_end, account, granularity="hours"
  - Returns time-series; if one slug is passed, returns its series; otherwise returns labels+data
  - Permissions: enforced based on account

- GET/POST `series`
  - Query/Body: slugs="a,b,c", when, granularity, account
  - Returns single-bucket values for multiple slugs (uses `metrics.fetch_values`)

- POST `value/set`
  - Body: slug, value, account
  - Simple key/value set; permissions enforced for write

- GET `value/get`
  - Query: slugs="a,b,c", account, default
  - Batch get key/value; view permissions enforced

- GET `categories`
  - Query: account
  - Returns list of categories

- GET `category_slugs`
  - Query: category (required), account

- GET `category_fetch`
  - Query: category (required), dt_start, dt_end, account, granularity, with_labels
  - Returns time-series for all slugs in the category

- DELETE `category_delete`
  - Body: category (required), account
  - Deletes the category index (does not delete the underlying time-series keys)

- GET/POST/DELETE `permissions` and `permissions/<account>`
  - Manage or read view/write permissions per account


## Timezone, windows, and labels

- Timezone:
  - All implicit “now” calculations use the metrics timezone (see Config).
  - Passing `timezone=` to `metrics.record` or `metrics.fetch_values` adjusts the boundary calculation so events fall into the expected hour/day.

- Automatic windows (when dt_start/dt_end omitted in `fetch`):
  - The library picks a sensible range per granularity:
    - minutes: ~30 minutes
    - hours: ~24 hours
    - days: ~30 days
    - weeks: ~12 weeks
    - months: ~12 months (approx)
    - years: ~12 years (approx)

- Labels:
  - `with_labels=True` returns display-friendly period labels.
  - For hours → "HH:00", minutes → "HH:MM", others → "YYYY[-MM[-DD]]" depending on granularity.


## Retention (key expiry)

Defaults (can be changed in code if needed):

- minutes: 1 day
- hours: 3 days
- days: 360 days
- weeks: 360 days
- months: no expiry
- years: no expiry

These apply per-bucket as values are recorded. If you want different retention, adjust the map where your project vendors this module.


## Configuration

Add to your Django settings (via Mojo settings bridge) as needed:

- `METRICS_TIMEZONE` (default: "America/Los_Angeles")
  - Used when normalizing “now” and when naive datetimes are provided.

- `METRICS_DEFAULT_MIN_GRANULARITY` and `METRICS_DEFAULT_MAX_GRANULARITY`
  - Used by helper generation utilities; you can still pass explicit min/max to `metrics.record`.
  - Valid values: "minutes", "hours", "days", "weeks", "months", "years"


## Tips and patterns

- Use categories to build dashboards: record with `category="engagement"`, then `fetch_by_category("engagement", with_labels=True)`.
- For multi-tenant apps, set `account` per tenant and gate access with the permissions helpers.
- For totals-to-date (e.g., “today’s signups”), call `fetch_values(..., granularity="days")`.
- If you rely heavily on minute-level data, increase retention or export aggregates elsewhere regularly.
- Slug naming: prefer simple, stable names without spaces. Colons are supported but normalized internally.


## Redis and clustering notes

- Keys are hash-tagged per account (`{mets:<account>}`) for slot locality on Redis Cluster.
- Multi-key reads use a cluster-safe MGET that groups keys by slot to preserve order.
- Iterations use SCAN to avoid blocking and to work across cluster shards.

You don’t need to configure anything special; just point Mojo at your Redis (cluster or standalone) and go.


## Full reference (TL;DR)

- Time-series:
  - `metrics.record(slug, when=None, count=1, category=None, account="global", min_granularity="hours", max_granularity="years", timezone=None)`
  - `metrics.fetch(slug_or_slugs, dt_start=None, dt_end=None, granularity="hours", account="global", with_labels=False)`
  - `metrics.fetch_values(slugs, when=None, granularity="hours", account="global", timezone=None)`

- Categories:
  - `metrics.get_categories(account="global")`
  - `metrics.get_category_slugs(category, account="global")`
  - `metrics.fetch_by_category(category, dt_start=None, dt_end=None, granularity="hours", account="global", with_labels=False)`
  - `metrics.delete_category(category, account="global")`

- Simple values:
  - `metrics.set_value(slug, value, account="global")`
  - `metrics.get_value(slug, account="global", default=None)`

- Accounts & permissions:
  - `metrics.list_accounts()`, `metrics.add_account(account)`, `metrics.delete_account(account)`
  - `metrics.set_view_perms(account, perms)`, `metrics.get_view_perms(account)`
  - `metrics.set_write_perms(account, perms)`, `metrics.get_write_perms(account)`
  - `metrics.get_accounts_with_permissions()`

That’s it. Simple, fast, and production-friendly.