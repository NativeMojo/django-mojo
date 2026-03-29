---
globs: mojo/**/rest/**/*.py,mojo/**/rest/*.py
---

# REST Conventions

- Use `request.DATA` only for request inputs.
- Prefer simple CRUD handlers:
  ```python
  @md.URL('resource')
  @md.URL('resource/<int:pk>')
  @md.uses_model_security(MyModel)
  def on_resource(request, pk=None):
      return MyModel.on_rest_request(request, pk)
  ```
- No trailing slash on list endpoints.
- URL prefix is the app directory name. Dynamic segments go at the end only — never `resource/<int:pk>/children`.
- For per-instance operations: use `POST_SAVE_ACTIONS` + `on_action_<name>` instead of custom endpoints.
- RestMeta endpoints need `@md.uses_model_security(Model)` — without it the URL returns 404.
- Do NOT add `@md.requires_auth()` on RestMeta endpoints — `VIEW_PERMS` handles authentication.
- Non-RestMeta endpoints use `@md.requires_perms(...)` — include the domain category permission alongside fine-grained perms.
- View functions return plain dicts — the framework converts them to JSON responses. Use `JsonResponse` directly only when you need explicit control (e.g., custom status codes, headers).
