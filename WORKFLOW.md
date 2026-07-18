# The Planning Workflow — Map & Setup Guide

A single, file-driven pipeline for AI-assisted development: every piece of work
flows **`request → scope → build → done`**, one item at a time, with the folder
an item lives in *being* its status. This document is two things:

1. **An explainer** — how the workflow works, for anyone new to the repo.
2. **A setup guide** — a complete manifest of every file the workflow uses, and
   how to drop the same system into another project.

For the day-to-day command reference see [`AI_DEV.md`](AI_DEV.md); for project
coding conventions see [`CLAUDE.md`](CLAUDE.md). This file is the *architecture
+ portability* view that ties them together.

---

## 1. The mental model (four rules)

1. **One kind of work item.** Bugs, features, and chores are the *same* thing —
   a markdown file with a `type:` field. No separate folders, templates, or
   counters per type.
2. **The folder is the stage.** An item's directory *is* its status. It advances
   only by moving folders — and only the helper scripts move it.

   ```
   inbox/  →  confirmed/  →  in_progress/  →  done/
   (raw)      (scoped)       (building)        (closed)
   ```
3. **One ID space.** Every item gets `DM-###`, allocated exactly once by
   `scripts/intake.sh`. Never hand-assigned, never reused. The prefix (`DM`)
   comes from `planning/.config`.
4. **Scope before build.** Nothing is built until it carries a self-contained
   `## Plan`. The absence of the `PLAN PENDING` marker *is* the "designed"
   signal — there is no status field.

---

## 2. The chain

Each phase is ideally its own fresh Claude session; the **item file carries all
context between them**, so a build session starts clean.

```
  new work
    │
    ▼
  /request  "<what you want / what's broken>"
    │   • classifies type (bug|feature|chore), explores/clarifies
    │   • writes an un-ID'd item to  planning/inbox/<slug>.md
    ▼
  /scope  <inbox item>
    │   • triage skim first — pushback (future/ | rejected/) is a valid outcome;
    │       saying "no" is half of triage's value
    │   • scripts/intake.sh → allocates DM-###, stamps frontmatter,
    │       moves inbox/ → confirmed/, bumps planning/.next_id
    │   • a cheap DRAFTER sub-agent proposes a verdict + draft plan; the session
    │       then VERIFIES its load-bearing claims first-hand (drafter proposes,
    │       reviewer disposes)
    │   • writes a self-contained ## Plan, stamps build routing
    │       (build_strategy + build_model), deletes the PLAN PENDING marker
    │   • gate: explicit user sign-off; "approved — build it" chains into /build
    ▼
  /build  <confirmed item | DM-###>
    │   • pre-flight: scripts/ready.sh (depends_on satisfied?) + not UNPLANNED
    │   • scripts/start.sh → claims confirmed/ → in_progress/ (WIP = 1)
    │   • executes per build_strategy (default inline):
    │       inline   — run in-session
    │       delegate — ONE builder sub-agent runs it end-to-end on build_model;
    │                  the session VERIFIES its output before close
    │       fanout   — L/XL + disjoint file partitions only; builders write code,
    │                  the orchestrator is the SOLE test-runner
    │   • establishes a GREEN test baseline, then implements
    │       (a failing regression test FIRST, for bugs)
    │   • runs tests, updates docs, commits (no push)
    │   • spawns 3 post-build agents in parallel:
    │       test-runner · docs-updater · security-review
    │   • scripts/close.sh → stamps ## Resolution, moves in_progress/ → done/
    ▼
  done/  — the file now tells the whole story: intake → plan → resolution
```

Two side-folders sit outside the main line — **`future/`** (parked ideas) and
**`rejected/`** (declined, kept for the rationale). They're plain folders; no ID
is allocated. `scripts/close.sh <file> future|rejected` moves an item there;
move it back to `inbox/` by hand to revive it.

### Two sub-agent seams

`/scope` and `/build` each delegate the expensive middle to a sub-agent, then keep
judgement in the driving session:

- **Scope — drafter / review.** A cheap **drafter** (read-only; sees the item + the
  tree) returns a *verdict* — `proceed` · `proceed-reduced` · `already-covered` ·
  `not-now` · `needs-clarification` — and, only for a proceed verdict, a draft plan
  with `file:line` evidence. The session then verifies the load-bearing claims
  first-hand; it never rubber-stamps. Scoping runs at a cheap review tier, and the
  model cost lands on verification, not recon. (Escalation: skip the drafter and
  scope in-session for P0/P1 security-surface work or L/XL items.)
- **Build — inline / delegate / fanout.** `/scope` stamps `build_strategy` and
  `build_model` onto the item (rubric: risk biases the model *upward*, size only
  sets the floor). `inline` runs in-session; `delegate` hands the whole build to one
  builder on `build_model` and the session **verifies its output before close**
  (Resolution, test report, `git log -p` spot-check); `fanout` (L/XL + disjoint file
  partitions only) splits code across builders while the orchestrator stays the sole
  test-runner.

**One test-runner, always.** Whatever the strategy, exactly one entity ever runs
the suite (shared port + Postgres DB). "approved — build it" lets a `/scope` session
chain straight into a `delegate` build running in the background — freeing the
session to `/scope` the next item, while `start.sh`'s WIP = 1 still serializes the
actual builds.

---

## 3. Everyday commands

| Command | What it does |
|---|---|
| `scripts/board.sh` | The pipeline at a glance — one cheap line per item (`id · stage · type · priority · state · title`). Only the output costs tokens, not the files it scans. `board.sh confirmed` / `board.sh future` filters to a stage. |
| `/request <desc>` | Chat front door. Captures new work as an un-ID'd `inbox/` item. Determines the type itself. |
| `/scope <item>` | Triage/pushback + intake + planning. A cheap drafter proposes; the session verifies first-hand. Allocates the ID, writes the `## Plan`, stamps build routing. |
| `/build <item>` | Implements a scoped item per its `build_strategy` (inline/delegate/fanout): claim → baseline → code + tests → commit → post-build agents → close. Verifies a delegate before close. |
| `/memory` | Shows Claude Code's local project memory (read-only). |

On the board, a `confirmed/` item shows `UNPLANNED` (intook but no plan yet — a
`/build` will refuse it), `ready`, or `BLOCKED` (a `depends_on` isn't in
`done/`); an `in_progress/` item shows `wip`.

---

## 4. Complete file manifest

Everything the workflow touches, grouped. The **Portability** column is the
key for §6:

- ✅ **copy as-is** — generic machinery, works unchanged in any repo
- ✏️ **copy + edit** — reusable shape, but has project-specific values to change
- 🔧 **rewrite** — encodes *this* project's stack/conventions; keep the file, replace the content

### Skills — `.claude/skills/*/SKILL.md` (invoked as `/<name>`)

| File | Purpose | Port |
|---|---|---|
| `skills/request/SKILL.md` | Turn a chat ask into one structured `inbox/` item; classify `type`; explore/clarify. Does **not** allocate an ID or implement. | ✏️ |
| `skills/scope/SKILL.md` | Triage/pushback + intake (`intake.sh`); spawn a cheap drafter, verify its load-bearing claims first-hand, write the self-contained `## Plan`, and stamp build routing. Gates on user sign-off. | ✏️ |
| `skills/build/SKILL.md` | Implement a scoped item per its build routing (inline/delegate/fanout), with tests; commit; spawn post-build agents; close. Verifies a delegate before close. | ✏️ |
| `skills/memory/SKILL.md` | Display Claude Code's local project memory. | ✏️ |

> Skills embed a few project commands (`bin/run_tests --agent`,
> `bin/create_testproject`, the `docs/*` tracks). The *workflow logic* is
> generic; edit those embedded commands per project.

### Rules — `.claude/rules/*.md` (loaded automatically, never invoked)

Rules with a `globs:` frontmatter line load only when Claude edits a matching
path; the rest are always active.

| File | Scope | Covers | Port |
|---|---|---|---|
| `rules/core.md` | always | `request.DATA`, no type hints, KISS, fail-closed security, domain-logic placement | 🔧 |
| `rules/git.md` | always | **No branches/worktrees without permission** (tests share a port + Postgres DB → no parallel runs); commit format & trailer | ✏️ |
| `rules/build-baseline.md` | always | Capture a **green test baseline before the first edit** so every later failure is attributable | ✏️ |
| `rules/docs.md` | always | Keep both doc tracks + `CHANGELOG.md` in sync | 🔧 |
| `rules/models.md` | `mojo/**/models/` | MojoModel inheritance, `created`/`modified`, RestMeta, one-model-per-file | 🔧 |
| `rules/rest.md` | `mojo/**/rest/` | URL patterns, CRUD handlers, `POST_SAVE_ACTIONS` | 🔧 |
| `rules/testing.md` | `tests/` | testit framework, server-process isolation, assert messages | 🔧 |
| `rules/performance.md` | `mojo/**/*.py` | N+1 queries, missing indexes, unbounded querysets | 🔧 |

### Agents — `.claude/agents/*.md` (spawned automatically by `/build` after commit)

Each runs in its own isolated context window.

| File | Model | Purpose | Port |
|---|---|---|---|
| `agents/test-runner.md` | sonnet | Run the full suite; fix trivial errors (syntax/imports); report complex failures without fixing | ✏️ |
| `agents/docs-updater.md` | sonnet | Read the git diff; update both doc tracks to match | 🔧 |
| `agents/security-review.md` | opus | Review the diff for permission gaps, data exposure, injection, auth bypasses | ✏️ |

> **Not every sub-agent is an agent file.** Scope's **drafter** and build's
> **delegate / fanout builders** are spawned ad-hoc via the `Task` tool with a
> `model` override (`build_model`) — they run this repo's own skills, so they need
> no `.claude/agents/*.md` file. Only the three persistent post-build reviewers
> above live as agent files.

### Scripts — `scripts/*.sh` (the deterministic, must-be-exact machinery)

Portable across macOS (BSD) and Linux (GNU). **All ✅ — they're driven entirely
by `planning/.config`, so they need no editing.**

| File | Purpose |
|---|---|
| `scripts/intake.sh` | Allocate the next ID, stamp frontmatter, move `inbox/ → confirmed/`, bump the counter — atomically. Refuses to consume a number if the item already has an `id`; reconciles against the tree so a stale counter can't duplicate. |
| `scripts/start.sh` | Claim a planned item: `confirmed/ → in_progress/`. Idempotent "resume"; enforces **WIP = 1**; refuses an `UNPLANNED` item. |
| `scripts/ready.sh` | Dependency gate — `READY` (exit 0) / `BLOCKED` (exit 1) by checking each `depends_on` is in `done/`. Handles cross-repo deps (`repo#ID`) as manual-verify notes. |
| `scripts/close.sh` | Route an item to a terminal folder. `done` (default) stamps the `## Resolution` block (closed date, branch, changed files from git) and moves to `done/`; `future`/`rejected` are plain moves. |
| `scripts/board.sh` | Render the pipeline table from frontmatter. |

### Planning tree — `planning/`

| Path | Purpose | Port |
|---|---|---|
| `planning/.config` | Shell-sourced config. Sets `PREFIX=DM` (the item-ID prefix). | ✏️ (set your prefix) |
| `planning/.next_id` | Single bare integer — the next item number. | ✏️ (reset to `1`) |
| `planning/_template.md` | The one item template (YAML frontmatter + section skeleton). | ✅ |
| `planning/inbox/` | New, unscoped items (no ID yet). | ✅ (empty dir) |
| `planning/confirmed/` | Scoped + planned items (have ID + `## Plan`). | ✅ (empty dir) |
| `planning/in_progress/` | Actively being built (WIP = 1). | ✅ (empty dir) |
| `planning/done/` | Closed items — the project's decision history. | ✅ (empty dir) |
| `planning/future/` | Parked ideas. | ✅ (empty dir) |
| `planning/rejected/` | Declined items, kept for rationale. | ✅ (empty dir) |

### Root docs & config

| Path | Purpose | Port |
|---|---|---|
| `CLAUDE.md` | Auto-loaded project instructions: conventions, trust order, the "start every thread here" checklist. | 🔧 |
| `AI_DEV.md` | The command-reference companion to this file. | ✏️ |
| `WORKFLOW.md` | *This file* — architecture map + setup guide. | ✏️ |
| `memory.md` | Committed **working memory** — non-obvious decisions & watch-list, read at the top of every thread. Starts nearly empty. | ✅ (empty scaffold) |
| `TESTING.md` | The full test workflow (`bin/run_tests`, `bin/create_testproject`). | 🔧 |
| `.claude/settings.local.json` | **Local only (gitignored)** — per-machine permissions/config. Not part of the portable scaffold. | — |

> **Two memories, don't confuse them.** `memory.md` (above) is committed and
> shared. `/memory` shows Claude Code's *separate* per-user local memory (under
> `~/.claude/projects/…`), which is not a repo file and doesn't travel with a
> clone.

---

## 5. Item file format & lifecycle

Every item starts from `planning/_template.md` and grows section-by-section, so
a finished `done/` file reads as the complete story: intake → plan → resolution.

**Frontmatter** (YAML, first block):

```yaml
---
id: DM-014             # allocated by intake.sh; never hand-set or reused
type: bug              # feature | bug | chore
title: Short imperative title
priority: P2           # P0 (drop everything) | P1 | P2 | P3
effort: M              # XS | S | M | L | XL
owner: backend
opened: 2026-06-05     # ISO date
depends_on: []         # hard blockers: [DM-003, otherrepo#WA-007]
related: []            # soft links
links: []              # external URLs
build_strategy: inline # inline (default) | delegate | fanout — stamped by /scope
build_model: sonnet    # sonnet | opus | fable — builder model (default: session model)
---
```

`build_strategy` / `build_model` are optional and blank in the template — `/scope`
stamps them at plan time (the user can override at build approval). Absent = `inline`
+ the session model.

**Sections, added by phase:**

| Phase | Added by | Sections |
|---|---|---|
| Intake | `/request` | `## What & Why`, `## Acceptance Criteria`, `## Repro` (bugs) |
| Plan | `/scope` | `## Plan` (Goal · Context · Changes · Design decisions · Edge cases · Tests · Docs · Open questions · Build routing); stamps `build_strategy`/`build_model`; the `PLAN PENDING` marker is **deleted** |
| Resolution | `/build` → `close.sh` | `## Resolution` (closed date · branch · files changed · tests added) |

The `PLAN PENDING` HTML comment is the gate: while it's present the item is
`UNPLANNED` and `scripts/start.sh` / `/build` refuse it.

---

## 6. Setting this up in a new project

The workflow is deliberately layered so the **machinery is generic** and only a
thin edge is project-specific.

### Copy verbatim (✅)

```
scripts/                    # all five *.sh — driven by .config, no edits
planning/_template.md
planning/{inbox,confirmed,in_progress,done,future,rejected}/   # empty dirs
```

(Git doesn't track empty dirs — drop a `.gitkeep` in each, or let the first
item create it.)

### Create fresh (✏️)

```
planning/.config    →  PREFIX=<YOURPREFIX>     # e.g. WM, HB, RA — the ID prefix
planning/.next_id   →  1                        # start the counter
memory.md           →  empty "Working Memory" scaffold (Key Decisions / Watch List)
```

### Copy then adapt (✏️ / 🔧)

1. **Skills** (`.claude/skills/`) — copy all four, then find-and-replace the
   embedded project commands:
   - the test command (here `bin/run_tests --agent` + reading a JSON report),
   - the schema/migration step (here `bin/create_testproject`),
   - the doc-track paths (here `docs/django_developer/`, `docs/web_developer/`).
2. **Rules** (`.claude/rules/`) — keep the *file set and the always/scoped
   split*, but rewrite the content for your stack. Portable in spirit:
   `git.md` (no unauthorized branches; the test-isolation reason), and
   `build-baseline.md` (green-before-you-start). Fully project-specific:
   `core.md`, `models.md`, `rest.md`, `testing.md`, `performance.md`, `docs.md`.
   Point each scoped rule's `globs:` at your source layout.
3. **Agents** (`.claude/agents/`) — copy the three post-build agents; edit their
   instructions to name your doc tracks and test command. Pick models to taste.
4. **`CLAUDE.md`** — write your project instructions. Include a "Planning"
   section describing this pipeline and a "start every thread here" checklist
   (read `CLAUDE.md` → read `memory.md` → run `scripts/board.sh` → pick a mode).
5. **`AI_DEV.md`** — adapt the command reference (mostly prefix/command swaps).

### Bootstrap checklist

- [ ] `scripts/` copied; `chmod +x scripts/*.sh`
- [ ] `planning/` dirs created; `.config` prefix set; `.next_id` = `1`
- [ ] `planning/_template.md` copied
- [ ] `.claude/skills/` copied + project commands swapped
- [ ] `.claude/rules/` written for your stack (globs pointed at your tree)
- [ ] `.claude/agents/` copied + doc tracks / test command adjusted
- [ ] `CLAUDE.md` written (conventions + planning section + thread checklist)
- [ ] `memory.md` empty scaffold committed
- [ ] Smoke test: `/request` a trivial chore → `/scope` it → confirm `DM-001`
      (or your prefix) lands in `confirmed/` with a plan → `scripts/board.sh`
      shows it → `/build` it → it lands in `done/`

### What makes it portable

The scripts never hard-code the prefix, paths, or item count — they read
`planning/.config` and reconcile IDs against the actual tree. Everything
project-specific is either in `.config` (the prefix) or in prose the model reads
(skills, rules, agents, `CLAUDE.md`). Swap that thin edge and the same
deterministic pipeline runs anywhere.

---

## 7. Invariants & gotchas (cheat-sheet)

- **WIP = 1.** At most one item in `in_progress/`. `start.sh` refuses a second.
- **One test runner at a time.** Tests share a dedicated port + a Postgres DB,
  so parallel runs corrupt each other. Never run the suite in two places at
  once; **exactly one entity ever runs it** — the inline session, the delegate
  builder, or the fanout orchestrator (fanout builders write code but never test).
- **Never rubber-stamp the drafter.** `/scope` verifies the drafter's load-bearing
  claims first-hand before presenting — a false `already-covered` costs as much as
  a bad plan.
- **Verify a delegate before close.** A `delegate` build's output is checked
  first-hand (Resolution, test report, `git log -p` spot-check) before the session
  relays it as done.
- **No branches/worktrees without explicit permission** — same reason (parallel
  checkouts collide on the port + DB). Work in place on `main`.
- **Green baseline before the first edit.** Capture it, record it in the item;
  then every new failure is unambiguously yours.
- **IDs are allocated once, by `intake.sh`, never reused** — even if the counter
  is stale or an item was merged back, it reconciles against the tree.
- **`PLAN PENDING` is the build gate** — an intook-but-unplanned item is refused
  until `/scope` finishes the plan and deletes the marker.
- **Advance only via the scripts** — never hand-move a file between planning
  folders (you'd skip ID allocation, the WIP check, or the Resolution stamp).
- **Commit by explicit pathspec** — stage named files, never `git add -A`; the
  planning tree often carries unrelated in-flight items.

---

*See also: [`AI_DEV.md`](AI_DEV.md) (command reference) ·
[`CLAUDE.md`](CLAUDE.md) (coding conventions) · [`TESTING.md`](TESTING.md) (test
workflow) · [`memory.md`](memory.md) (working memory).*
