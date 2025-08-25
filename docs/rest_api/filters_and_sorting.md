# Filtering & Sorting in the MOJO REST API

Get exactly the data you need—MOJO's REST API lets you filter, search, and sort results directly from your requests.

---

## Filtering: Narrow Down Your Results

You can filter your queries by including field-value pairs as query parameters (for GET requests) or in the request body (for POST/PUT). The API will return only objects matching all your filters.

**Examples:**

- Get all active projects:
  ```
  GET /api/projects/?is_active=true
  ```
- Get users created after a specific date:
  ```
  GET /api/users/?created__gte=2024-01-01
  ```
- Get items by name (partial match):
  ```
  GET /api/items/?name__icontains=widget
  ```

### Common Filter Methods

- `field=value` — Exact match
- `field__icontains=value` — Case-insensitive substring match
- `field__gte=value` — Greater than or equal to (`date`, `number`)
- `field__lte=value` — Less than or equal to
- `field__in=value1,value2` — Multiple possible values

> _Tip: Use field double-underscore (`__`) lookups for advanced filters—just like Django’s ORM._

---

## Sorting: Control Your Result Order

Use the `sort` parameter to set sort order for your results.

**Examples:**

- Sort projects by name (A → Z):
  ```
  GET /api/projects/?sort=name
  ```
- Sort users by date created (newest first):
  ```
  GET /api/users/?sort=-created
  ```

> _Tip: Prefix the field with `-` for descending order. You can also sort by most fields that are listed in the API model._

---

## Combined Filtering and Sorting

You can combine filters and sorting in a single request.

**Example:**
```
GET /api/projects/?is_active=true&sort=-created&name__icontains=widget
```

---

## Date Range Filters

MOJO also supports easy date-range filtering with standard field lookups.

- Projects between two dates:
  ```
  GET /api/projects/?created__gte=2024-01-01&created__lte=2024-02-01
  ```
- Many endpoints also support shortcut query params like `dr_start`, `dr_end`, `dr_field` for date-range filtering.

---

## Using POST for Complex Filters

You can PUT/POST JSON bodies with filter keys for more advanced or longer filter sets:

```
POST /api/users/
Content-Type: application/json

{
  "email__icontains": "example.com",
  "is_staff": true,
  "sort": "-created",
  "size": 100
}
```

---

## Search: Full Text and Field Contains

Many resources support text searching via a special parameter (often `search` or by using `field__icontains`).

**Example:**
```
GET /api/users/?search=alex
```
or
```
GET /api/users/?username__icontains=alex
```

---

## Quick Tips

- Not all fields may be filterable—check your API’s docs or try representative names.
- Use both filters and sorting for paginated, efficient client workflows.
- For large datasets, always use pagination (`size` and `start`) with your filters.
- Complex filter logic (AND/OR/combo filters) may require separate requests.

---

**Happy querying, filtering, and sorting with MOJO!**