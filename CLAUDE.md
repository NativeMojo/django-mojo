# Django-MOJO: LLM Compact Context

This file distills the core philosophy, structure, and conventions necessary for any LLM or contributor to produce code, docs, or refactors that fit perfectly in the Django-MOJO framework.

MOST IMPORTANT: FOLLOW -> KEEP IT SIMPLE STUPID (KISS)

---

## Project Philosophy

- **Explicit Simplicity:** Prefer direct, readable code—never "magic," excessive abstraction, or import/registration trickery.
- **Separation of Concerns:** Each model, REST handler, helper, and test belongs in its own file. No complex `__init__.py` patterns.
- **Security by Default:** All endpoints enforce strong object-level permissions, favoring a "fail closed" model. Permissions are handled via each model's `RestMeta` config, with clear separation between user (system), group, and owner.
- **DRY & Discoverable Docs:** Documentation is split for:
  - **Django devs:** core guides, setup, RestMeta/graphs, helpers, and permissions (in `docs/`).
  - **REST users:** authentication, filtering, listing, error handling, graph usage, and examples (in `docs/rest_api/`).

---

## Core MOJO Conventions

**Models**
- **Regular Models:** Inherit from `models.Model, MojoModel` (correct order: `models.Model, MojoModel`).
- **Models with Secrets:** Inherit from `MojoSecrets, MojoModel` (DO NOT include `models.Model` - MojoSecrets already provides it).
- Always include standard fields: `created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)` and `modified = models.DateTimeField(auto_now=True, db_index=True)`.
- Models live in `app/models` with one model per file.
- For related models, organize them in subdirectories (e.g., `models/push/`) with short filenames (`device.py`, `config.py`, `template.py`).
- Use `__init__.py` in subdirectories to export all models for clean imports.
- Define an inner `RestMeta` class with:
  - `VIEW_PERMS`, `SAVE_PERMS`, etc.
  - `GRAPHS` for all response graph shapes (see below for graphs deep dive).
- Adding fields: user="account.User" and/or group="account.Group" to Models when appropriate allows the framework to provide security and permissions to the model so users or groups can access the model. It should only be done when we think this model will require user and/or group level access controls.

- When building rest endpoints, keep it or simple CRUD convention (this handles everything we need and keeps it consistent):
```
import mojo.decorators as md

@md.URL('book')
@md.URL('book/<int:pk>')
def on_book(request, pk=None):
    return Book.on_rest_request(request, pk)
```

**Services**
- Domain-specific business logic belongs in `app/services/` (not `mojo/helpers/`).
- Keep service filenames short (e.g., `push.py` not `push_notifications.py`).
- Services handle complex business logic that doesn't belong in models or REST handlers.

**MojoSecrets Usage**
- MojoSecrets stores all sensitive data in a single encrypted JSON field (`mojo_secrets`).
- Use `set_secret(key, value)` and `get_secret(key, default)` methods.
- Never create individual encrypted model fields - always use the secrets system.

**Logging**
- Use `import logit` and call `logit.info()`, `logit.error()`, `logit.warn()`, `logit.debug()`.
- Automatic routing: `info`/`warn` → `mojo.log`, `error` → `error.log`, `debug` → `debug.log`.
- Always use module prefix (`logit.error`) for clarity and readability.

**REST Handlers**
- Place in `app/rest` with short, descriptive filenames.
- Use decorators (`@md.URL`, `@md.GET`, etc.) to register endpoints.
- Prefer data in params/POST body, not URL paths: `@md.POST('devices/push/send')` not `@md.POST('devices/push/send/<param>')`.
- **Data Access**: Always use `request.DATA` for unified access to both POST data and GET params - never use `request.POST.get()` or `request.GET.get()` directly.
- **URL Patterns**: List endpoints should NOT end with trailing slashes (e.g., `/api/account/devices/push` not `/api/account/devices/push/`).
- **CRITICAL — URL auto-prefix is the app directory name**: `@md.URL('session')` in app `wallet` → `/api/wallet/session`. NEVER use a parent package name as the prefix.
- **CRITICAL — Dynamic URL segments go at the END only**: `book/<int:pk>` is correct; `book/<int:pk>/chapters` is NEVER correct. Use query params or POST data for nested lookups.
- **CRITICAL — Use POST_SAVE_ACTIONS for model operations**: When an action targets a specific instance (test, clone, approve, send), use `on_action_<name>` instead of a dedicated REST endpoint.
- Standard handlers point to `Model.on_rest_request(request, pk)` for automatic CRUD logic.
- Only add custom handlers when necessary (nested, complex, special validation).
- Support both templated and direct content patterns where applicable (e.g., notifications can use templates OR direct title/body).

**Graphs (Serialization System)**
- Controlled per-model in `RestMeta.GRAPHS`.
- Each graph defines a named field set and, optionally, nested graphs for related/foreign-key fields.
- Developers: see `docs/rest_developer.md` for backend graph config.
- REST users: select a graph with the `graph` parameter (see `docs/rest_api/using_graphs.md`).

**Permission Flow (REST Impact)**
- MOJO auto-populates `request.group` if a group param is in the HTTP request.
- Permission checks always proceed in this order:
  1. **Unauthenticated:** Denied unless `all` perm present.
  2. **Instance Owner:** If `"owner"` in perms and instance.user matches request user, always allowed.
  3. **Group:** If `request.group` is set and model has a `group` field, only group-member access applies.
  4. **System/User:** Otherwise, use user-level perms.
- **Result:** If user lacks all valid perms, API returns 403 or an empty list—data is never exposed by default.

**Decorators (Backend Patterns):**
- Route: `@md.URL`/`@md.GET`/`@md.POST`/etc.
- Validation: `@md.requires_params`
- Auth/Perm: `@md.requires_auth`, `@md.requires_perms`

**Helpers & Utilities**
- General utilities in `mojo/helpers`, domain-specific logic in `app/services/`.
- Always use `from mojo.helpers.settings import settings`, then `settings.get("MY_SETTING", default_value)`.
- Use `from mojo.helpers import logit`, then `logit.info()`, `logit.error()`, etc.

**Testing**
- Use the inbuilt `testit` suite for both REST and backend unit tests.

---

## Documentation Structure

- **All framework and backend docs in** `docs/`
- **All API user/integration docs in** `docs/rest_api/`
- Never duplicate—always reference relevant files when documenting or implementing features.

---

## Workflow Example

1. Create a new model in `app/models/my_model.py` (`MojoModel` + `RestMeta`).
2. Add REST endpoint(s) in `app/rest/my_model.py` with `@md.URL` decorators.
3. Configure your `GRAPHS` in `RestMeta` for desired API shapes/results.
4. Rely on the automatic group/user/owner permission system.
5. Document only *new* patterns or idioms—otherwise point to existing guides.

---

## Implementation Best Practices

**Model Design**
- Keep business logic separate from data access - use services for complex operations.
- Design for both individual and bulk operations from the start.
- Support flexible configuration (system-wide and organization-specific).

**API Design**
- Design APIs to support both templated and direct content patterns.
- Always provide complete audit trails for sensitive operations.
- Use proper HTTP methods and status codes consistently.

**File Organization**
- One model per file, short descriptive names.
- Group related models in subdirectories (e.g., `models/push/device.py`, `models/push/config.py`).
- Use `__init__.py` files to maintain clean imports from subdirectories.
- Keep imports simple and explicit.

**Security & Permissions**
- Never expose sensitive fields (like `mojo_secrets`) in API responses.
- Use `exclude` in RestMeta GRAPHS to prevent credential exposure.
- Test permission enforcement thoroughly in all scenarios.

---

## Security

- All changes must maintain "fail closed" access unless explicitly documented.
- Owner and group checks are never bypassed by mistake due to clear permission evaluation order.
- Auditing and denied events are auto-reported for compliance.

---

## LLM/Contributor Checklist

**Before Implementation:**
- Check model inheritance order:
  - Regular models: `models.Model, MojoModel`
  - Models with secrets: `MojoSecrets, MojoModel` (DO NOT include `models.Model`)
- Include standard `created`/`modified` fields with proper indexing.
- Use MojoSecrets correctly (single JSON field, not individual encrypted fields).
- Place domain logic in `app/services/`, not `mojo/helpers/`.
- 'account.User' -> 'user' and 'account.Group' -> 'group' models should be used on most models so the framework can perform permission checks.

**During Development:**
- Use `logit.info()`, `logit.error()`, etc. with module prefix for clarity.
- Keep filenames short and descriptive.
- Put data in params/POST body, not URL paths.
- Always use `request.DATA` for data access, never `request.POST.get()` or `request.GET.get()`.
- List endpoints should not end with trailing slashes.
- Support both templated and direct patterns where applicable.

**Security & Testing:**
- Test permission enforcement thoroughly.
- Ensure "fail closed" behavior is maintained.

**Documentation:**
- Double-check if the area touches backend vs REST user patterns; keep doc/code modular.
- Never change permission or REST flow without cross-referencing/updating both dev and API docs.
- Keep code/config DRY and idiomatic—prefer composition and helper reuse over copy-paste.

---

_Reference this file before making any LLM- or contributor-initiated changes to code or documentation._

IMPORTANT:
 - this is a django framework not a django project, ask user to run any django project related commmands
 - Never create migration files, ask user to.
 - Never use Python type hints anywhere.
 - Don't make assumptions, ask the user for clarification.

MOST IMPORTANT: FOLLOW -> KEEP IT SIMPLE STUPID (KISS)
