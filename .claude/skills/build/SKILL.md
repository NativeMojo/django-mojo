---
name: build
description: Implement a planned issue or request — code, tests, commit, then spawn test/docs/security agents
user-invocable: true
argument-hint: <path to planned file>
---

Takes a planned issue or request file and implements it end-to-end: code, tests, commit, then spawns agents for full test suite, docs, and security review.

## Arguments

$ARGUMENTS — Path to a planned file (e.g., `planning/requests/webhook-retry.md`). Must have a `## Plan` section. If empty, list planned files and ask which one to build.

## Workflow

### 1. Read the Planned File
Read the file at $ARGUMENTS. It must have `Status: planned` and a `## Plan` section. If not, stop and tell the user to run `/plan` first.

### 2. Read All Files in Scope
- Read every file listed in the plan's Steps section
- Read `docs/django_developer/testit/Overview.md` (testit guide)
- Read `docs/django_developer/README.md`
- No blind edits

### 3. Confirm Before Building
Briefly summarize what you are about to build. Ask the user for a quick yes/no before writing code. If the plan is large, break it into phases and confirm each.

### 4. Implement
Make the changes described in the plan. The rules files (`.claude/rules/`) are loaded automatically — follow them.

### 5. Write and Run Tests
- Write testit tests covering the scenarios in the plan
- Run with `bin/run_tests -t <target>`
- Fix any failures in your code (not in the tests)
- Do not proceed until targeted tests pass

### 6. Git Commit (No Push)
- Stage specific files by name (never `git add -A` or `git add .`)
- Write a descriptive commit message summarizing what was built and why
- Do NOT push to remote

### 7. Spawn Post-Build Agents
After committing, spawn all three agents in parallel:

1. **test-runner** — Run the full test suite to catch regressions beyond your targeted tests
2. **docs-updater** — Read the git diff and update `docs/django_developer/` and `docs/web_developer/` as needed
3. **security-review** — Review the git diff for security concerns

Report their results to the user.

### 8. Resolve the File
Update the issue/request file:

```markdown
## Resolution

**Status**: resolved
**Date**: <YYYY-MM-DD>

### What Was Built
<concise summary>

### Files Changed
- `<file>` — <what changed>

### Tests
- `<test file>` — <what is tested>
- Run: `bin/run_tests -t <target>`

### Docs Updated
- `<doc file>` — <what changed>

### Security Review
<summary of findings or "No concerns">

### Follow-up
- <any remaining items, or "None">
```

Move the file to `planning/done/`.

### 9. Report
Print a concise summary:
- What was built
- Files changed
- Test results
- Doc updates
- Security findings (if any)
- What the user should run in their Django project to validate (migrations, etc.)

## Rules

- Always confirm the plan with the user before implementing.
- Tests come after implementation, not before.
- Run tests yourself with `bin/run_tests` — do not ask the user to run them.
- Commit but NEVER push.
- Stage specific files, never `git add -A`.
