# Django-MOJO

This file is loaded automatically by Claude Code.

## Project

Django-mojo is a Django backend framework providing models, REST, auth, jobs, metrics, realtime, chat, security, and more. It is a library/framework, not a standalone runnable project.

## Start Every Thread Here

1. Read this file in full.
2. Read `memory.md`.
3. Work is tracked on the **maestro board** (workspace `NativeMojo`, shared
   with web-mojo — see `.claude/maestro.json` for the live workspace/board
   id). Choose your mode:
   - Filing new work (bug/feature/chore) → `/maestro-task`
   - Triaging / planning an item → `/maestro-scope`
   - Implementing a scoped item  → `/maestro-build`
   - A batch of items, mostly autonomous (one approval gate) → `/maestro-auto`
   - Too small to track — one-file fix, typo, config tweak → `/maestro-vibe`
4. If maestro is unreachable or unauthenticated, the `maestro-*` skills stop
   with an explicit notice — fall back to the local flow (`/request` →
   `/scope` → `/build`; see "Local Fallback Workflow" under Planning). Never
   fall back silently.
5. Read `docs/django_developer/README.md` before building — do not reinvent existing features.

## How to Work Here

- **Rules** are in `.claude/rules/` and load automatically. Follow them.
- **Skills** are in `.claude/skills/` — invoked with `/<name>`. Primary:
  `/maestro-task`, `/maestro-scope`, `/maestro-build`, plus `/maestro-auto`
  (batch scope+build behind one approval gate) and `/maestro-vibe` (small
  single-session change, no board ceremony). The `maestro-*` skills are
  upstream-managed: they are installed verbatim from `get_dev_skills()` and
  carry a `maestro-skill-version` — never hand-edit them, or the next sync
  clobbers the edit. Repo-specific build rules belong in `.claude/rules/` or
  the workspace rule docs (`django-mojo-build-conventions`,
  `nativemojo-board-conventions`) instead. Local fallback (maestro
  down/unauthenticated only): `/request`, `/scope`, `/build`. Always
  available: `/memory`.
- **Agents** are in `.claude/agents/` — spawned automatically by
  `/maestro-build` (and by the fallback `/build`).
- See `AI_DEV.md` for the full developer workflow.

## Planning

Work items live on **two maestro boards** in the `NativeMojo` workspace, both
shared with web-mojo (see `.claude/maestro.json`):

| Board | Id | Holds |
|-------|----|-------|
| **Inbox** | 11 | Everything that is not security — bugs, features, chores. |
| **Security** | 37 | All security work: exploitable weaknesses, defense-in-depth hardening, and features whose purpose is a security control. |

`.claude/maestro.json` names **one** board (11) — that is the filing default,
not the whole queue. Routing to Security, and the rest of the two-board
protocol, lives in the workspace `nativemojo-board-conventions` rule doc, which
every `maestro-*` skill fetches via `get_workspace_context`. Read it before
filing, scoping or building. Three things it says that bite if you miss them:

- **Enumerate both boards.** Any time you list work — pick something to scope
  or build, report what's open, check WIP — call `get_board(11)` *and*
  `get_board(37)`. Listing only 11 hides all in-flight security work.
- **Match stage options by value, per board.** Both boards label a stage
  "Accepted", but Inbox backs it with `scoped` and Security with `accepted`.
  A cross-board move of an item at `scoped` silently lands it at `inbox`.
- **WIP = 1 per project is summed across both boards**, not per board.

An item's bug/feature/chore *kind* is still carried by its workspec, not by
which board it sits on. Security is routed as a cross-cutting concern — a
different axis from kind.

> The separate **Bugs** and **Feature Requests** intake boards were
> consolidated into board 11 on 2026-07-30 and archived; **Security** (37) was
> split out on 2026-07-31. If you find a doc describing three intake boards, or
> one describing a single board with no routing, it predates one of those
> changes — the workspace rule doc wins.

A board item's markdown
description is the workspec; its `stage` column value is inbox → scoped →
planned → building → review → done, plus **parked** (= on hold for now —
distinct from MoSCoW `Won't`, which means never); priority is MoSCoW
(must/should/could/won't); its `project` column names the repo it belongs to
— **django-mojo items only** are worked from this repo. The
`.claude/skills/maestro-*` skills (task/scope/build) read and write items
there — see each `SKILL.md` for the exact protocol, plus the workspace's
shared `nativemojo-board-conventions` rule doc (project stamping, repo match
on claim, WIP = 1 per project) and the repo-specific
`django-mojo-build-conventions` rule doc (both fetched via
`get_workspace_context`): one django-mojo item `building` at a time, the
post-build agent trio (test-runner, docs-updater, security-review), and
optional build routing.

A work item is board-backed XOR file-backed — never both. `planning/` itself
still holds:
- `done/` — the pre-migration historical archive (183 items closed before the
  2026-07-19 maestro move), plus any future fallback-mode closures. Browse
  with `scripts/board.sh done` or `git log`.
- `future/` / `rejected/` — pre-migration parked/declined items, kept for
  rationale only.
- `.cache/` (gitignored) — scratch working copy `/maestro-scope` and
  `/maestro-build` pull an item's description into for the session.
- `built/` — commit-time snapshots `/maestro-build` writes at claim time (the
  board item remains the source of truth; these are a local paper trail).

### Local Fallback Workflow

If maestro is unreachable or unauthenticated, `/request` → `/scope` → `/build`
still work exactly as before, entirely file-based:

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
  inbox/         # new, unscoped items (no id yet) — fallback only
  confirmed/     # scoped + planned items (have id + plan) — fallback only
  in_progress/   # actively being built (claimed by /build; WIP = 1) — fallback only
  done/          # closed items (pre-migration archive + any fallback closures)
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
