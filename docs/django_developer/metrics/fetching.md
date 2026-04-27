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

## Settings

| Setting | Default | Description |
|---|---|---|
| `METRICS_TIMEZONE` | `"America/Los_Angeles"` | Default timezone for metric recording |
| `METRICS_TRACK_USER_ACTIVITY` | `False` | Auto-record per-user activity metrics |
