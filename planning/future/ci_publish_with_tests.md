# Future: CI Publish Workflow with Test Gate

**Type**: feature / release engineering
**Status**: Future
**Date**: 2026-03-21

## Summary

Add a GitHub Actions workflow that runs the full test suite before publishing to PyPI.
Only publish if all tests pass. Triggered by `v*` tags (same as `publish.py` creates).

## Workflow Design

1. Trigger on `v*` tag push
2. Spin up test environment: PostgreSQL + Redis
3. Run `./bin/create_testproject` + `./bin/testit.py`
4. If tests pass → `poetry build && poetry publish` (django-mojo)
5. If tests pass → `cd shim && poetry build && poetry publish` (django-nativemojo shim)
6. If tests fail → abort, notify

## Notes

- Requires `PYPI_API_TOKEN` secret in GitHub repo settings
- PostgreSQL and Redis services need to be configured as GitHub Actions services
- `create_testproject` script handles DB setup — needs to work headlessly in CI (it already does)
- Shim publish is a no-op after the first release since it uses `>=` version constraint — can be skipped or made conditional
