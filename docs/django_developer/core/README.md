# Core Framework — Django Developer Reference

This section covers the foundational components of django-mojo:

- [MojoModel & REST Framework](mojo_model.md) — Base model class, CRUD lifecycle, RestMeta configuration
- [Decorators](decorators.md) — URL routing, auth, validation decorators
- [Rate Limiting & Endpoint Metrics](rate_limiting.md) — Fixed-window, sliding-window, and usage tracking decorators
- [Middleware](middleware.md) — Request parsing, authentication, CORS middleware
- [Serialization & Graphs](graphs.md) — GRAPHS system, serialization, response format
- [REST Permissions](../rest/permissions.md) — VIEW_PERMS, SAVE_PERMS, OWNER_FIELD, CAN_DELETE, owner/group scoping
- [Django Cache Backend](cache.md) — Mojo Redis-backed Django cache (`mojo.cache.MojoRedisCache`)
