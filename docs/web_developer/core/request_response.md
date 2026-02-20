# Request & Response Format — REST API Reference

## Sending Data

All endpoints accept data as:

- **Query string** for GET requests: `?name=value`
- **JSON body** for POST/PUT: `Content-Type: application/json`
- **Form data** for POST: `Content-Type: application/x-www-form-urlencoded`

All three are merged and treated identically by the server.

```bash
# GET with query params
curl -H "Authorization: Bearer <token>" \
     "https://api.example.com/api/myapp/book?status=published"

# POST with JSON body
curl -X POST \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"title": "My Book", "status": "draft"}' \
     https://api.example.com/api/myapp/book
```

## Response Envelope

Every response is wrapped in a standard envelope.

### Success — Single Object

```json
{
  "status": true,
  "data": {
    "id": 1,
    "title": "My Book",
    "created": "2024-01-15T10:30:00Z"
  }
}
```

### Success — List

```json
{
  "status": true,
  "count": 42,
  "start": 0,
  "size": 10,
  "data": [
    {"id": 1, "title": "Book One"},
    {"id": 2, "title": "Book Two"}
  ]
}
```

### Error

```json
{
  "status": false,
  "code": 403,
  "error": "Permission denied",
  "is_authenticated": true
}
```

## HTTP Status Codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 400 | Bad request / validation error |
| 401 | Not authenticated |
| 403 | Authenticated but permission denied |
| 404 | Resource not found |
| 500 | Server error |

## Dates

All datetimes are returned in ISO 8601 UTC format: `"2024-01-15T10:30:00Z"`

When sending dates, ISO 8601 format is accepted: `"2024-01-15"` or `"2024-01-15T10:30:00Z"`

## Null Values

Use `null` in JSON for empty/unset values. Empty string `""` for numeric fields is treated as `0`.

## Foreign Key Fields

To set a foreign key, send the integer ID:

```json
{"author": 5}
```

To clear a foreign key:

```json
{"author": null}
```
