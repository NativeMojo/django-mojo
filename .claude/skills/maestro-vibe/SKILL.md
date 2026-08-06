---
name: maestro-vibe
description: >-
  Vibe-code one or more small changes in one session — explore, implement, test,
  commit — with full build discipline but no board ceremony. The lightweight
  alternative to the task→scope→build workflow for work too small to track;
  leaves a born-done history item on the board at close-out.
user-invocable: true
argument-hint: <small feature/bug/tweak description(s)>
maestro-skill-version: 10
---

# Maestro Vibe — One Session, No Board Ceremony

Same build discipline as `/maestro-build` — conventions, tests, commits,
honest reporting — with no board ceremony: no workspec, no `## Plan` push, no
stage flips, no `planning/built/` snapshot. The board hears about it once, at
close-out (step 9).

For work that is small, single-session and low-risk: a one-file fix, a small
bug, a typo, a config tweak, a tiny endpoint addition — where a workspec would
cost more than the change.

## One Change or Several

**Takes one small change or a handful.** Someone with three tweaks should not
have to invoke this three times.

- Do them **one at a time**, each through steps 2-8 — read, tier, implement,
  run, commit — before starting the next. A commit per change keeps the history
  honest and keeps a red test attributable.
- **The size gate below applies to the set, not only to each change.** Five
  "small" changes across four apps is a build wearing a vibe's clothes: sum the
  diff, not the sentence count, and offer `/maestro-task` when it adds up.
- **Verify per change, at its own tier.** Where two changes cover the same
  module, one run of that module after both is enough. Anything needing `full`
  was never vibe-sized.
- **One history item per separable change** at close-out (step 9); changes that
  are genuinely one sweep get one item naming them all.
- **Report once**, one short block per change.

## Size Gate — escalate if it isn't actually small

Sanity-check the scope before the first edit. If the change is bigger than it
sounded — multiple apps touched, a schema/contract change, permission design,
anything you'd want a reviewed plan for — **stop and say so**: "This is bigger
than a vibe — want me to file it with `/maestro-task` instead?" Never grind a
large change through with no plan just because the session started as a vibe.
Same mid-build: if the diff sprawls past what was described, pause and offer to
escalate.

## Workflow

1. Parse the ask from the arguments (or ask what they want) — one change, or
   several, in which case name the list back and take them in order. One or two
   clarifying questions max — if scope needs a real Q&A session, escalate to
   `/maestro-task`.
2. Read `AGENTS.md` when present, plus `CLAUDE.md`, applicable project rules,
   and every file you'll touch — no blind edits. Fast, not sloppy.
3. **Pick the verification tier** — the same three `/maestro-scope` assigns,
   decided inline since there is no plan to carry one:
   - **`none`** — a test run would prove nothing (docs, comments, prose), or
     the change is verified more convincingly some other way. Say what.
   - **`targeted`** — the default: name the modules that cover the change and
     run only those, after. **No baseline.**
   - **`full`** — unbounded blast radius (migration, shared helper, contract).
     Take a green baseline first; needing `full` is proof this was never
     vibe-sized, so offer `/maestro-task` instead.

   Below `full` there is no up-front baseline. If a test goes red, attribute it
   then: read the failure, and if that doesn't settle it, `git stash -u`,
   re-run that one test, unstash. A failure that predates you is reported, not
   silently fixed.
4. For a bug: write a regression test that reproduces it and confirm it FAILS
   before the fix. Every tier, including `none`.
5. Implement, matching existing patterns. Tests with the change, not after.
6. Run what the tier calls for — no more. Fix failures in your code, not the
   tests. Escalate a tier if the diff outgrew it; never quietly relax one.
7. Update docs/changelog per the repo's conventions if behavior changed.
8. Commit per the repo's git conventions (no push unless its rules say so).
9. **File the history item** — one board item on the repo's maestro board
   (`.claude/maestro.json`), born done:
   - Title `Vibe: <what shipped>`, stage = the board's done value, owner = the
     user, plus `project` from that same config when it carries one.
   - `contract={"tier": <step 3's tier>, "run": [<what you actually ran>],
     "evidence": [<`tests` / `visual` / `manual` / `command`>]}` — the tier as
     data, and here declared equals actual by construction, which is what makes
     vibe work queryable beside scoped work. At `none`, `run` still names any
     rider that ran (a bug's regression test) and `evidence` names what stood
     in for a suite.
   - Description = the whole record, kept brief, in a workspec's two tiers: a
     sentence or two of plain language on what shipped and why anyone would
     care, then `---` and `## Spec` with the commit shas (one line each), how
     it was verified, and anything deliberately left open. Five lines is a
     normal vibe spec — do not pad it.
   - Skip it when the vibe closed an existing tracked item (that close-out IS
     the history) or the session escalated to `/maestro-task`.
   - Maestro unreachable: say so in the report and move on — this never blocks
     a close-out.
10. Report back briefly: what changed (commits, one line each), how it was
    verified — the tier, what ran, and at `none` the plain statement that no
    tests ran and what stood in for them — and anything left open. Keep the
    report as small as the change.

## Board Touchpoints

- **During the build the board is not part of this flow.** No tracking items,
  no stage flips, no progress comments — the conversation is the work record
  until close-out.
- The **only** item a vibe creates is step 9's. Work that deserves a live board
  item deserves `/maestro-task` — hand off.
- If the change closes an **existing** board item (you notice one, or the user
  names one), do that item's close-out instead: comment the commits, flip its
  stage — any session that finishes a tracked item flips it. No separate
  history item then.
- **Name what you touched.** The history item, or any item you closed, gets its
  title and URL — not a bare `#588` — and the board by name (`board "Backlog"`,
  not `board 8`). Ids are internal keys the reader has to look up.
- **Say who wrote it.** Pass `client=` (the client you are running in) and
  `model=` (your model id) on the `create_board_item`, `update_board_item` and
  `comment_on_item` calls a close-out makes. The server cannot observe either,
  so a write that does not say is recorded under the default label.

## Forbidden

- Silently growing a vibe into a multi-session build — escalate instead.
- Writing no tests for a behavior change because "it's just a vibe" — the tier
  decides which tests *run*, never whether the change is covered.
- Reporting a vibe as verified when the tier's run did not happen.
- Creating live tracking items, workspecs, or `planning/` files — the born-done
  history item at close-out is the single exception, filed once, at the end,
  only for completed vibes.

