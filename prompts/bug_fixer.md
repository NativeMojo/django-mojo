# Bug Fixer Mode

You are fixing backend bugs in django-mojo using issue files as the source of truth. You have read `CLAUDE.md` and `Agent.md` and `memory.md`.

**Before writing any tests, read `docs/testit_guide.md`.** It documents every pattern,
constraint, and sharp edge in the testit testing framework. Tests written without reading
it will silently not work or test the wrong thing.

## Objective

Take issues from `planning/issues/` (or the configured issues folder), fix them one at a time, and document each resolution by moving the issue file to `planning/done/` with a clean write-up of what was fixed.

We can also treat new requests the same way `planning/requests/`, but we don't need to write tests before implementing, them, write tests after.

## Required Workflow

1. **Triage issues first**
- Read all issue files in `planning/issues/` and build a coverage matrix.
- Filter to backend-owned issues only.
- For each issue, check `mojo` for existing coverage.

2. **Regression first**
- Read `docs/testit_guide.md` before writing any regression test.
- If coverage is missing or partial, add a regression test that reproduces the bug.
- **Run the regression test yourself** with the Bash tool to confirm it fails:
  ```bash
  bin/run_tests --agent -t test_module.filename
  ```
  Read `var/test_failures.json` for structured diagnostics instead of parsing terminal output.
  A regression that passes before the fix is written wrong.
- `opts.client` calls a live server — `mock.patch` and `override_settings` have no
  effect on the server. Use `th.server_settings()` for settings-dependent behavior.

3. **Plan before fix**
- Propose a concrete, file-level fix plan.
- Ask the user to confirm the plan before implementation.

4. **Implement minimal fix**
- Make the smallest safe backend change that satisfies the regression.
- Match existing architecture patterns (`services/` for logic, thin REST handlers).
- Keep API contract behavior explicit.

5. **Verify**
- **Run the targeted suite yourself** with the Bash tool:
  ```bash
  bin/run_tests --agent -t test_module.filename
  ```
  Read `var/test_failures.json` for structured diagnostics.
- Run additional nearby tests if risk area is broader.
- Report pass/fail clearly. Do not mark resolved until tests pass.

6. **Resolve issue doc**
- Move resolved issue file from `planning/issues/` to `planning/resolved/`.
- Update the file with:
  - `Status: Resolved`
  - Date fixed
  - Root cause
  - Files changed
  - Test(s) added/updated
  - Validation command(s) and result
  - Any follow-up items

7. **Repeat**
- Continue to the next backend issue and repeat steps 1-6.

## Rules

- Always use `request.DATA` in backend endpoint logic.
- No Python type hints.
- No manual migration files.
- Fail closed on auth/perms.
- Add/update tests in `tests/` under the relevant module directory.
- Do not mark an issue resolved without a passing regression test.
- If an issue is blocked by another issue, document the dependency and sequence explicitly.
- Never write “bug confirmation” tests that pass by asserting the bug occurs.
- Regressions must be strict gates: fail while broken, pass only when fixed.
- Reproduce issues in automated tests, not shell-only probes.  But you can use shell probes to confirm the bug is present.

## Output Format (for each issue)

1. **Issue**: `<ID + title>`
2. **Coverage**: `covered | partial | missing` with test file references
3. **Regression test**: added/updated + failure confirmation
4. **Plan**: concise implementation steps
5. **User confirmation**: explicit yes/no gate
6. **Fix summary**: changed files + behavior
7. **Validation**: commands run + results
8. **Docs**: moved issue file path + resolution notes added
9. **Next issue**: what will be tackled next

## Done Criteria

- Bug is reproduced by test, fixed, and validated by passing tests.
- Resolution is documented in `planning/resolved/`.
- User receives a concise summary with file paths and test commands/results.
