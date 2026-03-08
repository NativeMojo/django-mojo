# Decorators — Django Developer Reference

Import all decorators from the `mojo.decorators` package:

```python
import mojo.decorators as md
```

## Routing Decorators

### `@md.URL(path)`
Registers the function for all HTTP methods at the given path.

### `@md.GET(path)`, `@md.POST(path)`, `@md.PUT(path)`, `@md.DELETE(path)`
Method-specific route registration.

**Path conventions:**
- Relative paths are auto-prefixed with the app's name (the Django app directory name): `book` → `/api/myapp/book`
- Absolute paths starting with `/` bypass the prefix: `/health` → `/health`
- Path parameters use Django URL syntax: `book/<int:pk>`, `item/<str:slug>`
- List endpoints must NOT end with trailing slashes

```python
# Standard CRUD endpoint
@md.URL('book')
@md.URL('book/<int:pk>')
def on_book(request, pk=None):
    return Book.on_rest_request(request, pk)

# Method-specific
@md.GET('book/export')
def on_book_export(request):
    ...

# Absolute path (bypasses app prefix)
@md.GET('/health')
def health_check(request):
    ...
```

## CRITICAL: URL Path Rules

### Auto-prefix uses the app directory name

The prefix is the **Django app directory name**, not a parent package or project name.

```
app directory: wallet/
@md.URL('session')  →  /api/wallet/session    ✓
@md.URL('wallet')   →  /api/wallet/wallet     ✓  (yes, repeated — that's correct)

NEVER: /api/mojopay/session  (parent package name — wrong)
```

If your app lives at `myproject/apps/wallet/`, the prefix is `wallet`, not `myproject` or `apps`.

### Dynamic segments go at the END only

Never put `<int:pk>` or any dynamic segment in the middle of a path.

```python
# Correct — dynamic pk at the end
@md.URL('book')
@md.URL('book/<int:pk>')

# WRONG — dynamic segment in the middle
@md.URL('book/<int:pk>/chapters')   # ✗ never do this
@md.URL('user/<int:pk>/settings')   # ✗ never do this
```

For nested resources, use POST data or query params instead:

```python
# Correct — pass parent id as a param
@md.GET('book/chapters')
def on_chapters(request):
    book_id = request.DATA.get_typed("book_id", 0, int)
    ...
```

### Prefer POST_SAVE_ACTIONS over custom endpoints for model operations

When an operation acts on a specific model instance (test connection, clone, send, approve), use `POST_SAVE_ACTIONS` + `on_action_<name>` instead of a dedicated REST endpoint. This keeps the API surface small and consistent.

```python
# Preferred — action on existing instance via POST_SAVE_ACTIONS
POST /api/myapp/integration/42
{"test_connection": true}

# Avoid — dedicated endpoint just for one action
@md.POST('integration/test_connection')   # ✗ unnecessary endpoint
```

See [mojo_model.md — POST_SAVE_ACTIONS](mojo_model.md#post_save_actions) for the full pattern.

## Authentication Decorators

### `@md.requires_auth`
Rejects unauthenticated requests with 401.

```python
@md.GET('profile')
@md.requires_auth
def on_profile(request):
    return request.user.on_rest_get(request)
```

### `@md.requires_perms(*perms)`
Checks that the authenticated user (or group) has all listed permissions.

```python
@md.POST('admin/reset')
@md.requires_perms("admin_access")
def on_admin_reset(request):
    ...
```

### `@md.requires_group_perms(*perms)`
Like `requires_perms` but forces group-context permission evaluation.

### `@md.requires_bearer`
Validates that a Bearer token is present and valid.

## Security Annotation Decorators

These decorators do not enforce access — they annotate the function in the security registry for auditing and documentation purposes.

| Decorator | Purpose |
|---|---|
| `@md.public_endpoint()` | Explicitly marks endpoint as public (no auth) |
| `@md.custom_security()` | Marks endpoint as having custom security logic |
| `@md.uses_model_security()` | Indicates endpoint uses `RestMeta` permission system |
| `@md.token_secured()` | Marks endpoint as requiring a token |

```python
@md.GET('status')
@md.public_endpoint()
def on_status(request):
    return {"status": True, "version": "1.0"}
```

## Validation Decorators

### `@md.requires_params(*params)`
Returns 400 if any listed params are missing from `request.DATA`.

```python
@md.POST('book/publish')
@md.requires_params("book_id", "publish_date")
def on_publish(request):
    book = Book.objects.get(pk=request.DATA.book_id)
    ...
```

## Cron Decorators

### `@md.cron(schedule)`
Registers a function as a cron task.

```python
@md.cron("0 * * * *")  # every hour
def hourly_cleanup():
    ...
```

## Return Values

All routed view functions should return plain dicts — never construct `JsonResponse` manually. The framework wraps them automatically:

| Return value | Response sent to client |
|---|---|
| `{"name": "Joe"}` | `{"status": True, "code": 200, "data": {"name": "Joe"}}` |
| `[item1, item2]` | `{"status": True, "code": 200, "data": [...], "size": 2}` |
| `{"status": False, "error": "not found"}` | passed through as-is |
| `{"status": True, "data": {...}}` | passed through as-is |
| raise `ValueError("bad input")` | `{"status": False, "error": "bad input", "code": 400}` |
| raise `PermissionError("denied")` | `{"status": False, "error": "denied", "code": 403}` |

**Always return raw dicts. Never import or use `JsonResponse` in view functions.**

```python
# Good — return plain data
@md.GET('book/stats')
@md.requires_auth
def on_book_stats(request):
    return {"total": Book.objects.count(), "active": Book.objects.filter(status="active").count()}

# Good — explicit error envelope
@md.POST('book/publish')
@md.requires_params("book_id")
def on_publish(request):
    book = Book.objects.filter(pk=request.DATA.book_id).first()
    if not book:
        return {"status": False, "error": "Book not found"}
    book.publish()
    return {"status": True, "id": book.id}

# Good — raise for error conditions (auto-converted to 400/403/500)
@md.POST('book/approve')
@md.requires_auth
def on_approve(request):
    book = Book.objects.get(pk=request.DATA.book_id)
    if not request.user.has_permission("approve_books"):
        raise PermissionError("Approval permission required")
    book.status = "approved"
    book.save()
    return {"status": True}
```

## Error Handling

All routed functions are automatically wrapped with error handling that:
- Catches unhandled exceptions and returns a JSON error response
- Reports exceptions to the incident system
- Tracks error metrics

## Rate Limiting & Metrics Decorators

See [Rate Limiting & Endpoint Metrics](rate_limiting.md) for the full reference on:

- `@md.rate_limit` — fixed-window limits (general API throughput)
- `@md.strict_rate_limit` — sliding-window limits (login, password reset, MFA)
- `@md.endpoint_metrics` — per-endpoint usage tracking by IP, duid, api_key, user, or group

## Decorator Stacking Order

When stacking multiple decorators, routing decorators (`@md.URL`, `@md.GET`, etc.) go first (outermost), followed by auth/validation:

```python
@md.POST('resource')
@md.requires_auth
@md.requires_params("name")
def on_create(request):
    ...
```
