# Feature: PyPI Package Rename — django-nativemojo → django-mojo

**Type**: feature / release engineering
**Status**: Resolved
**Date**: 2026-03-21
**Resolved**: 2026-03-21

## Resolution

All acceptance criteria met. Published and live.

## What Was Done

### pyproject.toml
- Renamed `name` from `django-nativemojo` to `django-mojo`
- Updated description and author email

### shim/
- Created `shim/pyproject.toml` — `django-nativemojo` compatibility package using poetry-core
- Dependency: `django-mojo>=1.0.60` (range, not exact pin — shim never needs republishing)
- Created `shim/django_nativemojo/__init__.py` — emits `DeprecationWarning` on import
- Created `shim/README.md` — PyPI page explains the deprecation and migration path
- Published to PyPI manually: `cd shim && poetry build && poetry publish`

### CI
- Created `.github/workflows/publish.yml` — triggers on `v*` tags, publishes both packages using `PYPI_API_TOKEN` secret
- Shim excluded from routine release workflow (no need to republish on each release)

### README.md
- Full rewrite — dropped "lightweight" framing
- New positioning: "full-stack Django framework for teams that want to ship, not assemble"
- Added comparison table, code examples for REST/auth/settings/jobs/realtime
- Added migration section for `django-nativemojo` users

## Files Changed

- `pyproject.toml` — renamed to django-mojo
- `shim/pyproject.toml` — new
- `shim/README.md` — new
- `shim/django_nativemojo/__init__.py` — new
- `.github/workflows/publish.yml` — new
- `README.md` — full rewrite

## Follow-up (Phase 2+)

- Update internal Dockerfiles and requirements files to `pip install django-mojo`
- Monitor PyPI download stats on both packages to track migration progress
- Freeze `django-nativemojo` releases once old usage is drained
