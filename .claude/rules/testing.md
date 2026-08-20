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

## Running
- Run with `bin/run_tests --agent -t test_module.filename` — do not ask the user to run them
- Always use `--agent` flag — read `testproject/var/test_failures.json` for diagnostics, never parse terminal output
- The work item's verification tier decides what runs; see `.claude/rules/build-baseline.md`
  for the tiers, picking the modules for a `targeted` run, and what counts as `full` here
- Use `--all` to include opt-in modules (`requires_extra` — `slow` and `extended`) — needed
  for pre-publish validation, or when you changed what `--all` selects
- Never use `--plain` — it disables the rich progress UI and parallel execution

## Tiers and Isolation
- The default tier is only for critical core contracts: security boundaries, shared
  framework behavior, and regressions whose failure means django-mojo is broken for
  consumers. Exhaustive variants and feature-internal coverage belong in `extended`;
  expensive pre-release coverage belongs in `slow`.
- Every test must pass by itself and under the default parallel runner. Tests may not
  depend on module order or leak state into another test.
- Default-tier tests must not patch or mutate process-wide configuration such as the
  shared `SettingsHelper`, `django.conf.settings`, environment variables, key material,
  or live config files. Exercise the core contract with test-owned data, dependency
  injection, or a local fake instead.
- If process-wide mutation is genuinely unavoidable, put that coverage in an opt-in
  module and mark the module `serial`. An extra tag alone does not provide isolation:
  `--all` still runs eligible modules in parallel.

## Rules
- Every `assert` must include a descriptive failure message — no bare asserts
- Tests must pass when the feature is correct and fail when it is broken
- Never write tests that assert the feature is absent or broken
- Setup functions must clean up test data before creating it — tests run on long-lived databases, not just fresh ones. Delete any records your setup will create before inserting them.
- If a test fails, fix the **code** (not the test) unless the test itself is wrong
- Never write "bug confirmation" tests that pass by asserting the bug occurs
- Regressions must fail while broken, pass only when fixed
