# response — Django Developer Reference

## View Functions: Return Raw Dicts

In view functions (decorated with `@md.URL`, `@md.GET`, etc.), always return plain dicts. The framework wraps them automatically — no `JsonResponse` needed.

```python
# Success — data gets wrapped as {"status": True, "data": {...}}
return {"id": book.id, "title": book.title}

# Explicit success envelope — passed through as-is
return {"status": True, "id": book.id}

# Error envelope — passed through as-is
return {"status": False, "error": "Book not found"}

# Raise instead — auto-converted to 400/403
raise ValueError("Invalid input")
raise PermissionError("Access denied")
```

See [decorators.md — Return Values](../core/decorators.md#return-values) for the full wrapping rules.

## JsonResponse (low-level)

`JsonResponse` is mojo's drop-in replacement for Django's `JsonResponse`. It auto-adds `code` and `server` fields to every response.

```python
from mojo.helpers.response import JsonResponse
```

Use it only outside of view functions — in middleware, custom decorators, or utility code where you need direct control over the HTTP response:

```python
# In a custom decorator
def requires_player(fn):
    def wrapper(request, *args, **kwargs):
        if not getattr(request, "player", None):
            return JsonResponse({"error": "Player auth required", "status": False}, status=401)
        return fn(request, *args, **kwargs)
    return wrapper
```

## Convenience Helpers

```python
from mojo.helpers.response import error, success

return error("Invalid input", status=400)
return success({"id": 1, "name": "Test"})
```

These are also for non-view contexts. In a normal view, just return a dict.
