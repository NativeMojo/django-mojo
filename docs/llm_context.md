# Django-MOJO: LLM Compact Context

This file distills the core philosophy, structure, and conventions necessary for any LLM or contributor to produce code, docs, or refactors that fit perfectly in the Django-MOJO framework.

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
- Inherit from `MojoModel` (or extensions) and live in `app/models`.
- Define an inner `RestMeta` class with:
  - `VIEW_PERMS`, `SAVE_PERMS`, etc.
  - `GRAPHS` for all response graph shapes (see below for graphs deep dive).

**REST Handlers**
- Place in `app/rest`.
- Use decorators (`@md.URL`, `@md.GET`, etc.) to register endpoints.
- Standard handlers point to `Model.on_rest_request(request, pk)` for automatic CRUD logic.
- Only add custom handlers when necessary (nested, complex, special validation).

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
- All repeated logic resides in `mojo/helpers`—always use or extend these before new standalone functions.

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

## Security

- All changes must maintain "fail closed" access unless explicitly documented.
- Owner and group checks are never bypassed by mistake due to clear permission evaluation order.
- Auditing and denied events are auto-reported for compliance.

---

## LLM/Contributor Checklist

- Double-check if the area touches backend vs REST user patterns; keep doc/code modular.
- Never change permission or REST flow without cross-referencing/updating both dev and API docs.
- Keep code/config DRY and idiomatic—prefer composition and helper reuse over copy-paste.

---

_Reference this file before making any LLM- or contributor-initiated changes to code or documentation._