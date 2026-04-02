---
name: bug
description: Investigate a bug report, confirm if possible, write a structured issue file to planning/issues/
user-invocable: true
argument-hint: <description of the problem>
---

The user is reporting a bug. Your job is to investigate, confirm if possible, and write a structured issue file. Do NOT fix the bug in this session.

## Arguments

$ARGUMENTS — The user's bug description. If empty, ask the user to describe the problem.

## Workflow

### 1. Understand the Report
Parse $ARGUMENTS. If the description is ambiguous, ask one focused clarifying question before investigating.

### 2. Investigate the Code
- Read the relevant models, REST handlers, services, and tests
- Trace the code path described in the bug report
- Identify the likely root cause or narrow it to 2-3 candidates

### 3. Best-Effort Confirmation
- If you can confirm the bug through code analysis alone (logic error, missing guard, wrong field name, race condition), state your confidence level
- If a regression test is feasible without a running project, write one in `tests/` and run it with `bin/run_tests --agent -t <target>` to demonstrate the failure (read `var/test_failures.json` for diagnostics)
- If confirmation requires a running server or specific data state, say so — don't force it

### 4. Write the Issue File
Create `planning/issues/<slug>.md`:

```markdown
# <Title>

**Type**: bug
**Status**: open
**Date**: <YYYY-MM-DD>
**Severity**: critical | high | medium | low

## Description
<What is broken — from the user's report, refined with your findings>

## Context
<Why this matters, what is affected, who is impacted>

## Acceptance Criteria
- <What "fixed" looks like — specific, testable statements>

## Investigation
**Likely root cause**: <your finding>
**Confidence**: confirmed | high | medium | speculative
**Code path**: <file:line references tracing the bug>
**Regression test**: <file path if written, or "not feasible — requires <reason>">
**Related files**: <list of files that will likely need changes>
```

### 5. Stop and Hand Off
Print:
- The issue file path
- A one-line summary of what you found
- `To plan the fix, start a new session and run: /bug planning/issues/<slug>.md`

## Rules

- Do NOT implement a fix. Investigation and documentation only.
- Do NOT move files between planning folders.
- If the bug cannot be confirmed, say so honestly and set confidence to "speculative".
- Keep the slug short and descriptive (e.g., `login-mfa-bypass`, `job-retry-stuck`).
