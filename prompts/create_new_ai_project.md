# Bootstrap AI Agent Workflow for a New Project

You are setting up a structured AI agent workflow for this project. Your job is to analyze the existing codebase and create all the scaffolding files needed so future AI threads can work efficiently, consistently, and with minimal wasted context.

Do not copy files from another project. Derive everything from this project's actual structure, patterns, and conventions.

---

## Step 1 — Analyze the Project

Before creating any files, read and understand the project. Answer these questions internally:

**Structure**
- What kind of project is this? (web API, library, framework, CLI tool, etc.)
- What is the primary language and framework? (Django, FastAPI, Flask, Node, etc.)
- How is the codebase organized? (apps, packages, modules, services)
- Are there existing tests? What framework? Where do they live?
- Are there existing docs? What audiences? Where do they live?

**Conventions**
- How are models defined? What base classes are used?
- How are REST endpoints or routes defined?
- Where does domain/business logic live? (services, handlers, utils)
- What naming conventions are used? (files, classes, functions, URLs)
- Are there any non-obvious patterns that a new agent must know?

**Workflow**
- Is there a `planning/` folder or similar? How are issues and requests tracked?
- Is there a changelog? How are releases structured?
- Are there any existing agent instruction files (`CLAUDE.md`, `Agent.md`, etc.)?

Read at minimum:
- The root directory listing
- `README.md` (if present)
- 2-3 representative source files (a model, a route/view, a test)
- Any existing agent instruction files

---

## Step 2 — Create the Workflow Files

Create the following files, adapted to this project's specific stack and conventions. Do not use generic boilerplate — every rule and example must reflect actual patterns in this codebase.

### `Agent.md`

The master operating contract. Every AI agent reads this first — it is the single source of truth.
`CLAUDE.md` (Claude Code's auto-load file) simply redirects here, keeping the rules agent-agnostic.

Must include:
- Mandatory thread start sequence (read Agent.md → memory.md → choose mode)
- Mode selection: planning, building, bug fixing (link to prompt files)
- Source of truth table (docs, memory, prompts)
- Non-negotiable rules (project-specific — e.g., use `request.DATA`, no type hints, etc.)
- Core conventions per layer: models, views/routes, services, tests, security
- Documentation rules (what tracks exist, what gets updated when)
- Pre-edit checklist (read before touching)
- Delivery checklist (what must be true before closing any task)

### `CLAUDE.md`

A one-line redirect. Claude Code auto-loads this file — it should immediately point to `Agent.md`.

```markdown
# [Project Name]

This file is loaded automatically by Claude Code. All agent rules and conventions live in `Agent.md` — read that file first.
```

That's it. All content goes in `Agent.md`.

### `memory.md`

Living context log. Starts mostly empty.

Structure:
```markdown
# [Project Name] Working Memory

## Memory Hygiene Rules
- Keep compact and current.
- Cap each section to 5 active bullets max.
- Prefer outcomes and decisions over narrative.
- Move completed items to Archive.

## Current Focus
- (empty — add active items here)

## Key Decisions
- (add project-specific non-obvious decisions as they are made)

## In-Progress Work
- (add active tasks here)

## Open Questions
- (add unresolved design questions here)

## Archive
- (add completed sprints and resolved items here)
```

### `prompts/planning.md`

Collaborative feature planning mode. Adapted to this project's planning artifacts (tickets, request files, etc.).

Must include:
- Role: senior engineer helping scope and design before code is written
- Workflow: understand → explore codebase → propose plan → confirm with user
- Output format: goal, what exists, what changes, design decisions, edge cases, tests, docs, open questions, ready-to-build gate
- Rules: no implementation code in this mode, resolve ambiguities before handing off

### `prompts/building.md`

Implementation mode. Mirrors the workflow used in practice.

Must include:
- Role: senior engineer building features one at a time
- Workflow: understand → plan+confirm → implement → test → docs → resolve request → repeat
- Rules: confirm plan before writing code, minimal changes, read before editing, tests after implementation
- Output format per request: request, understanding, plan, implementation, tests, docs, done, next
- Done criteria

### `prompts/bug_fixer.md`

Bug triage and fix mode.

Must include:
- Role: engineer fixing bugs from an issues folder
- Workflow: triage → regression test → plan+confirm → implement → verify → resolve issue doc → repeat
- Rules: regression must fail before fix, must pass after, never write passing-bug tests
- Output format per issue: issue, coverage, regression test, plan, confirmation gate, fix summary, validation, docs, next
- Done criteria

---

## Step 3 — Create the Planning Folder Structure

Create the following directories with a `.gitkeep` or brief `README.md` explaining each:

```
planning/
  requests/   ← new feature requests, one file per item
  issues/     ← bugs and regressions, one file per item
  done/       ← resolved requests and issues, moved here on completion
```

Each request/issue file should follow this template (create a `_template.md` in each folder):

```markdown
# [Title]

**Type**: request | bug
**Status**: open | in-progress | resolved
**Date**: YYYY-MM-DD

## Description
[What needs to be done or what is broken]

## Context
[Why this matters, relevant links or prior decisions]

## Acceptance Criteria
[What done looks like]

---
<!-- Filled in on resolution -->
## Resolution
**Status**: Resolved — YYYY-MM-DD
**Root cause** (bugs): ...
**Files changed**: ...
**Tests added**: ...
**Validation**: ...
```

---

## Step 4 — Verify and Report

After creating all files, report back:

1. **Project type and stack** — what you found
2. **Key conventions captured** — the 3-5 most important non-obvious rules you encoded
3. **Files created** — list with one-line description each
4. **What's missing** — anything you couldn't determine from the codebase that the user should fill in manually
5. **First suggested action** — what the user should do next (e.g., add the first request file, run a test suite, etc.)

---

## Rules for This Bootstrap Task

- Derive all rules from the actual codebase — do not invent conventions.
- If something is ambiguous, note it in the report as "What's missing."
- Keep all files concise — agents read these on every thread, so token cost matters.
- Do not create migration files, install packages, or run project commands.
- Do not modify existing source code.
