# Django-MOJO Developer Guide

Welcome to Django-MOJO! This guide provides a concise overview of conventions, project patterns, and the essential steps to get started developing and contributing to Django-MOJO.

---

## Initial Setup & Configuration

Before you can use MOJO in your Django project:

1. **Add MOJO to Django Settings:**  
    Add `'mojo.base'` to your `INSTALLED_APPS` list in your Django `settings.py` file:
    ```python
    INSTALLED_APPS = [
        ...
        'mojo.base',
    ]
    ```
2. **Migrate the Database:**  
    Apply Django migrations to add MOJO’s models and tables:
    ```bash
    python manage.py migrate
    ```
3. **Configure MOJO Settings:**  
    Add or update MOJO settings in `settings.py` as needed. Common options:
    ```python
    MOJO_API_MODULE = "api"
    MOJO_APPEND_SLASH = True  # or False for your preferred routing style
    ```

---

---

## Guiding Principles

- **Simplicity:** Prioritize clear, explicit code. Avoid unnecessary abstractions or “magic.”
- **Separation of Concerns:** Organize logic into small, maintainable files. Each model, REST handler, and utility lives in its own file.
- **Extensibility:** Adding new features (models, endpoints, helpers) should require minimal setup—no central file editing or complex code generation.
- **Secure by Default:** Adopt best practices for permission enforcement using MOJO’s object-level permissions.

---

## Key Patterns and Conventions

### Models

- Place each Django model in its own file in the relevant app’s `models/` folder.
- Inherit from `MojoModel` (or extensions like `MojoSecrets` if needed).
- Define an inner `RestMeta` class to configure:
  - Permissions (`VIEW_PERMS`, `SAVE_PERMS`, etc.)
  - Default list filters
  - Serialization graphs for API output

**Example:**
```python
from django.db import models
from mojo.models import MojoModel

class Project(MojoModel):
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class RestMeta:
        VIEW_PERMS = ["view_project"]
        SAVE_PERMS = ["manage_project"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "default": {
                "fields": ["id", "name", "is_active"],
            }
        }
```

### REST Endpoints

- Add REST views to each app’s `rest/` directory.
- Use decorators from `mojo.decorators` (usually imported as `md`) to register endpoints.
- The main route delegates to your model’s `on_rest_request()` for automatic CRUD logic.
- Use concise catch-all patterns; add special routes only when needed.

#### Common Decorator Usage Patterns

- **Register a view for any HTTP method:**  
  `@md.URL('pattern')`

- **Register method-specific routes:**  
  `@md.GET('pattern')`  
  `@md.POST('pattern')`  
  `@md.PUT('pattern')`  
  `@md.DELETE('pattern')`

- **Require request parameters:**  
  `@md.requires_params('username', 'password')` ensures those are present for the view.

- **Enforce permissions/auth:**  
  `@md.requires_auth` or `@md.requires_perms('admin', 'edit')` guards the endpoint.

**Example:**
```python
from mojo import decorators as md
from .models.project import Project

@md.URL('project')
@md.URL('project/<int:pk>')
def on_project(request, pk=None):
    return Project.on_rest_request(request, pk)

@md.GET('hello/')
def hello_view(request):
    return JsonResponse({"message": "Hello, World!"})

@md.requires_auth
@md.GET('profile/')
def profile_view(request):
    return JsonResponse({"profile": ...})

@md.requires_params('email')
@md.POST('invite/')
def invite(request):
    # Invite logic
    ...
```

### Utilities & Helpers

- **Reuse helpers from `mojo/helpers/`** (logging, cron, request handling, crypto, etc.).
- New utility logic? Add or extend helpers—don’t copy-paste code.

---

## Quickstart Workflow

1. **Create a Model:**
   - Place in `apps/<your_app>/models/<model_name>.py`.
   - Inherit from `MojoModel`, add a `RestMeta` class.

2. **Create REST Handlers:**
   - Place in `apps/<your_app>/rest/<model_name>.py`.
   - Register routes using `@md.URL` decorators.

3. **Write or Extend Helpers:**
   - For repeated logic, contribute or use existing modules in `mojo/helpers/`.

4. **Testing:**
   - Use the `testit` suite and REST client for tests.
   - Place your tests following the existing pattern; see [`docs/testit.md`](testit.md).

5. **Add Documentation:**
   - Reference or update documentation in `docs/` for new features.
   - For high-level changes, update the `README.md`.

---

## Permissions System: User vs. Group (and the `sys.` Prefix)

MOJO supports both system-level (global, user-based) permissions and group-level (per-group member) permissions. To avoid ambiguity when the same permission key might exist at both levels, MOJO uses a simple namespacing rule:

- **System-level permissions**: Prefix the permission key with `sys.` (e.g. `"sys.admin"`)
    - This forces the check to only the User's global permissions.
    - Ignored at the group member level.
- **Group/member-level permissions**: Use the key without a prefix (e.g. `"manage_group"`).
    - This checks first for direct user's permissions, then the specific group member's permissions.

**Examples:**
```python
# Check if user has system-wide admin permission
member.has_permission("sys.admin")  # Only checks member.user.has_permission("admin")

# Check if member can manage this group
member.has_permission("manage_group")  # Checks both the user and the group member's own permissions

# List support (OR logic)
member.has_permission(["sys.superuser", "manage_group"])
```

This pattern guarantees clear, auditable separation of tenant-specific (group) permissions vs. true system-wide access and supports advanced security requirements out of the box.

---

## Coding Style Recommendations

- **Keep It Short:** Short files and short functions.
- **Be Explicit:** No hidden glue code or dynamic imports—everything is where you’d expect.
- **Consistent Imports:** Prefer `from mojo import decorators as md` and explicit relative imports in submodules.
- **No `__init__.py` Magic:** Don’t rely on `__init__.py` for registration or auto-discovery.

---

## Useful References

- **High-level overview:** [`README.md`](../README.md)
- **REST Pattern & Graphs:** [`rest_developer.md`](rest_developer.md)
- **Testing framework:** [`testit.md`](testit.md)
- **Decorators & Helpers:** [`decorators.md`](decorators.md), [`helpers.md`](helpers.md)

---

## Contributing

- Prefer pull requests for features and bugfixes.
- Document public-facing changes.
- Write minimal and meaningful tests.

For detailed area-specific documentation, see the `docs/` folder.

---

**Happy coding with Django-MOJO!**