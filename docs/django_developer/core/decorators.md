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
- Relative paths are prefixed with the app's URL prefix (e.g., `book` → `/api/myapp/book`)
- Absolute paths starting with `/` bypass app prefixes (e.g., `/health` → `/health`)
- Path parameters use Django URL syntax: `book/<int:pk>`, `item/<str:slug>`

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

# Absolute path
@md.GET('/health')
def health_check(request):
    ...
```

**Note:** List endpoints must NOT end with trailing slashes.

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
    return JsonResponse({"status": "ok"})
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

## Error Handling

All routed functions are automatically wrapped with error handling that:
- Catches unhandled exceptions and returns a JSON error response
- Reports exceptions to the incident system
- Tracks error metrics

## Decorator Stacking Order

When stacking multiple decorators, routing decorators (`@md.URL`, `@md.GET`, etc.) go first (outermost), followed by auth/validation:

```python
@md.POST('resource')
@md.requires_auth
@md.requires_params("name")
def on_create(request):
    ...
```
