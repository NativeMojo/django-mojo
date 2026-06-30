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
| 401 | Not authenticated — request reached a permission-gated endpoint with no valid session |
| 403 | Authenticated but permission denied |
| 404 | Resource not found |
| 500 | Server error |

**401 vs 403:** Permission-gated endpoints return **401** for unauthenticated requests and **403** for authenticated requests that lack the required permission. Both include `"is_authenticated": false` or `true` respectively in the error envelope. Clients should redirect to login on 401 and show a "not authorized" message on 403.

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

## Owner Assignment on Create

When creating a record (POST without a pk), the framework automatically stamps the `user` field with the authenticated caller if the body omits it. If you include `user` in the body, that value is used instead — provided you have view access to the target user account. This lets callers with sufficient permissions create records on behalf of another user:

```json
{"user": 7, "code": "abc"}
```

If the body sends `null` or `0` for `user`, the framework treats it as omitted and falls back to the authenticated caller. Omitting the field entirely is the normal self-signup path. See the framework reference for per-model opt-out options.

## Client IP

The server records your IP address for rate limiting, geofencing, API-key `allowed_ips`
checks, audit logs, and login-anomaly detection. The recorded IP comes from the
`X-Real-IP` header set by the reverse proxy — **not** from `X-Forwarded-For`. Sending a
forged `X-Forwarded-For` header has no effect on the IP the server sees.
