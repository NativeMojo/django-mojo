# AI Development Workflow — django-mojo

This project uses Claude Code with structured skills, rules, and agents for AI-assisted development.

## Quick Start

Skills are invoked with `/<name>` in Claude Code.

### Report a Bug
```
/bug <description of the problem>
```
Investigates the code, best-effort confirms the bug, writes an issue file to `planning/issues/`.

### Request a Feature
```
/request <description of what you want>
```
Explores the codebase, clarifies scope interactively, writes a request file to `planning/requests/`.

### Plan an Implementation
```
/plan planning/issues/<file>.md
/plan planning/requests/<file>.md
```
Reads the file, designs an implementation approach, adds a `## Plan` section after user confirmation.

### Build It
```
/build planning/requests/<file>.md
```
Implements the plan, writes tests, commits (no push), then automatically runs test suite, updates docs, and does a security review.

### Show Memory
```
/memory
```
Displays all stored Claude Code memories for this project.

## Workflow Chain

Each step is ideally its own Claude session. The file carries context between sessions.

```
/bug or /request
  |
  |  writes file to planning/issues/ or planning/requests/
  v
/plan <file>
  |
  |  adds ## Plan section to the file
  v
/build <file>
  |
  |  implements, commits, then spawns 3 agents in parallel:
  |    - test-runner: runs full test suite, fixes trivial errors, reports complex ones
  |    - docs-updater: reads git diff, updates django_developer/ and web_developer/ docs
  |    - security-review: checks diff for permission gaps, injection, auth bypasses
  |
  |  moves file to planning/done/
  v
Done.
```

**Why separate sessions?** Each phase benefits from a fresh context window. Investigation context is captured in the file, so the next session starts clean without burned context.

## Planning Directory

```
planning/
  issues/      Bug reports (from /bug)
  requests/    Feature requests (from /request)
  done/        Resolved issues and requests (from /build)
  future/      Parked ideas — not ready to plan yet
  rejected/    Declined items with rationale
```

## Skills

Skills in `.claude/skills/` are invoked with `/<name>`. Each skill runs inline in your current session.

| Skill | Purpose |
|---|---|
| `/bug` | Investigate a bug, write issue file |
| `/request` | Explore and clarify a feature request, write request file |
| `/plan` | Design implementation approach, add Plan section to file |
| `/build` | Implement, test, commit, spawn post-build agents |
| `/memory` | Show current Claude Code memory state |

## Rules (Automatic)

Rules in `.claude/rules/` are loaded automatically. You do not invoke them — Claude follows them whenever they apply.

| Rule | Scope | What It Covers |
|---|---|---|
| `core.md` | Always | request.DATA, no type hints, no migrations, KISS, security |
| `models.md` | `mojo/**/models/` | MojoModel inheritance, created/modified, RestMeta, one-per-file |
| `rest.md` | `mojo/**/rest/` | URL patterns, CRUD handlers, POST_SAVE_ACTIONS |
| `testing.md` | `tests/` | testit framework, server process isolation, assert messages |
| `docs.md` | Always | Both doc tracks, indexes, CHANGELOG |
| `performance.md` | `mojo/` | N+1 queries, missing indexes, unbounded querysets |

Path-scoped rules only load when Claude is editing files matching those patterns — they don't waste context for unrelated work.

## Agents (Automatic Post-Build)

Agents in `.claude/agents/` run in isolated context windows. The `/build` skill spawns them automatically after committing.

| Agent | Purpose |
|---|---|
| `test-runner` | Runs full test suite. Fixes trivial errors (syntax, imports). Reports complex failures without fixing them. |
| `docs-updater` | Reads git diff. Updates `docs/django_developer/` and `docs/web_developer/` to match code changes. |
| `security-review` | Reviews git diff for permission gaps, data exposure, injection risks, auth bypasses, secret leakage. |

## File Template

Issues and requests follow a standardized format with sections added progressively:

1. **Intake** (`/bug` or `/request`): Title, Type, Status, Date, Description, Context, Acceptance Criteria, Investigation
2. **Planning** (`/plan`): appends `## Plan` with steps, decisions, edge cases, testing
3. **Resolution** (`/build`): appends `## Resolution` with summary, files changed, tests, docs, security review

This progressive format means every resolved file in `planning/done/` tells the full story from bug report to fix.
