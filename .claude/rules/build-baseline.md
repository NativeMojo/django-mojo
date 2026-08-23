# Build Verification — What to Run, and When a Baseline Is Worth It

**The work item's verification tier decides.** `/maestro-scope` sets it (`none` /
`targeted` / `full`) and pushes it as the item's `quality_contract`; `/maestro-build`
reads it and runs exactly that. This file is the django-mojo half of that contract:
the commands, the module mapping, and which changes count as `full` **here**.

A baseline is not free — the suite serializes on one port and one Postgres database
(see `.claude/rules/git.md`), so it is minutes of the user's time, every build, twice.
Buy it when attributing a failure after the fact would actually be hard. That is the
`full` tier, and only that tier.

## Vocabulary — say what you mean

Three different things used to all get called "the full suite". They are not the same:

| Term | Command | What it runs |
|---|---|---|
| **scoped run** | `bin/run_tests --agent -t <module> [-t <module>]` | Only the named modules. |
| **whole suite** | `bin/run_tests --agent` | Every module in the default tier. |
| **all-tier run** | `bin/run_tests --agent --all` | Whole suite **plus** opt-in modules (`requires_extra`). |

Never write "full suite" when you mean the whole-suite run — `--all` is a different,
heavier thing. Say "whole suite" or "all-tier run".

> **Tier presets (maestro #2790).** A bare `bin/run_tests --agent` now selects the
> **framework** preset, which until Phase 3 is byte-identical to the whole default tier —
> so "whole suite" and `--tier framework` are the same run today. `--tier core` (the future
> ≤30s baseline) and `--tier all` (== `--all`) are also available. See
> `docs/django_developer/testit/Tiers.md`. This vocabulary table stays accurate; the bare
> run's selection does not change until Phase 3 populates `core` and flips the default.

## The rule

Read the tier from the item's `quality_contract` (or its `### Verification` block),
then:

- **`none`** → **no test run, before or after.** Do the thing the plan's `Why:` line
  named in its place — load the config and read back the resolved value, run the one
  command the change affects — and report that instead.
- **`targeted`** → **no baseline.** Build, then run the modules the plan named:
  `bin/run_tests --agent -t <module> ...`. Stop there. Running the whole suite to
  feel safe spends the user's minutes on your comfort.
- **`full`** → **baseline before the first edit**, whole suite after.
  1. `bin/run_tests --agent` before touching anything.
  2. Read `testproject/var/test_failures.json` (NOT terminal output). Record on the
     item: total / passed / failed / skipped, and the names of any pre-existing
     failures.
  3. **Red baseline → STOP** and tell the user the area is already failing before you
     started. Don't build on red without their say-so; if they say go, that recorded
     set is the ONLY thing you may attribute to "not mine."
  4. After implementing, re-run and compare. The acceptable end state is: the accepted
     baseline failures and nothing new.

**A bug's regression test runs at every tier**, on its own, before and after the fix —
a regression test nobody saw fail proves nothing. It costs seconds and is never the
thing a tier is protecting you from.

**Escalate freely, downgrade never.** If the diff picked up something the plan didn't
anticipate — a migration, a shared helper, a changed contract, a second app — move up
a tier, run it, and say in the report that you escalated and why. Moving *down* a tier
is the user's call, not yours.

**No tier on the item** (untracked work, `/maestro-vibe`, a plan written before tiers
existed) → pick one yourself from the list below and say which and why. Do **not**
default to `full` because nothing told you otherwise — that is the exact cost this
mechanism exists to remove.

## What counts as `full` in this repo

Any of these — don't deliberate:

- The change touches shared framework code: `mojo/helpers/`, `mojo/models/`,
  `mojo/decorators/`, `mojo/middleware/`, `mojo/rest/`, or `testit/`.
- Any model, migration, or `RestMeta` change (blast radius is every serializer/graph).
- The change touches **3 or more** apps.
- You cannot confidently name the blast radius. Not knowing IS the trigger.

Everything else is `targeted`, or `none` when a test run would prove nothing at all
(docs, comments, planning files, skill and prose files).

## Choosing the modules for a `targeted` run

Map what you changed to the modules that cover it:

- `mojo/apps/<app>/...` ⇒ `tests/test_<app>` (the naming is 1:1: `mojo/apps/shortlink`
  ⇒ `test_shortlink`).
- Add the obvious dependents. A change to `account` permissions also touches
  `test_global_perms` and `test_user_mgmt`; a change to auth touches `test_auth`,
  `test_register`, `test_mfa`, `test_oauth`.
- Not sure which module covers it? `grep -rl "<symbol>" tests/` and take what it finds.
- **Name them explicitly.** "The relevant tests" is not a plan — it is the build
  session guessing with the scoper's authority.
- **When in doubt, widen.** A module too many costs seconds; a module too few loses
  the coverage the tier was claiming.

## Attributing a red test without a baseline

A baseline answers exactly one question: *was this failure already there?* Below
`full`, buy that answer only when something actually fails:

1. **Read the failure first.** Most name your change unambiguously — the file, the
   assertion, the symbol you just touched. That settles it at zero cost.
2. If it doesn't: `git stash -u`, re-run **that same targeted test**, `git stash pop`.
   Seconds, and it settles the question exactly where the doubt is.
3. A failure that predates you gets **reported, not silently fixed** — it isn't yours
   and isn't part of this item. Say so in the summary.

`full` keeps its up-front baseline because attributing a red *whole suite* after the
fact is neither cheap nor unambiguous.

## When to run `--all`

- The user explicitly asks (e.g. pre-publish validation).
- You changed **what `--all` selects** — i.e. you added or moved a `requires_extra` tag.
- Otherwise `--all` is not part of routine work, and failures that appear only under it
  are out of scope for a normal build unless the user asks.

## Notes

- The report is at **`testproject/var/test_failures.json`** (`VAR_ROOT` is
  `testproject/var`, not `./var`).
- Use `--agent` always; read the JSON report, never parse terminal scrollback.
  Never `--plain` — it disables the rich progress UI and parallel execution.
- **One test run at a time per checkout.** Never run parallel suites inside one
  worktree. Different worktrees use isolated ports, databases, and Redis indexes
  and may test concurrently after each has completed its setup.
- **Scoping runs no tests.** `/maestro-scope` decides the tier; it does not verify.
  The one exception is reproducing a bug you are scoping: that single test, alone,
  never while a build is running.
- Verifying a build is ONE run at the end of the work, not a run per agent. Do not ask
  the `test-runner` agent to repeat a run you have already done. In a multi-item run,
  the closing run is the union of every item's modules, once, at the highest tier any
  item carries.
