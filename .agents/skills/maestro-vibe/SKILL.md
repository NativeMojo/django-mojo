---
name: maestro-vibe
description: >-
  Vibe-code a small change in one session — explore, implement, test, commit —
  with full build discipline but no board ceremony. The lightweight
  alternative to the task→scope→build workflow for work too small to track;
  leaves a born-done history item on the board at close-out.
---

<!-- Generated from .claude/skills/maestro-vibe/SKILL.md (maestro-skill-version: 4). Do not edit directly. -->

# Maestro Vibe — One Session, No Board Ceremony

The whole task→scope→build workflow collapsed into one quick session. Same
engineering discipline as `$maestro-build` (conventions, tests, commits,
honest reporting) — but the conversation is the work record while you build.
No workspec, no `## Plan` push, no stage flips, no `planning/built/`
snapshot. The board hears about it exactly once, after the fact: a brief
born-done history item filed at close-out, so vibed work is searchable
later instead of existing only in one chat log.

Use this for work that is small, single-session, and low-risk: a one-file
fix, a small bug, a typo, a config tweak, a tiny endpoint addition. The kind
of thing where writing a workspec would cost more than the change itself.

## Size Gate — escalate if it isn't actually small

Before the first edit, sanity-check the scope. If exploration reveals the
change is bigger than it sounded — multiple apps touched, a schema/contract
change, permission design, anything you'd want a reviewed plan for — **stop
and say so**: "This is bigger than a vibe — want me to file it with
`$maestro-task` instead?" Don't grind a large change through with no plan
just because the session started as a vibe. The same applies mid-build: if
the diff is sprawling past what was described, pause and offer to escalate.

## Workflow

1. Parse the ask from the arguments (or ask what they want). One or two
   clarifying questions max — if scope needs a real Q&A session, that's a
   sign to escalate to `$maestro-task`.
2. Read the repo's `AGENTS.md` / `.claude/rules/` and every file you'll
   touch — no blind edits. Vibe means fast, not sloppy.
3. **Pick the verification tier** — the same three `$maestro-scope` assigns,
   decided inline here since there is no plan to carry one:
   - **`none`** — a test run would prove nothing (docs, comments, prose), or
     the change is verified more convincingly some other way. Say what.
   - **`targeted`** — the default: name the modules that cover the change and
     run only those, after. **No baseline.**
   - **`full`** — unbounded blast radius (migration, shared helper, contract).
     Take a green baseline first — and treat needing `full` as a signal this
     was never vibe-sized: offer `$maestro-task` instead.

   Below `full` there is no up-front baseline. If a test goes red, attribute
   it then: read the failure, and if that doesn't settle it, `git stash -u`,
   re-run that one test, unstash. A failure that predates you is reported,
   not silently fixed.
4. For a bug: write a regression test that reproduces it and confirm it
   FAILS before the fix. This happens at every tier and costs seconds.
5. Implement, matching existing patterns. Tests with the change, not after.
6. Run what the tier calls for — no more. Fix failures in your code, not the
   tests. Escalate a tier if the diff outgrew it; never quietly relax one.
7. Update docs/changelog per the repo's conventions if behavior changed.
8. Commit per the repo's git conventions (no push unless its rules say so).
9. **File the history item**: one board item on the repo's maestro board
   (`.claude/maestro.json`), born done — title `Vibe: <what shipped>`,
   stage set to the board's done value, owner = the user, and the `project`
   from that same config when it carries one. The description is the whole
   record, kept brief: what changed, the commit shas (one line each), how it
   was verified, and anything deliberately left open. Skip it when the vibe closed an
   existing tracked item (that item's close-out IS the history) or when the
   session escalated to `$maestro-task`. If maestro is unreachable, say so
   in the report and move on — the history item never blocks a close-out.
10. Report back briefly: what changed (commits, one line each), how it was
    verified — the tier, what ran, and at `none` the plain statement that no
    tests ran and what stood in for them — and anything left open. No 15-line
    ceremony — this was small, the report should be too.

## Board Touchpoints

- **During the build, the board is not part of this flow.** No tracking
  items, no stage flips, no progress comments — the conversation is the
  work record until close-out.
- The **only** item a vibe creates is step 9's born-done history record.
  Work that deserves a live board item deserves `$maestro-task` — hand off.
- If the change turns out to close an **existing** board item (you notice
  one, or the user names one), do that item's close-out instead: comment
  the commits and flip its stage — finishing a tracked item flips its stage
  regardless of which skill did the work. No separate history item then.
- **Name what you touched.** Reporting the history item — or any item you
  closed — means its title and URL, not a bare `#588`, and the board by name
  (`board "Internal"`, not `board 8`). Ids are internal keys; a reader who
  gets one has to go look it up.

## Forbidden

- Silently growing a vibe into a multi-session build — escalate instead.
- Writing no tests for a behavior change because "it's just a vibe" — the
  tier decides which tests *run*, never whether the change is covered.
- Reporting a vibe as verified when the tier's run did not happen.
- Creating live tracking items, workspecs, or `planning/` files — the
  born-done history item at close-out is the single exception, and it is
  filed once, at the end, only for completed vibes.
