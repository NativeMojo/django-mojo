# AI Development Workflow — django-mojo

This project uses Claude Code with structured skills, rules, agents, and helper
scripts for AI-assisted development.

There is **one kind of work item**. Bugs, features, and chores differ only by a
`type` field — not by folder, template, counter, or mode. The folder an item
lives in *is* its stage, and items advance only via the helper scripts.

## Quick Start

Skills are invoked with `/<name>` in Claude Code.

### See the Board
```
scripts/board.sh                 # active pipeline: inbox / confirmed / done
scripts/board.sh confirmed       # filter to one stage
scripts/board.sh future          # a parking folder
```
One cheap line per item (id, stage, type, priority, ready/BLOCKED, title) —
only the output costs tokens, not the files it scans.

### Request New Work (chat front door)
```
/request <what you want / what's broken>   # writes planning/inbox/<slug>.md
```
`/request` (PR-style — a request for a feature, bug, or chore) determines the
`type` itself, captures a structured, **un-ID'd** item into `planning/inbox/`,
explores and clarifies (for a bug, best-effort confirms the root cause), but does
**not** implement, allocate an id, or move folders — `/scope` runs intake next.
Drop a file from `planning/_template.md` by hand for the same effect.

### Scope an Item
```
/scope <path to an inbox item, or a description of new work>
```
Owns intake. It runs `scripts/intake.sh`, which allocates the next `ITEM-###`
from `planning/.next_id`, stamps the YAML frontmatter, moves the file
`inbox/ → confirmed/`, and bumps the counter — atomically. Then it explores
(via the read-only Explore subagent) and writes a **self-contained `## Plan`** —
full enough that a cold session can build it without re-exploring — and deletes the
`PLAN PENDING` marker. No code is written. Named `/scope` (not `/plan`) to avoid
Claude Code's built-in plan mode.

### Build It
```
/build <path to a confirmed item, or its ITEM id>
```
Pre-flight refuses an `UNPLANNED` item (one still carrying the `PLAN PENDING`
marker — run `/scope` first) and `scripts/ready.sh` gates on `depends_on`. It works
**in place** (no branch/worktree — see below). It first **claims** the item with
`scripts/start.sh` (`confirmed/ → in_progress/`, WIP = 1, resume-safe), then
implements (a failing regression test first, for bugs), runs tests, commits (no
push), spawns three agents in parallel — full test suite, docs, security review —
and runs `scripts/close.sh`, which stamps the Resolution block (closed/branch/files
changed) and moves the file `in_progress/ → done/`.

### Show Memory
```
/memory
```
Displays Claude Code's local project memory for this repo (read-only).

## Workflow Chain

Each step is ideally its own Claude session. The file carries context between sessions.

```
new work
  |
  v
/request <description>
  |  determines type (bug|feature|chore); explore/clarify;
  |  writes an un-ID'd item to planning/inbox/ (id blank)
  |  (or drop a file from planning/_template.md by hand)
  v
/scope <item>
  |  scripts/intake.sh: ITEM-### + frontmatter + inbox/ -> confirmed/ + counter bump
  |  writes a self-contained ## Plan and deletes the PLAN PENDING marker
  v
/build <item>
  |  scripts/start.sh: claim confirmed/ -> in_progress/ (WIP=1, resume-safe)
  |  scripts/ready.sh pre-flight (READY/BLOCKED); refuses UNPLANNED
  |  implements, writes/runs tests, commits, then spawns 3 agents in parallel:
  |    - test-runner: runs full test suite, fixes trivial errors, reports complex ones
  |    - docs-updater: reads git diff, updates django_developer/ and web_developer/ docs
  |    - security-review: checks diff for permission gaps, injection, auth bypasses
  |  scripts/close.sh: stamp Resolution + in_progress/ -> done/
  v
Done.
```

**Why separate sessions?** Each phase benefits from a fresh context window.
Scoping context is captured in the file, so the build session starts clean.

## Helper Scripts (`scripts/`)

Deterministic, portable (macOS BSD + GNU/Linux) helpers so the must-be-exact
work isn't model-followed prose.

| Script | Purpose |
|---|---|
| `intake.sh` | Allocate ID, stamp frontmatter, move `inbox/ → confirmed/`, bump counter (atomic). Refuses to consume a number if the item already has an id; reconciles against the tree so a stale counter can't dup. |
| `start.sh` | Claim a planned item: move `confirmed/ → in_progress/`. Idempotent resume; enforces WIP = 1; refuses an `UNPLANNED` item. Called automatically by `/build`. |
| `board.sh` | Pipeline at a glance. `board.sh [inbox\|confirmed\|in_progress\|done\|future\|rejected]`. Confirmed items show `UNPLANNED` / `ready` / `BLOCKED`; in_progress shows `wip`. |
| `ready.sh` | Dependency gate — `READY` (exit 0) / `BLOCKED` (exit 1) for an item's `depends_on`. |
| `close.sh` | Route an item to a terminal/parking folder: `close.sh <file> [done\|future\|rejected]`. `done` (default, from `in_progress/`) stamps Resolution (closed/branch/files changed) and moves to `done/`; `future`/`rejected` are plain moves (no stamp). |

## Planning Directory

```
planning/
  .next_id     Next item number to assign (single bare integer)
  _template.md The one item template (YAML frontmatter)
  inbox/       New, unscoped items (no id yet)
  confirmed/   Scoped + planned items (have id + plan, from /scope)
  in_progress/ Actively being built (claimed by /build via start.sh; WIP = 1)
  done/        Closed items (from /build via close.sh)
  future/      Parked ideas — not ready to scope (just a folder)
  rejected/    Declined items, kept for rationale (just a folder)
```

`future/` and `rejected/` are plain parking folders — no id is assigned. Park or
decline an item with `scripts/close.sh <file> future` / `... rejected` (a plain
move, no Resolution stamp); move it back to `inbox/` by hand to revive it (intake
assigns its id then).

### Item Lifecycle

1. **Intake & Plan** (`/scope` → `scripts/intake.sh`): allocates the ID, stamps
   frontmatter, moves to `confirmed/`, then writes a self-contained `## Plan` and
   deletes the `PLAN PENDING` marker. Until that marker is gone the item is
   `UNPLANNED` and `/build` refuses it.
2. **Build** (`/build` → `scripts/start.sh`): claims the item `confirmed/ →
   in_progress/` (WIP = 1, resume-safe), then implements.
3. **Resolution** (`/build` → `scripts/close.sh`): fills `## Resolution`, moves
   `in_progress/ → done/`.

IDs are assigned only by `scripts/intake.sh`, never by hand, never reused. The
folder is the stage — there is no `stage` field.

## Skills

Skills in `.claude/skills/` are invoked with `/<name>`. Each skill runs inline in your current session.

| Skill | Purpose |
|---|---|
| `/request` | Capture new work → un-ID'd item in `planning/inbox/`; determines `type` (bug/feature/chore) |
| `/scope` | Intake (`intake.sh`), triage, and plan |
| `/build` | Implement a scoped item, test, commit, spawn post-build agents, close (`close.sh`) |
| `/memory` | Show current Claude Code memory state |

## Rules (Automatic)

Rules in `.claude/rules/` are loaded automatically. You do not invoke them — Claude follows them whenever they apply. Layer rules are path-scoped via `globs:` frontmatter, so they load only when Claude edits matching files.

| Rule | Scope | What It Covers |
|---|---|---|
| `core.md` | Always | request.DATA, no type hints, no migrations, KISS, security |
| `git.md` | Always | No branches/worktrees without permission (tests share a port + Postgres DB → no parallel runs); commit format |
| `models.md` | `mojo/**/models/` | MojoModel inheritance, created/modified, RestMeta, one-per-file |
| `rest.md` | `mojo/**/rest/` | URL patterns, CRUD handlers, POST_SAVE_ACTIONS |
| `testing.md` | `tests/` | testit framework, server process isolation, assert messages |
| `docs.md` | Always | Both doc tracks, indexes, CHANGELOG |
| `performance.md` | `mojo/` | N+1 queries, missing indexes, unbounded querysets |

## Agents (Automatic Post-Build)

Agents in `.claude/agents/` run in isolated context windows. The `/build` skill spawns them automatically after committing.

| Agent | Purpose |
|---|---|
| `test-runner` | Runs full test suite. Fixes trivial errors (syntax, imports). Reports complex failures without fixing them. |
| `docs-updater` | Reads git diff. Updates `docs/django_developer/` and `docs/web_developer/` to match code changes. |
| `security-review` | Reviews git diff for permission gaps, data exposure, injection risks, auth bypasses, secret leakage. |

## Item File Format

Items use the YAML frontmatter in `planning/_template.md`, with sections added progressively:

1. **Intake & Plan** (`/scope`): frontmatter (`id`, `type`, `title`, `priority`, `effort`, `owner`, `opened`, `depends_on`, `related`, `links`), `## What & Why`, `## Acceptance Criteria`, `## Repro` (bugs), and the agreed plan in `## Notes`.
2. **Resolution** (`/build` → `close.sh`): `## Resolution` with closed date, branch, files changed, tests added.

This progressive format means every resolved file in `planning/done/` tells the full story from intake to fix.
