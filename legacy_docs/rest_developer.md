# MOJO REST Developer Guide

This guide explains the MOJO REST framework including the enhanced request object, graphs system, and REST endpoint development patterns.
It is intended for Django/MOJO backend developers. For REST API consumers, see the docs in `rest_api/`.

---

## Enhanced Request Object

The MOJO framework enhances Django's request object with additional attributes and intelligent data parsing.

### Request Data: request.DATA

All incoming request data (GET params, POST body, JSON payloads) is parsed and available as `request.DATA`, which is an **objict** instance - a smart dictionary that supports both dict-style and attribute-style access.

```python
@md.POST('projects')
def create_project(request):
    # Access data multiple ways
    name = request.DATA.name              # Attribute access
    name = request.DATA["name"]           # Dict access
    name = request.DATA.get("name")       # Safe get with default
    
    # Nested access works seamlessly
    owner_email = request.DATA.owner.email
    
    # Check existence
    if request.DATA.description:
        # Has description field
        pass
```

**objict features:**
- **Attribute access**: `request.DATA.field_name`
- **Safe access**: Returns `None` for missing keys instead of raising KeyError
- **Nested access**: `request.DATA.user.profile.avatar`
- **Type conversion**: `.get_typed("count", typed=int)`
- **All dict methods**: `.keys()`, `.values()`, `.items()`, etc.

See [objict.md](objict.md) for complete objict documentation.

### Request Attributes

The MOJO middleware adds several useful attributes to every request:

#### Authentication & User Context

```python
# User object (from JWT Bearer token or anonymous)
request.user              # User instance or ANONYMOUS_USER objict
request.user.username     # Access user properties
request.user.is_authenticated  # Check if authenticated

# Group context (multi-tenant)
request.group             # Group instance if ?group=<id> was passed
request.group.name        # Access group properties

# Bearer token info
request.bearer            # Token type prefix ("bearer", "api_key", etc.)
request.auth_token        # objict(prefix="bearer", token="...")
```

**Anonymous User:**
```python
# When not authenticated, request.user is:
ANONYMOUS_USER = objict(
    display_name="Anonymous",
    username="anonymous", 
    email="anonymous@example.com",
    is_authenticated=False,
    has_permission=lambda: False
)
```

#### Request Metadata

```python
# Client information
request.ip                # Client IP address (handles proxies/CDN)
request.user_agent        # User agent string
request.duid              # Device unique ID (from cookie/header)

# Request tracking
request.started_at        # Unix timestamp when request started
request.request_log       # Request log instance (if logging enabled)

# Device context  
request.device            # UserDevice instance (tracked automatically for authenticated users)
```

### Device Tracking

MOJO automatically tracks devices for authenticated users. Each unique device is identified by a `duid` (device unique ID) from a cookie/header, or falls back to a hash of the user agent string.

```python
@md.POST('login')
def login(request):
    user = authenticate(request.DATA.username, request.DATA.password)
    
    # Device is automatically tracked after authentication
    # Access device information
    if request.device:
        print(f"Device ID: {request.device.duid}")
        print(f"Last IP: {request.device.last_ip}")
        print(f"First seen: {request.device.first_seen}")
        print(f"Device info: {request.device.device_info}")
        
        # Device info includes parsed user agent:
        # {
        #     "browser": "Chrome",
        #     "browser_version": "120.0",
        #     "os": "macOS",
        #     "os_version": "14.2",
        #     "device_type": "Desktop"
        # }
```

**Device Tracking Features:**
- Automatically tracks unique devices per user
- Stores device information (browser, OS, device type from user agent)
- Records IP addresses used by each device
- Tracks first seen and last seen timestamps
- Links devices to geolocation data via IP addresses

**Manual Device Tracking:**
```python
from mojo.apps.account.models import UserDevice

# Track device manually (usually automatic via middleware)
device = UserDevice.track(request, user=request.user)

# Access device locations (IP history)
for location in device.locations.all():
    print(f"IP: {location.ip_address}")
    print(f"Location: {location.geolocation.city}, {location.geolocation.country}")
    print(f"Last seen: {location.last_seen}")
```

**Device-based Security:**
```python
@md.POST('sensitive-action')
@md.requires_auth()
def sensitive_action(request):
    # Check if device is recognized
    if not request.device or request.device.first_seen > (datetime.now() - timedelta(hours=1)):
        # New or very recent device
        return JsonResponse({
            "error": "New device detected. Please verify your identity.",
            "requires_2fa": True
        }, status=403)
    
    # Proceed with action
    return JsonResponse({"status": True})
```

**Settings:**
```python
# How long before device location data is considered stale (seconds)
GEOLOCATION_DEVICE_LOCATION_AGE = 300  # 5 minutes
```


### Authentication Middleware

The authentication middleware processes the `Authorization` header and populates request attributes:

```python
# Request with header: Authorization: Bearer <jwt_token>
request.user              # Populated with User from JWT
request.bearer            # "bearer"
request.auth_token.token  # The actual JWT token

# Request with header: Authorization: ApiKey <key>
request.api_key           # Populated with ApiKey instance
request.bearer            # "api_key" 
request.auth_token.token  # The actual API key
```

**Custom Bearer Handlers:**
```python
# settings.py
AUTH_BEARER_HANDLERS = {
    "bearer": "mojo.apps.account.models.user.User.validate_jwt",
    "api_key": "myapp.auth.validate_api_key",
    "device": "myapp.auth.validate_device_token"
}

AUTH_BEARER_NAME_MAP = {
    "bearer": "user",
    "api_key": "api_key", 
    "device": "device"
}
```

### Using Request Data in Views

```python
@md.POST('projects')
@md.requires_auth()
@md.requires_params("name", "description")  # Validates presence
def create_project(request):
    # All data pre-parsed as objict
    project = Project.objects.create(
        name=request.DATA.name,
        description=request.DATA.description,
        owner=request.user,
        metadata=request.DATA.get("metadata", {})  # Optional with default
    )
    
    # Access nested data
    if request.DATA.settings:
        project.update_settings(request.DATA.settings.to_dict())
    
    return JsonResponse({
        "status": True,
        "project": project.to_dict(graph="default")
    })
```

---

## Graphs: Declarative Serialization

Graphs are declarative, named serialization configurations for your Django models. They tell the MOJO framework how to output your model (and any related/nested models) when returning API responses—allowing you to control exactly what fields and relationships are exposed under various API contexts.

---

## What Are Graphs?

Graphs are declarative, named serialization configurations for your Django models. They tell the MOJO framework how to output your model (and any related/nested models) when returning API responses—allowing you to control exactly what fields and relationships are exposed under various API contexts.

---

## Where Do Graphs Live?

Graphs are defined in the `RestMeta` inner class of your MOJO model:

```python
class Project(MojoModel):
    name = models.CharField(...)
    created = models.DateTimeField(...)
    owner = models.ForeignKey('User', ...)

    class RestMeta:
        GRAPHS = {
            "default": {
                "fields": ["id", "name", "created", "owner"],
                "graphs": {
                    "owner": "basic"
                }
            },
            "basic": {
                "fields": ["id", "name"]
            }
        }
```

- Each key in `GRAPHS` names a graph.
- `"fields"` — which model fields should be included when serializing under this graph.
- `"graphs"` — controls how any **related field** (FK, O2O, M2M) should itself be serialized by referencing another graph (often from the related model).

---

## Core Patterns & Advanced Nesting

### Basic Structure

- Each graph is a dictionary.
- Fields can be any model field (including relationships).
- Use the `"graphs"` key to control serialization of nested objects.

### Nested Graph Example

Suppose `Project` has an `owner` (User) field and you want nested data for the user in the API response:

```python
class User(MojoModel):
    username = models.CharField(...)
    email = models.EmailField(...)

    class RestMeta:
        GRAPHS = {
            "basic": {"fields": ["id", "username"]},
            "detailed": {"fields": ["id", "username", "email"]}
        }

class Project(MojoModel):
    owner = models.ForeignKey(User, ...)

    class RestMeta:
        GRAPHS = {
            "default": {
                "fields": ["id", "owner"],
                "graphs": {"owner": "basic"}
            }
        }
```

- `Project` with the `"default"` graph will embed a serialized `"owner"` using the `"basic"` graph of the related `User`.

### Deeper Nesting

You can nest graphs multiple layers deep for more complex domains (organizations > teams > members...).

---

## Field Options & Special Cases

- **fields:** List the fields you want returned for this graph; any relations listed should also have a graph handler in `"graphs"` for best results.
- **graphs:** Dictionary mapping field names (relation fields) to the graph name to use for serialization.
- You can use the same or different field graphs for list vs detail endpoints by naming them (`"list"`, `"default"`, `"detailed"`, etc.) and using the `graph` query param or context in API clients.

---

## URL Routing & Prefixes

### Automatic App Prefixes

MOJO automatically prefixes REST URLs with the app name. Routes are defined in `apps/<app_name>/rest/<model>.py`:

```python
# File: apps/projects/rest/project.py
from mojo import decorators as md

@md.URL('project')
def on_project(request):
    # URL becomes: /api/projects/project
    pass
```

**URL Structure:**
- Base API prefix: `/api/` (configurable via `MOJO_PREFIX`)
- App prefix: Automatically added based on app name (e.g., `projects/`)
- Route pattern: Your defined pattern (e.g., `project`)
- Final URL: `/api/projects/project`

### Absolute Paths (Bypassing App Prefix)

Use a leading `/` to create absolute paths that bypass the app prefix:

```python
# File: apps/projects/rest/project.py

@md.URL('project')
def on_project(request):
    # URL: /api/projects/project (includes app prefix)
    pass

@md.URL('/public/status')
def on_status(request):
    # URL: /api/public/status (bypasses app prefix)
    pass
```

**When to use absolute paths:**
- Shared utility endpoints (health checks, status)
- Cross-app endpoints (not tied to one app)
- Public endpoints that shouldn't show app structure
- Legacy URL compatibility

### Configuring URL Behavior

```python
# settings.py

# Base API prefix (default: "api")
MOJO_PREFIX = "api"

# Automatically add trailing slashes (default: False)
MOJO_APPEND_SLASH = False

# Let MOJO handle the prefix automatically (default: False)
# If True: Use path("", include('mojo.urls')) in Django urls.py
# If False: Use path("api/", include('mojo.urls')) in Django urls.py
REST_AUTO_PREFIX = False
```

---

## Building REST Endpoints

REST routes are defined in `apps/<app_name>/rest/<model>.py` with one file per model.

### Keep It Simple: CRUD Pattern

**Always start with the simple CRUD pattern** - one endpoint handles Create, Read, Update, Delete:

```python
from mojo import decorators as md
from mojo.apps.account.models import Group

@md.URL('group')
@md.URL('group/<int:pk>')
def on_group(request, pk=None):
    # Single endpoint handles:
    # GET /api/account/group           -> List all
    # GET /api/account/group/123       -> Get one
    # POST /api/account/group          -> Create
    # PUT /api/account/group/123       -> Update
    # DELETE /api/account/group/123    -> Delete
    return Group.on_rest_request(request, pk)
```

**Why simple CRUD?**
- Client code is simpler: `model.save()` always POSTs to same endpoint
- One URL to secure, one endpoint to test
- Standard REST conventions
- Automatic permission handling via RestMeta

### Primary Key vs Alternative Key Lookups

MOJO automatically supports lookups by both primary key (integer ID) and alternative keys (UUID, slug, etc.):

```python
class Project(MojoModel):
    # id field is automatic (integer primary key)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True)
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=255)

    class RestMeta:
        ALT_PK_FIELD = "uuid"  # Use UUID for non-numeric lookups
        GRAPHS = {"default": {"fields": ["id", "uuid", "slug", "name"]}}

# All these work with the same endpoint:
# GET /api/projects/project/123          -> Lookup by ID (numeric)
# GET /api/projects/project/550e8400-... -> Lookup by UUID (ALT_PK_FIELD)
# PUT /api/projects/project/my-project   -> Lookup by UUID if ALT_PK_FIELD set
```

**How it works:**
- **Numeric strings** (`"123"`) → Converted to int, lookup by `pk` (ID field)
- **Non-numeric strings** (`"550e8400-..."`) → Lookup by `ALT_PK_FIELD` 
- **Integers** (`123`) → Lookup by `pk` directly

**Common ALT_PK_FIELD values:**
```python
class RestMeta:
    ALT_PK_FIELD = "uuid"    # UUID field (most common)
    # ALT_PK_FIELD = "slug"  # Slug field for human-readable URLs
    # ALT_PK_FIELD = "code"  # Custom unique code field
```

**Benefits:**
- ✅ Clients can use friendly identifiers (UUIDs, slugs) instead of database IDs
- ✅ Database IDs stay hidden from public URLs
- ✅ Same endpoint works with both ID and alternative key
- ✅ No code changes needed - automatic detection

**Example with slug:**
```python
class Article(MojoModel):
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=255)

    class RestMeta:
        ALT_PK_FIELD = "slug"

# GET /api/blog/article/123                    -> By ID
# GET /api/blog/article/how-to-use-mojo-rest  -> By slug
# Both work automatically!
```

### Actions Over Custom Endpoints

**Don't create custom endpoints for model actions.** Instead, use `POST_SAVE_ACTIONS` to handle actions via POST data:

**❌ Bad: Custom endpoints for actions**
```python
# Multiple endpoints = more client code
@md.POST('project/<int:pk>/cancel')
def cancel_project(request, pk):
    project = Project.objects.get(pk=pk)
    project.cancel()
    return JsonResponse({"status": True})

@md.POST('project/<int:pk>/retry')
def retry_project(request, pk):
    project = Project.objects.get(pk=pk)
    project.retry()
    return JsonResponse({"status": True})

@md.POST('project/<int:pk>/archive')
def archive_project(request, pk):
    project = Project.objects.get(pk=pk)
    project.archive()
    return JsonResponse({"status": True})
```

**✅ Good: Use POST_SAVE_ACTIONS**
```python
# One endpoint handles everything
@md.URL('project')
@md.URL('project/<int:pk>')
def on_project(request, pk=None):
    return Project.on_rest_request(request, pk)
```

**Define actions in your model:**
```python
class Project(MojoModel):
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=50)

    class RestMeta:
        POST_SAVE_ACTIONS = ["cancel", "retry", "archive", "assign"]
        GRAPHS = {"default": {"fields": ["id", "name", "status"]}}

    def on_action_cancel(self, value):
        """Called when POST /api/projects/project/123 {"cancel": true}"""
        if value:  # Only if truthy
            self.status = "cancelled"
            self.save()
            # Send notifications, update related records, etc.

    def on_action_retry(self, value):
        """Called when POST /api/projects/project/123 {"retry": true}"""
        if value:
            self.status = "pending"
            self.retry_count += 1
            self.save()

    def on_action_archive(self, value):
        """Called when POST /api/projects/project/123 {"archive": true}"""
        if value:
            self.archived = True
            self.save()

    def on_action_assign(self, user_id):
        """Called when POST /api/projects/project/123 {"assign": 42}"""
        if user_id:
            self.assigned_to_id = user_id
            self.save()
```

**Client usage is simple:**
```javascript
// All actions use the same endpoint
await project.save({id: 123, cancel: true})
await project.save({id: 123, retry: true})
await project.save({id: 123, assign: 42})

// No need to track multiple URLs or methods
// Just POST data to the model's endpoint
```

**Benefits of POST_SAVE_ACTIONS:**
- ✅ Client has one URL: `projects/project/123`
- ✅ Standard POST method for all actions
- ✅ Actions are discoverable in model code
- ✅ Same permission checking as save
- ✅ Actions run in same transaction as save
- ✅ Can combine actions: `{archive: true, notify_users: true}`
- ✅ Simpler client code: `model.save(data)`

### When to Create Custom Endpoints

Only create custom endpoints when:

1. **Not tied to a specific model instance** (collection operations)
2. **Complex queries or reports** (not simple CRUD)
3. **External integrations** (webhooks, callbacks)
4. **Public APIs** (no authentication)

```python
# Collection operation - OK for custom endpoint
@md.POST('project/bulk_archive')
@md.requires_perms("manage_projects")
def bulk_archive(request):
    project_ids = request.DATA.ids
    Project.objects.filter(id__in=project_ids).update(archived=True)
    return JsonResponse({"status": True, "count": len(project_ids)})

# Complex report - OK for custom endpoint
@md.GET('project/stats')
def project_stats(request):
    stats = Project.objects.aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(status='active')),
        cancelled=Count('id', filter=Q(status='cancelled'))
    )
    return JsonResponse({"status": True, "stats": stats})

# Public webhook - OK for custom endpoint
@md.POST('/webhook/github')  # Absolute path for public endpoint
def github_webhook(request):
    # Process GitHub webhook
    return JsonResponse({"status": True})
```

### Simple Custom Endpoints

For endpoints that don't need CRUD:

```python
@md.GET('group/hello')
@md.requires_perms("manage_groups")
def on_group_hello(request):
    return JsonResponse({"status": True, "data": "Hello World"})

@md.POST('group/echo')
@md.requires_auth()
@md.requires_params("echo")  # Validates presence of parameters
def on_group_echo(request):
    return JsonResponse({"status": True, "data": request.DATA.echo})
```

---

## POST Save Actions

POST_SAVE_ACTIONS

```python
class Project(MojoModel):
    owner = models.ForeignKey(User, ...)

    class RestMeta:
        POST_SAVE_ACTIONS = ["cancel_request", "retry_request"]
        GRAPHS = {
            "default": {
                "fields": ["id", "owner"],
                "graphs": {"owner": "basic"}
            }
        }

    def on_action_cancel_request(self, value):
        pass

    def on_action_retry_request(self, value):
        pass
```

Usage would be you would POST to the endpoint with the action name in the data.

```
POST /api/projects/2 {"cancel_request": true}
```

## Best Practices

- **Keep It Minimal:** Only include fields and relationships that are really needed in each graph context.
- **Avoid Circular Relations:** Don't nest parent-child graphs so deeply you risk recursion/infinity.
- **Use a "basic" or "list" graph:** For lists and lightweight API calls, define a minimal graph without costly relationships or sensitive fields.
- **Use "default" for detail responses:** Include richer, but still controlled, information.
- **Customize via `"graphs"` as needed:** Always declare how nested FKs/O2Os should be serialized for control and safety.
- **DRY Patterns:** Reuse graph names ("basic", "default") across your models—a consistent API helps everyone.
- **Advanced: Compose or generate graphs dynamically (by override or using helper mixins) if you have meta or API-layer needs.**

---

## Extension Patterns

- **Graph Inheritance:** If you have a base model with common fields via an abstract model, you can reference its graph definitions directly, or build up graphs with shared fragments via Python.
- **Overrides:** You can override the `to_dict` or `GraphSerializer` for custom serialization logic, if the graph system’s declarative style isn’t enough for some edge case.
- **Dynamic Graphs:** For extremely dynamic scenarios (per-user, runtime-driven), you can programmatically select or extend graphs in your model/endpoint code before returning the API result, though this should be used sparingly.

---

## Debugging and Evolving Graphs

- **Testing:** Always test your API outputs for every graph you expose—pay special attention to nested serializations and sensitive data exposure.
- **Versioning:** If you need backwards compatibility, use new graph names for new versions and softly deprecate old ones.
- **Audit:** Review graphs periodically to ensure no private fields have slipped into too-public graphs.

---

## References

- See the REST user guide for details on how to select different graphs at request time (`docs/rest_api/using_graphs.md`).
- Core serialization logic lives in `mojo/serializers/simple.py`.

---

## FAQ

**Q: What if a field is missing from "fields" but present in "graphs"?**
- Only fields in `"fields"` are returned; `"graphs"` only influences *how* related objects will be serialized if their field is also listed.

**Q: Can graphs be selected dynamically by API users?**
- Yes! Typically via the `graph` query param in REST requests (`?graph=list`, `?graph=detailed`, etc.).

**Q: What if a related model doesn’t have a graph with the needed name?**
- You’ll see an error or raw PK output—always define your graphs for every related model.

---

**Questions or best-practice tips? Improve this doc and help the community!**
