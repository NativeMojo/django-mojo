---
globs: tests/**/*.py
---

# Testing Conventions

Before writing any test, read `docs/django_developer/testit/Overview.md`. This is mandatory.

## Framework
- Use testit: `from testit import helpers as th`
- Decorator: `@th.django_unit_test()`
- Function signature: `def test_xxx(opts):`
- Tests go in `tests/` directory (NOT inside the package)
- Import the module under test inside the test function

## Server Isolation
- `opts.client` calls a **separate server process** — `mock.patch` and `override_settings` have NO effect on the server
- Use `th.server_settings(**overrides)` for Django settings overrides (writes to var/django.conf, reloads server)
- Never use `override_settings` in testit tests

## Rules
- Every `assert` must include a descriptive failure message — no bare asserts
- Tests must pass when the feature is correct and fail when it is broken
- Never write tests that assert the feature is absent or broken
- Setup functions must clean up test data before creating it — tests run on long-lived databases, not just fresh ones. Delete any records your setup will create before inserting them.
- Run with `bin/run_tests -t test_module.filename` — do not ask the user to run them
- If a test fails, fix the **code** (not the test) unless the test itself is wrong
- Never write "bug confirmation" tests that pass by asserting the bug occurs
- Regressions must fail while broken, pass only when fixed
