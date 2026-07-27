---
name: maestro-vibe
description: >-
  Vibe-code a small change in one session — explore, implement, test, commit —
  with full build discipline but no board ceremony. The lightweight alternative
  to the task→scope→build workflow for work too small to track; leaves a
  born-done history item on the board at close-out.
user-invocable: true
argument-hint: <small feature/bug/tweak description>
maestro-skill-version: 2
---

# Maestro Vibe — One Session, No Board Ceremony

The whole task→scope→build workflow collapsed into one quick session. Same
engineering discipline as `/maestro-build` (conventions, tests, commits,
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
`/maestro-task` instead?" Don't grind a large change through with no plan
just because the session started as a vibe. The same applies mid-build: if
the diff is sprawling past what was described, pause and offer to escalate.

## Workflow

1. Parse the ask from the arguments (or ask what they want). One or two
   clarifying questions max — if scope needs a real Q&A session, that's a
   sign to escalate to `/maestro-task`.
2. Read the repo's `CLAUDE.md` / `.claude/rules/` and every file you'll
   touch — no blind edits. Vibe means fast, not sloppy.
3. Establish the green test baseline per the repo's test conventions if
   you'll be running tests. If the baseline is red, tell the user before
   building on it.
4. For a bug: write a regression test that reproduces it and confirm it
   FAILS before the fix.
5. Implement, matching existing patterns. Tests with the change, not after.
6. Run the relevant tests; fix failures in your code, not the tests.
7. Update docs/changelog per the repo's conventions if behavior changed.
8. Commit per the repo's git conventions (no push unless its rules say so).
9. **File the history item**: one board item on the repo's maestro board
   (`.claude/maestro.json`), born done — title `Vibe: <what shipped>`,
   stage set to the board's done value, owner = the user, project set when
   the repo maps to one. The description is the whole record, kept brief:
   what changed, the commit shas (one line each), how it was verified, and
   anything deliberately left open. Skip it when the vibe closed an
   existing tracked item (that item's close-out IS the history) or when the
   session escalated to `/maestro-task`. If maestro is unreachable, say so
   in the report and move on — the history item never blocks a close-out.
10. Report back briefly: what changed (commits, one line each), how it was
    verified, anything left open. No 15-line ceremony — this was small,
    the report should be too.

## Board Touchpoints

- **During the build, the board is not part of this flow.** No tracking
  items, no stage flips, no progress comments — the conversation is the
  work record until close-out.
- The **only** item a vibe creates is step 9's born-done history record.
  Work that deserves a live board item deserves `/maestro-task` — hand off.
- If the change turns out to close an **existing** board item (you notice
  one, or the user names one), do that item's close-out instead: comment
  the commits and flip its stage — finishing a tracked item flips its stage
  regardless of which skill did the work. No separate history item then.

## Forbidden

- Silently growing a vibe into a multi-session build — escalate instead.
- Skipping tests or committing unverified work because "it's just a vibe".
- Creating live tracking items, workspecs, or `planning/` files — the
  born-done history item at close-out is the single exception, and it is
  filed once, at the end, only for completed vibes.
