# Filtering, Searching & Sorting — REST API Reference

All list endpoints support these query parameters.

## Field Filters

Filter by exact field value:

```
GET /api/myapp/book?status=published
GET /api/myapp/book?author=5
```

### Filter Operators

Append an operator suffix to the field name:

| Suffix | Example | Behavior |
|---|---|---|
| *(none)* | `?status=active` | Exact match |
| `__in` | `?status__in=active,pending` | Matches any value in comma-separated list |
| `__not` | `?status__not=deleted` | Excludes this value |
| `__not_in` | `?status__not_in=deleted,archived` | Excludes all values in list |
| `__isnull` | `?author__isnull=true` | NULL check |

```
GET /api/myapp/book?status__in=published,draft
GET /api/myapp/book?author__isnull=false
GET /api/myapp/book?status__not=deleted
```

### Null Values

```
GET /api/myapp/book?author=null
```

### Relation / Foreign Key Filters

Filter through related fields using `__` notation:

```
GET /api/myapp/book?author__id=5
GET /api/myapp/book?author__username=alice
```

## Date Range Filter

```
GET /api/myapp/book?dr_start=2024-01-01&dr_end=2024-12-31
GET /api/myapp/book?dr_field=modified&dr_start=2024-06-01
```

| Param | Description |
|---|---|
| `dr_field` | Field to filter on (default: `created`) |
| `dr_start` | Start datetime (inclusive) |
| `dr_end` | End datetime (inclusive) |

Dates accept ISO 8601: `2024-01-01` or `2024-01-01T00:00:00Z`

## Text Search

```
GET /api/myapp/book?search=django
```

### Advanced Search Syntax

| Syntax | Example | Behavior |
|---|---|---|
| Single term | `search=django` | Matches any search field containing "django" |
| Multiple terms | `search=django rest` | AND: both terms must match |
| Quoted phrase | `search="django rest"` | Exact phrase match |
| Exclusion | `search=django -python` | Contains "django" but not "python" |
| Field-specific | `search=title:django` | Search only in `title` field |

```
GET /api/myapp/book?search="machine learning" -python author:alice
```

## Sorting

```
GET /api/myapp/book?sort=title
GET /api/myapp/book?sort=-created    # descending (prefix with -)
```

Default sort is `-id` (newest first) unless the model specifies otherwise.

## Combining Parameters

```
GET /api/myapp/book?status=published&sort=-created&search=django&dr_start=2024-01-01&start=0&size=20
```
