# Request & Response Format — REST API Reference

## Sending Data

All endpoints accept data as:

- **Query string** for GET requests: `?name=value`
- **JSON body** for POST/PUT: `Content-Type: application/json`
- **Form data** for POST: `Content-Type: application/x-www-form-urlencoded`

All three are merged into a single payload. If the same key appears in more
than one source, the later source wins, replacing the value whole: **query
string < form body < JSON body**. So a JSON body key always beats the same key
in the query string (e.g. `POST /api/thread/1?group=5` with body
`{"group": 7}` resolves `group` to `7`). Repeating a key *within* the query
string (`?tag=a&tag=b`) still produces an array.

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
| 503 | Service temporarily unavailable — retry according to `Retry-After` |
| 500 | Server error |

**401 vs 403:** Permission-gated endpoints return **401** for unauthenticated requests and **403** for authenticated requests that lack the required permission. Both include `"is_authenticated": false` or `true` respectively in the error envelope. Clients should redirect to login on 401 and show a "not authorized" message on 403.

**Malformed `Authorization` header never 500s.** If the header value isn't exactly
`<scheme> <token>` (e.g. a bare token with no scheme, an empty header, or extra
whitespace-separated parts), the server treats the request as unauthenticated rather than
erroring — a permission-gated endpoint responds normally with **401**, and a public endpoint
still succeeds.

### Temporary database unavailability

Deployments using the bounded ASGI database pool can return this response if
all database leases are briefly occupied, including while authenticating the
request:

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 1
Content-Type: application/json

{"status":false,"error":"Database temporarily unavailable","code":503}
```

This is a transient service-capacity response, not an authentication failure.
Wait at least the number of seconds in `Retry-After` before retrying. Keep
retries bounded; do not immediately fan out or retry every failed request at
once.

## HTML Error Pages — and why they will not reach you

The server ships styled HTML pages for 400, 403, 404, 500, 503 and the unconfigured
root, so a person who mistypes a URL sees a readable page instead of a JSON blob.

**Nothing about the JSON API changed.** The error envelope above, its fields, its
status codes and its exact bytes are the same as before those pages existed. The
server decides purely from your `Accept` header, and errs toward JSON in every
ambiguous case:

| `Accept` you send | What you get |
|---|---|
| *(no header)* | JSON |
| `*/*` | JSON |
| `application/json` (or any `*+json`) | JSON — **decisive, even alongside `text/html`** |
| `application/json, text/plain, */*` (axios/fetch defaults) | JSON |
| `text/html,*/*` (equal quality) | JSON |
| `text/html,application/xhtml+xml,…,*/*;q=0.8` (a browser address bar) | HTML page |

In short: you get an HTML page only when you ask for `text/html` **specifically** and
name no JSON type. Every HTTP client that sends `*/*` — curl, `requests`, Go's
`http`, most uptime monitors — keeps getting JSON. If you want to be certain, send
`Accept: application/json`.

Two consequences worth knowing:

- **A browser tab pointed at an API URL now renders a page**, not raw JSON. That is
  expected; it does not mean the endpoint changed.
- **HTML pages always carry the true HTTP status**, even on deployments running
  `MOJO_APP_STATUS_200_ON_ERROR` (which folds JSON error responses to HTTP 200 for
  clients that can't handle error statuses). That shim still applies to your JSON
  responses exactly as before.

The 500 page shows a reference like `REF · 48213`. That is the incident id — quote it
when reporting a failure. It is the only detail the page carries; the stack trace and
request data live on the access-controlled incident record, not in the response.

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

## Batch Create/Update

Some list endpoints accept a `batched` array to create/update several rows in one POST — check the endpoint's docs for whether it supports this. Each item without an `id`/`pk` is created; each item with one is updated:

```json
{"batched": [{"title": "New"}, {"id": 5, "title": "Updated"}]}
```

The response wraps successes under `data.items` (`data.count` = number saved) and any per-row failures — including a row you don't have permission to write (`"error": "permission denied"`), or a row whose verb the model has disabled (`"error": "UPDATE not allowed"` / `"CREATE not allowed"`) — under `data.errors`, e.g. `{"index": 1, "error": "permission denied"}`. A denied, disabled, or invalid row is skipped, not fatal to the rest of the batch. See the framework reference for the full permission model: [Batch Save Permissions](../../django_developer/rest/permissions.md#batch-save-permissions).

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
