# Django-MOJO

This file is loaded automatically by Claude Code.

## Project

Django-mojo is a Django backend framework providing models, REST, auth, jobs, metrics, realtime, chat, security, and more. It is a library/framework, not a standalone runnable project.

## Start Every Thread Here

1. Read this file in full.
2. Read `memory.md`.
3. Run `scripts/board.sh` — the pipeline at a glance (inbox/confirmed/done).
4. Choose your mode:
   - Filing new work (bug/feature/chore) → `/request` (writes an un-ID'd item to `planning/inbox/`)
   - Triaging / planning an item → `/scope` (`.claude/skills/scope/SKILL.md`)
   - Implementing a scoped item  → `/build` (`.claude/skills/build/SKILL.md`)
5. Read the item:
   - New, unscoped → `planning/inbox/`
   - Scoped, active → `planning/confirmed/`
6. Read `docs/django_developer/README.md` before building — do not reinvent existing features.

## How to Work Here

- **Rules** are in `.claude/rules/` and load automatically. Follow them.
- **Skills** are in `.claude/skills/` — invoked with `/<name>` (`/request`, `/scope`, `/build`, `/memory`).
- **Agents** are in `.claude/agents/` — spawned automatically by `/build`.
- See `AI_DEV.md` for the full developer workflow.

## Planning

There is **one kind of work item**. Bugs, features, and chores differ only by a
`type` field — not by folder, template, counter, or mode.

- **The folder is the stage.** `inbox/ → confirmed/ → in_progress/ → done/`.
  Advance an item only with the scripts — `scripts/intake.sh` (→ confirmed),
  `scripts/start.sh` (→ in_progress), `scripts/close.sh` (→ done). There is no
  `stage` field; don't hand-move files.
- **One ID space.** Every item gets `DM-###`, allocated once by
  `scripts/intake.sh` from `planning/.next_id`. The prefix comes from
  `planning/.config` (`PREFIX=DM`; scripts default to `ITEM` when the file is
  absent). Never hand-assign, edit the counter by hand, or reuse an ID.
- **Capture, scope, build.** `/request` is the chat front door (PR-style — a
  request for a feature, bug, or chore). It determines the `type` and writes an
  un-ID'd item to `planning/inbox/` (no id yet). `/scope` owns intake (runs `scripts/intake.sh`, allocates the id,
  stamps frontmatter, moves to `confirmed/`) and planning — it writes a
  **self-contained `## Plan`** (enough for a cold session to build from) and deletes
  the `PLAN PENDING` marker. `/build` first **claims** the item with
  `scripts/start.sh` (`confirmed/ → in_progress/`), implements from that plan; bugs
  get a regression test, then it commits, spawns the post-build agents, and runs
  `scripts/close.sh`. Nothing is built until it has been scoped.
- **Build routing is optional frontmatter.** `/scope` may stamp `build_strategy`
  (`inline` | `delegate` | `fanout`) and `build_model` (`sonnet` | `opus` |
  `fable`); `/build` honors them — see the build skill's Execution Strategy
  section. Absent = inline + session model. Invariant: exactly one entity ever
  runs tests.
- **`in_progress/` = actively being built.** `/build` claims into it
  automatically; at most one item lives there (WIP = 1), and it's resume-safe — a
  half-done build is obvious, never mistaken for a fresh `ready` item.
- **The `## Plan` is the "designed" signal** — not a folder or a frontmatter status
  field. A confirmed item still carrying the `PLAN PENDING` marker is
  intook-but-unplanned: `board.sh` shows it as `UNPLANNED` and `/build` refuses it
  until `/scope` finishes.
- **Never** `/build` an item that is `UNPLANNED`, or that `scripts/ready.sh` reports
  BLOCKED (its `depends_on` aren't all in `planning/done/`).

```
planning/
  .next_id       # next item number (single bare integer)
  _template.md   # the one item template
  inbox/         # new, unscoped items (no id yet)
  confirmed/     # scoped + planned items (have id + plan)
  in_progress/   # actively being built (claimed by /build; WIP = 1)
  done/          # closed items
  future/        # parked ideas — not ready to scope (just a folder)
  rejected/      # declined items, kept for rationale (just a folder)
```

`future/` and `rejected/` are plain parking folders — no id is allocated. Park or
decline an item with `scripts/close.sh <file> future` / `... rejected` (a plain
move, no Resolution stamp); move it back to `inbox/` by hand to revive it.
`scripts/board.sh future` / `scripts/board.sh rejected` list them.

Helper scripts (`scripts/`): `intake.sh`, `board.sh`, `ready.sh`, `close.sh`.

## Trust Order

When docs and code conflict:
1. `docs/django_developer/README.md`
2. `docs/web_developer/README.md`
3. Existing code patterns in the target app
