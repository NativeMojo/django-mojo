# Build Verification — What to Run, and How to Judge a Red Run

**The work item's verification tier decides.** `/maestro-scope` sets it (`none` /
`targeted` / `full`) and pushes it as the item's `quality_contract`; `/maestro-build`
reads it and runs exactly that. This file is the django-mojo half of that contract:
the commands, the module mapping, and which changes count as `full` **here**.

**No tier takes a baseline** (Ian, 2026-09-25). Nothing runs before the first edit.
A run is minutes of the user's time; one run after the change is the whole cost. The
one question a baseline used to answer — *was this failure already there?* — is
answered from the recorded `main` run instead (see "Judging a red run" below).

## Vocabulary — say what you mean

Three different things used to all get called "the full suite". They are not the same:

| Term | Command | What it runs |
|---|---|---|
| **scoped run** | `bin/run_tests --agent -t <module> [-t <module>]` | Only the named modules. |
| **core run** | `bin/run_tests --agent` | The `core` preset — the core baseline, 40s budget (bare run since #2792). |
| **whole suite** | `bin/run_tests --agent --tier framework` | django-mojo's own critical tier (`core`+`framework`+`bug`). |
| **all-tier run** | `bin/run_tests --agent --all` | Everything, including opt-in `extended`/`admin`/`edge`/`slow`. |

Never write "full suite" when you mean the whole-suite run — `--all` is a different,
heavier thing. Say "whole suite" or "all-tier run".

> **Tier presets (maestro #2790/#2792).** The suite is curated into buckets, and a bare
> `bin/run_tests --agent` now runs the **`core`** preset — the core baseline (40s budget) every consumer
> runs. **The `full` verification tier below therefore means `--tier framework`, not the
> bare run.** `--tier core` (== the bare run), `--tier framework` (the whole critical tier),
> and `--tier all` (== `--all`) are the presets; see `docs/django_developer/testit/Tiers.md`.
> Note: the framework preset still carries the pre-existing shared-state flake class
> (throttle/rate-limit/token tests that assert on shared counters) — a red there that passes
> solo is a schedule-dice flake, not your change.

## The rule

Read the tier from the item's `quality_contract` (or its `### Verification` block),
then:

- **`none`** → **no test run, before or after.** Do the thing the plan's `Why:` line
  named in its place — load the config and read back the resolved value, run the one
  command the change affects — and report that instead.
- **`targeted`** → **no baseline.** Build, then run the modules the plan named:
  `bin/run_tests --agent -t <module> ...`. Stop there. Running the whole suite to
  feel safe spends the user's minutes on your comfort.
- **`full`** → **no baseline either.** Build, then ONE whole-suite run after the
  change: `bin/run_tests --agent --tier framework`. Read
  `testproject/var/test_failures.json` (NOT terminal output) and record on the item:
  total / passed / failed / skipped, and the names of any failures. Judge each red
  test as below.

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

## Judging a red run

At every tier, judge a red test only when something actually fails, as the workspace
rule `testit-running-tests` says:

1. **Read the failure first.** Most name your change unambiguously — the file, the
   assertion, the symbol you just touched. That settles it at zero cost.
2. **The recorded `main` run:** `test_status(12, branch="main")`. A test already red
   there is not yours.
3. **The workspace's `known-flaky-tests` fact.** A listed flake that passes alone is
   not yours.
4. **Still unsettled:** rerun **that one file** on a clean `main` worktree. Red there
   too means it predates you.
5. A failure that predates you gets **reported, not silently fixed** — it isn't yours
   and isn't part of this item. Say so in the summary.

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
