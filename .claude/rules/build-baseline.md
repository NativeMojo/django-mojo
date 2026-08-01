# Build Baseline — Establish Green BEFORE Touching Code

Non-negotiable. Before writing ANY code for a build/fix task, capture a baseline
test run so that every later failure is unambiguously attributable to your change.
This eliminates the wasteful "is this failure mine?" investigation after the fact.

**The baseline is scoped to what your change can affect** — not the whole suite.
Attribution is still decided up front by evidence; you just buy it for the area you
are touching instead of for all ~3,300 tests, every time, twice.

## Vocabulary — say what you mean

Three different things used to all get called "the full suite". They are not the same:

| Term | Command | What it runs |
|---|---|---|
| **scoped run** | `bin/run_tests --agent -t <module> [-t <module>]` | Only the named modules. **The default baseline.** |
| **whole suite** | `bin/run_tests --agent` | Every module in the default tier. |
| **`--full`** | `bin/run_tests --agent --full` | Whole suite **plus** opt-in modules (`requires_extra`). |

Never write "full suite" when you mean the whole-suite run — `--full` is a different,
heavier thing. Say "whole suite" or "`--full`".

## The rule

1. **Choose the scope** (see below), then run it **before the first edit**:
   `bin/run_tests --agent -t <module> ...`
   Always `--agent`. One test run at a time (shared port + Postgres — see
   `.claude/rules/git.md`).
2. Read **`testproject/var/test_failures.json`** (NOT terminal output). Record in the
   work item: the scope you chose, total / passed / failed / skipped, and the names of
   any pre-existing failures.
3. **Interpret the baseline:**
   - **All green** → every failure you see afterwards in that scope is YOURS. Fix all of
     them before closing. No exceptions, no "pre-existing" excuses.
   - **Some red at baseline** → STOP and tell the user the area is already failing before
     you started. Do not build on red unless the user explicitly says to proceed; if they
     do, the recorded pre-existing set is the ONLY thing you may attribute to "not mine."
4. **After implementing**, re-run **the same scope** and compare. The only acceptable end
   state is: baseline failures (if any the user accepted) and nothing new.
5. **Widen once at the end** if your change grew past the scope you picked, or if any
   escalation trigger below became true while you were building. Re-scoping mid-build is
   normal — silently keeping a too-narrow scope is not.

## Choosing the scope

Map what you changed to the modules that cover it:

- `mojo/apps/<app>/...` ⇒ `tests/test_<app>` (the naming is 1:1: `mojo/apps/shortlink`
  ⇒ `test_shortlink`).
- Add the obvious dependents. A change to `account` permissions also touches
  `test_global_perms` and `test_user_mgmt`; a change to auth touches `test_auth`,
  `test_register`, `test_mfa`, `test_oauth`.
- Not sure which module covers it? `grep -rl "<symbol>" tests/` and take what it finds.
- **When in doubt, widen.** A scope that is too narrow silently loses attribution — the
  one property this rule exists to protect. A scope that is too wide only costs seconds.

## Escalate to the WHOLE SUITE when

Any of these is true — do not deliberate, just run the whole suite:

- The change touches shared framework code: `mojo/helpers/`, `mojo/models/`,
  `mojo/decorators/`, `mojo/middleware/`, `mojo/rest/`, or `testit/`.
- Any model, migration, or `RestMeta` change (blast radius is every serializer/graph).
- The change touches **3 or more** apps.
- You cannot confidently name the blast radius. Not knowing IS the trigger.

## When to run `--full`

- The user explicitly asks (e.g. pre-publish validation).
- You changed **what `--full` selects** — i.e. you added or moved a `requires_extra` tag.
- Otherwise `--full` is not part of routine work, and failures that appear only under it
  are out of scope for a normal build unless the user asks.

## Why

- Attribution must be decided UP FRONT, by evidence, not reconstructed later by
  stashing/guessing. Re-running clean HEAD after the fact to ask "was it me?" is
  exactly the waste this rule removes. A scoped baseline still answers the only
  question that matters: *was this area already red before I touched it?*
- Green before → green after stays a checkable invariant. Scoping changes how much you
  pay for it, not whether you have it.

## Notes

- The report is at **`testproject/var/test_failures.json`** (`VAR_ROOT` is
  `testproject/var`, not `./var`).
- Use `--agent` always; read the JSON report, never parse terminal scrollback.
- Verifying a build is ONE run at the end of the work, not a run per agent. Do not
  ask the `test-runner` agent to repeat a whole-suite run you have already done.
