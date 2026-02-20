# Middleware — Django Developer Reference

## Required Middleware Stack

Add to `MIDDLEWARE` in `settings.py` in this order:

```python
MIDDLEWARE = [
    "mojo.middleware.cors.CORSMiddleware",
    "mojo.middleware.mojo.MojoMiddleware",
    "mojo.middleware.auth.AuthMiddleware",
    # ... other middleware
]
```

---

## MojoMiddleware

**File:** `mojo/middleware/mojo.py`

Sets up the request object for all downstream handlers.

**What it does:**
- Sets `request.DATA` — unified `objict` containing all parsed GET params, POST body, and JSON body
- Sets `request.group = None` (populated later by auth or decorator if group param present)
- Sets `request.device_id` from `X-Device-Id` header or `device_id` param
- Sets `request.ip` — real client IP (respects `X-Forwarded-For`)
- Sets `request.user_agent` — parsed UA string
- Sets cache-control headers on response (`no-cache`, `no-store`)
- Sets a `ContextVar` (`ACTIVE_REQUEST`) so model methods can access the request without passing it explicitly

**Always use `request.DATA`** — never `request.POST.get()` or `request.GET.get()`.

```python
# Correct
value = request.DATA.get("field_name")
value = request.DATA.field_name  # dot notation also works

# Wrong
value = request.POST.get("field_name")
value = request.GET.get("field_name")
```

---

## AuthMiddleware

**File:** `mojo/middleware/auth.py`

Validates Bearer tokens and populates `request.user`.

**What it does:**
- Parses the `Authorization` header (e.g., `Bearer <token>`)
- Looks up the registered bearer handler for the token type
- Calls the handler to resolve the token to a user
- Sets `request.user` (authenticated `User` instance or anonymous)
- Default handler: `User.validate_jwt(token)` for `Bearer` tokens

**Custom bearer handlers** can be registered via settings to support API keys, service tokens, etc.

**`request.group`** is auto-populated if the request includes a `group` param that matches a group the user belongs to.

---

## CORSMiddleware

**File:** `mojo/middleware/cors.py`

Handles Cross-Origin Resource Sharing for browser clients.

**Behavior:**
- Allows all origins (`*`)
- Allows all methods: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS
- Handles OPTIONS preflight requests (returns 200 immediately)
- 24-hour preflight cache (`Access-Control-Max-Age: 86400`)
- Allows custom headers including `X-Api-Key`, `X-Device-Id`

No configuration needed — works out of the box.

---

## request.DATA Reference

`request.DATA` is an `objict` instance (dict subclass with attribute access).

| Source | Merged into request.DATA |
|---|---|
| Query string (`?key=val`) | Yes |
| POST form fields | Yes |
| JSON body | Yes (merged at top level) |
| Multipart files | Available via `request.FILES` |

**Access patterns:**

```python
request.DATA.get("name")           # standard dict get with optional default
request.DATA.name                  # attribute access
request.DATA.get("size", 10)       # with default
request.DATA.get_typed("size", 10, int)  # with type coercion
```

**Nested data** (dot notation keys are auto-expanded):

```python
# request with key "user.name" = "Alice"
request.DATA.user.name  # → "Alice"
```

**Array notation** (`tags[]` or `tags[0]`) is automatically converted to a list.

---

## ACTIVE_REQUEST ContextVar

The current request is stored in a `ContextVar` accessible anywhere in the call stack:

```python
from mojo.models.rest import ACTIVE_REQUEST

request = ACTIVE_REQUEST.get()  # None if outside a request context
```

Model methods access it via `self.active_request` and `self.active_user` properties — no need to pass the request explicitly through service layers.
