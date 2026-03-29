---
name: request
description: Explore codebase, clarify scope, write a structured feature request file to planning/requests/
user-invocable: true
argument-hint: <description of what you want>
---

The user is requesting a new feature or enhancement. Your job is to explore the codebase, clarify scope, and write a structured request file. Do NOT implement anything.

## Arguments

$ARGUMENTS — The user's feature description. If empty, ask the user what they want to build.

## Workflow

### 1. Understand the Goal
Parse $ARGUMENTS. Identify what is being asked for and who it affects (backend developers, API consumers, or both).

### 2. Explore the Codebase
Read relevant models, REST handlers, services, tests, and docs. Identify:
- What already exists that can be reused
- What needs to be created or modified
- Constraints: security, permissions, backwards compatibility
- Read `docs/django_developer/README.md` to check for existing framework features

### 3. Ask Clarifying Questions
If scope is ambiguous, ask focused questions. Resolve:
- Exact API contract (endpoints, fields, responses) if applicable
- Permission model (who can do what)
- Edge cases the user cares about
- What is explicitly out of scope

Do not proceed with a vague request. A good request file is unambiguous enough to plan against.

### 4. Write the Request File
Create `planning/requests/<slug>.md`:

```markdown
# <Title>

**Type**: request
**Status**: open
**Date**: <YYYY-MM-DD>
**Priority**: high | medium | low

## Description
<What needs to be built — clear, specific>

## Context
<Why this matters, relevant background, who asked for it>

## Acceptance Criteria
- <Specific, testable criteria — when are we done?>

## Investigation
**What exists**: <relevant code/patterns already in place>
**What changes**: <file-level breakdown of what gets added/modified>
**Constraints**: <security, compat, or other concerns identified>
**Related files**: <list of files in scope>

## Endpoints (if applicable)
| Method | Path | Description | Permission |
|---|---|---|---|
| ... | ... | ... | ... |

## Settings (if applicable)
| Setting | Default | Purpose |
|---|---|---|
| ... | ... | ... |

## Tests Required
- <List of test scenarios that should be written>

## Out of Scope
- <Explicitly excluded items>
```

### 5. Stop and Hand Off
Print:
- The request file path
- A one-line summary of the feature
- `To plan the implementation, start a new session and run: /plan planning/requests/<slug>.md`

## Rules

- Do NOT write implementation code. Exploration and documentation only.
- Do NOT move files between planning folders.
- Resolve ambiguities before writing the file.
- Keep the slug short and descriptive (e.g., `webhook-retry`, `group-permissions-ui`).
