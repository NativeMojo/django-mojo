# REST API URL Routing

Django-MOJO provides flexible URL routing for REST APIs with support for automatic prefixing and absolute paths.

## Table of Contents

- [Overview](#overview)
- [Configuration](#configuration)
- [URL Patterns](#url-patterns)
- [Absolute Paths](#absolute-paths)
- [Examples](#examples)
- [Migration Guide](#migration-guide)

## Overview

Django-MOJO's REST routing system supports two modes:

1. **Manual Prefix Mode** (default, `REST_AUTO_PREFIX=False`)
   - Django project controls the MOJO_PREFIX
   - Traditional Django URL configuration
   - Backward compatible with existing projects

2. **Auto Prefix Mode** (`REST_AUTO_PREFIX=True`)
   - MOJO framework handles MOJO_PREFIX automatically
   - Absolute paths (starting with `/`) bypass MOJO_PREFIX
   - Cleaner Django project configuration

## Configuration

### Settings

```python
# settings.py

# Enable automatic MOJO_PREFIX handling (default: False)
REST_AUTO_PREFIX = True

# API prefix (default: "api")
MOJO_PREFIX = "api"

# REST API module name (default: "rest")
MOJO_API_MODULE = "rest"

# Append trailing slash to URLs (default: False)
MOJO_APPEND_SLASH = False
```

### Django Project URLs

#### Manual Prefix Mode (REST_AUTO_PREFIX=False)

```python
# urls.py - Django project
from django.urls import path, include
from mojo.helpers.settings import settings

MOJO_PREFIX = settings.get("MOJO_PREFIX", "api").strip("/") + "/"

urlpatterns = [
    path("", my_homepage_view),
    path(MOJO_PREFIX, include('mojo.urls')),  # You control the prefix
]
```

#### Auto Prefix Mode (REST_AUTO_PREFIX=True)

```python
# urls.py - Django project
from django.urls import path, include

urlpatterns = [
    path("", my_homepage_view),
    path("", include('mojo.urls')),  # MOJO handles prefix automatically
]
```

## URL Patterns

### Registering REST Endpoints

Use the `@md.URL()`, `@md.GET()`, `@md.POST()`, etc. decorators:

```python
# myapp/rest.py
from mojo import decorators as md
from mojo.helpers.response import JsonResponse

@md.GET("users")
def get_users(request):
    """List all users"""
    return JsonResponse({"users": [...]})

@md.POST("users")
def create_user(request):
    """Create a new user"""
    return JsonResponse({"user": {...}})

@md.GET("users/<int:user_id>")
def get_user(request, user_id):
    """Get specific user"""
    return JsonResponse({"user": {...}})
```

### URL Pattern Types

#### 1. Relative Paths (No leading slash)

```python
@md.GET("users")
@md.GET("orders/pending")
@md.POST("auth/login")
```

These get the app prefix automatically.

#### 2. Absolute Paths (Leading slash)

```python
@md.GET("/health")
@md.POST("/webhook/stripe")
@md.GET("/public/status")
```

These bypass the app prefix (and MOJO_PREFIX if `REST_AUTO_PREFIX=True`).

### Resulting URLs

#### Manual Prefix Mode (REST_AUTO_PREFIX=False)

```python
# In myapp/rest.py

@md.GET("users")
# → /api/myapp/users

@md.GET("/health")
# → /api/health  (bypasses app prefix only)
```

#### Auto Prefix Mode (REST_AUTO_PREFIX=True)

```python
# In myapp/rest.py

@md.GET("users")
# → /api/myapp/users

@md.GET("/health")
# → /health  (bypasses both app prefix AND MOJO_PREFIX)
```

## Absolute Paths

Absolute paths (starting with `/`) are useful for:

1. **Global endpoints** that shouldn't be namespaced under an app
2. **Webhooks** from external services
3. **Health checks** and monitoring endpoints
4. **Public APIs** that need clean URLs

### Examples

```python
# myapp/rest.py
from mojo import decorators as md
from mojo.helpers.response import JsonResponse

# Regular endpoint - gets app prefix
@md.GET("orders")
def get_orders(request):
    # URL: /api/myapp/orders
    return JsonResponse({"orders": []})

# Absolute endpoint - bypasses app prefix
@md.GET("/health")
def health_check(request):
    # Manual mode: /api/health
    # Auto mode: /health
    return JsonResponse({"status": "ok"})

# Webhook - bypasses app prefix
@md.POST("/webhook/stripe")
def stripe_webhook(request):
    # Manual mode: /api/webhook/stripe
    # Auto mode: /webhook/stripe
    return JsonResponse({"received": True})
```

## Examples

### Example 1: Basic App with Relative Paths

```python
# myapp/rest.py
from mojo import decorators as md
from mojo.helpers.response import JsonResponse

@md.GET("users")
def list_users(request):
    return JsonResponse({"users": [...]})

@md.POST("users")
def create_user(request):
    return JsonResponse({"user": {...}})

@md.GET("users/<int:user_id>")
def get_user(request, user_id):
    return JsonResponse({"user": {...}})

@md.DELETE("users/<int:user_id>")
def delete_user(request, user_id):
    return JsonResponse({"deleted": True})
```

**Resulting URLs:**
- Manual mode: `/api/myapp/users`, `/api/myapp/users/123`
- Auto mode: `/api/myapp/users`, `/api/myapp/users/123`

### Example 2: Mixed Relative and Absolute Paths

```python
# myapp/rest.py
from mojo import decorators as md
from mojo.helpers.response import JsonResponse

# App-specific endpoints (relative)
@md.GET("dashboard")
def get_dashboard(request):
    return JsonResponse({"dashboard": {...}})

@md.POST("reports")
def create_report(request):
    return JsonResponse({"report": {...}})

# Global endpoints (absolute)
@md.GET("/health")
def health_check(request):
    return JsonResponse({"status": "healthy"})

@md.POST("/webhook/github")
def github_webhook(request):
    return JsonResponse({"received": True})

@md.GET("/api-docs")
def api_documentation(request):
    return JsonResponse({"version": "1.0", "endpoints": [...]})
```

**Resulting URLs (Manual mode):**
- `/api/myapp/dashboard` (app-prefixed)
- `/api/myapp/reports` (app-prefixed)
- `/api/health` (bypasses app prefix)
- `/api/webhook/github` (bypasses app prefix)
- `/api/api-docs` (bypasses app prefix)

**Resulting URLs (Auto mode):**
- `/api/myapp/dashboard` (app-prefixed)
- `/api/myapp/reports` (app-prefixed)
- `/health` (bypasses everything)
- `/webhook/github` (bypasses everything)
- `/api-docs` (bypasses everything)

### Example 3: Custom App Prefix

```python
# myapp/rest.py
from mojo import decorators as md

# Override default app name prefix
APP_NAME = "v1"  # Instead of "myapp"

@md.GET("users")
def list_users(request):
    # URL: /api/v1/users
    return JsonResponse({"users": [...]})
```

### Example 4: Regex URL Patterns

```python
# myapp/rest.py
from mojo import decorators as md

@md.GET("^items/(?P<slug>[a-z0-9-]+)$")
def get_item_by_slug(request, slug):
    """
    Match: /api/myapp/items/my-item-slug
    """
    return JsonResponse({"item": {...}})

@md.GET("^archive/(?P<year>\d{4})/(?P<month>\d{2})$")
def get_archive(request, year, month):
    """
    Match: /api/myapp/archive/2025/01
    """
    return JsonResponse({"archive": [...]})
```

## Migration Guide

### Migrating to Auto Prefix Mode

If you want to migrate from manual to auto prefix mode:

#### Step 1: Enable REST_AUTO_PREFIX

```python
# settings.py
REST_AUTO_PREFIX = True
MOJO_PREFIX = "api"
```

#### Step 2: Update Django Project URLs

**Before:**
```python
# urls.py
MOJO_PREFIX = "api/"
urlpatterns = [
    path(MOJO_PREFIX, include('mojo.urls')),
]
```

**After:**
```python
# urls.py
urlpatterns = [
    path("", include('mojo.urls')),
]
```

#### Step 3: Review Absolute Paths

If you have absolute paths that you want to keep under `/api/`:

**Before (manual mode):**
```python
@md.GET("/webhook/stripe")
# → /api/webhook/stripe
```

**After (auto mode):**
```python
@md.GET("/webhook/stripe")
# → /webhook/stripe (bypasses /api/)
```

If you want it under `/api/`, use a relative path:
```python
@md.GET("webhook/stripe")
# → /api/myapp/webhook/stripe
```

Or create a dedicated webhook app without prefix:
```python
# webhooks/rest.py
APP_NAME = "webhook"

@md.GET("stripe")
# → /api/webhook/stripe
```

### Backward Compatibility

The default mode (`REST_AUTO_PREFIX=False`) maintains full backward compatibility. Existing projects will continue to work without any changes.

## Best Practices

1. **Use relative paths** for app-specific endpoints
2. **Use absolute paths** for:
   - Global health checks
   - Webhooks from external services
   - Public APIs that need clean URLs
   - Cross-app shared endpoints
3. **Enable AUTO_PREFIX mode** for new projects
4. **Keep MOJO_PREFIX** short and consistent (e.g., "api", "v1")
5. **Document your URL structure** clearly for API consumers

## Troubleshooting

### URLs not matching

Check:
1. `REST_AUTO_PREFIX` setting
2. Django project URL configuration
3. Leading slash on absolute paths
4. `MOJO_APPEND_SLASH` setting

### Absolute URLs still getting prefixed

Ensure:
1. Path starts with `/` in decorator: `@md.GET("/health")`
2. `REST_AUTO_PREFIX=True` if you want to bypass MOJO_PREFIX
3. MOJO framework is up to date

### URL conflicts

If multiple apps define the same absolute path, the last one loaded wins. Use:
1. Unique absolute paths
2. Relative paths with app prefixes
3. Custom `APP_NAME` to avoid conflicts
