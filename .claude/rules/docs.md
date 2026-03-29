# Documentation Rules

Keep both doc tracks in sync when behavior changes:

- `docs/django_developer/*` — for backend/framework developers building with django-mojo (models, Python API, configuration, architecture)
- `docs/web_developer/*` — for frontend/REST developers consuming the API (endpoints, request/response format, permissions needed)

A new endpoint or behavior change typically needs docs in **both** places.

## When to Update
- New or changed REST endpoints
- New or changed model fields, permissions, or graphs
- New or changed configuration/settings
- New framework features or helpers

## What to Update
- Update root indexes (`README.md` in each doc folder) when adding new doc files
- Update `CHANGELOG.md` for meaningful behavior or API changes
- Read `docs/django_developer/README.md` before building — do not reinvent existing features
