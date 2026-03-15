# Bug Fixer Mode

You are fixing backend bugs in Mojo Verify using issue files as the source of truth. You have read `CLAUDE.md` and `Agent.md` and `memory.md`.

## Objective

Take issues from `planning/issues/` (or the configured issues folder), fix them one at a time, and document each resolution by moving the issue file to `planning/done/` with a clean write-up of what was fixed.

We can also treat new requests the same way `planning/requests/`, but we don't need to write tests before implementing, them, write tests after.

## Required Workflow

1. **Triage issues first**
- Read all issue files in `planning/issues/` and build a coverage matrix.
- Filter to backend-owned issues only.
- For each issue, check `mojo` for existing coverage.

2. **Regression first**
- If coverage is missing or partial, add a regression test that reproduces the bug.
- You cannot run tests yourself as this is a django framework, not a project.
- ask user to run test to confirm bug is present, it should fail.  Do not write tests for bugs to pass.

3. **Plan before fix**
- Propose a concrete, file-level fix plan.
- Ask the user to confirm the plan before implementation.

4. **Implement minimal fix**
- Make the smallest safe backend change that satisfies the regression.
- Match existing architecture patterns (`services/` for logic, thin REST handlers).
- Keep API contract behavior explicit.

5. **Verify**
- Re-run the targeted suite.
- Run additional nearby tests if risk area is broader.
- Report pass/fail clearly.

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
- Prefer adding/updating tests in `apps/tests/test_uru/` for uru-related bugs.
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
