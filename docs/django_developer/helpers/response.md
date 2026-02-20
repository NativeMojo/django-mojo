# response — Django Developer Reference

## Import

```python
from mojo.helpers.response import JsonResponse
```

## JsonResponse

Drop-in replacement for Django's `JsonResponse` that:
- Auto-adds `code` and `server` (hostname) fields to every response
- Ensures data is serialized consistently as `objict`

```python
from mojo.helpers.response import JsonResponse

# Success
return JsonResponse({"status": True, "data": my_data})

# Error
return JsonResponse({"status": False, "error": "Not found"}, status=404)
```

## Convenience Helpers

```python
from mojo.helpers.response import error, success

# Error response
return error("Invalid input", status=400)

# Success response
return success({"id": 1, "name": "Test"})
```

## Standard Response Pattern in REST Handlers

```python
@md.GET('book/<int:pk>')
def on_book_detail(request, pk):
    book = Book.objects.filter(pk=pk).first()
    if not book:
        return error("Book not found", status=404)
    return book.on_rest_get(request)
```

For standard CRUD, let `MojoModel.on_rest_request()` handle all responses — only use `JsonResponse` directly for custom endpoints.
