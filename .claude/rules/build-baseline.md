# Build Baseline — Establish Green BEFORE Touching Code

Non-negotiable. Before writing ANY code for a build/fix task, capture a baseline
test run so that every later failure is unambiguously attributable to your change.
This eliminates the wasteful "is this failure mine?" investigation after the fact.

## The rule
1. **Before the first edit**, run the default suite:
   `bin/run_tests --agent`
   Do **NOT** use `--full` for the baseline — it is heavy (opt-in modules like
   `test_security`) and runs ONLY when the user explicitly asks for it (e.g.
   pre-publish validation). The default suite is the baseline.
2. Read `var/test_failures.json` (NOT terminal output). Record the baseline in the
   work item (e.g. under `## Notes`): total / passed / failed / skipped, and the
   names of any pre-existing failures.
3. **Interpret the baseline:**
   - **All green** → every failure you see after your change is YOURS. Fix all of
     them before closing. No exceptions, no "pre-existing" excuses.
   - **Some red at baseline** → STOP and tell the user the suite is already failing
     before you started. Do not build on a red baseline unless the user explicitly
     says to proceed; if they do, the recorded pre-existing set is the ONLY thing
     you may attribute to "not mine."
4. After implementing, run the full suite again and compare against the baseline.
   The only acceptable end state is: baseline failures (if any the user accepted)
   and nothing new.

## Why
- Attribution must be decided UP FRONT, by evidence, not reconstructed later by
  stashing/guessing. Re-running clean HEAD after the fact to ask "was it me?" is
  exactly the waste this rule removes.
- A green baseline turns "we can never have failing tests" into a checkable
  invariant: green before → green after.

## Notes
- One test run at a time (shared port + Postgres DB — see `.claude/rules/git.md`).
- Use `--agent` always; read the JSON report, never parse terminal scrollback.
- `--full` is never part of routine work — run it only when the user explicitly
  requests it. Failures that appear only under `--full` (opt-in modules) are out of
  scope for a normal build unless the user asks.
