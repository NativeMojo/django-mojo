# TestIt Tiers — buckets, presets, and budgets

A test declares the **bucket** it belongs to; a runner selects a **preset**, a
named set of buckets. This replaces the old two-axis "default tier vs. `--all`"
model, which had no middle ground between "everything critical" and "everything
at all" and let the default tier grow to thousands of tests.

> **Status.** Live (maestro #2790–#2793). The suite is curated into buckets,
> a **bare `bin/run_tests` is the ≤30s `core` baseline** (~9s today); `--tier
> framework` runs django-mojo's own critical tier and `--all` runs everything.
> Consumers configure their own tiers via `apps/tests/testit.json` (see *For
> consumers* below). Legacy `default_core` / `requires_extra` keys still map onto
> buckets automatically (below), so a package that predates the `tier` key keeps
> working.

## Buckets

| Bucket | What belongs here |
|---|---|
| `core` | The ≤30s baseline every consumer runs. Security boundaries, the handful of framework contracts whose failure means django-mojo is broken for everyone. Held to the strictest isolation contract. |
| `framework` | django-mojo's own critical contracts (the old default tier). Parallel-safe. |
| `bug` | One isolated regression per fixed bug. Runs in the framework preset. |
| `extended` | Correct-but-not-critical coverage, exhaustive input matrices, deep feature-internal variants. |
| `admin` | Admin-portal coverage most consumers do not care about. |
| `edge` | Edge-deployment coverage most consumers do not care about. |
| `slow` | Expensive; only meaningful before a release. |

`serial` is **not** a bucket — it stays an orthogonal execution attribute (a
package that must run sequentially for a reason other than isolation, e.g.
signal handlers). A bucket says *what* a test is; `serial` says *how* it runs.

## Presets

A runner selects buckets with `--tier` (repeatable). Three names are presets;
any other name is a literal single bucket.

| Preset | Selects | Budget |
|---|---|---|
| `core` | `core` | 30s (hard-fail; non-advisory GitHub status on every push) |
| `framework` | `core` + `framework` + `bug` | 90s (warn locally; advisory in GitHub CI until #2813) |
| `all` | every bucket | — |

```bash
./bin/run_tests                          # the core preset (the default)
./bin/run_tests --tier core              # just the ≤30s baseline
./bin/run_tests --tier framework         # django-mojo's own critical tier
./bin/run_tests --tier admin --tier edge # two literal buckets
./bin/run_tests --all                    # everything (== --tier all)
./bin/run_tests --extra slow             # framework preset + ad-hoc slow tag
```

`--extra X` still adds an ad-hoc tag on top of the preset (consumer muscle
memory), and `--all` remains an alias for `--tier all`. `-t pkg` runs that
package regardless of its bucket. `--list-tiers` (alias of `--list-extras`)
statically lists declared tags.

## Declaring a bucket

Whole package, in its `__init__.py`:

```python
TESTIT = {"tier": "extended", "serial": True}   # serial required if it mutates
```

A single test, or a whole file:

```python
@th.tier("bug")
@th.django_unit_test("regression: widget 500 on empty name")
def test_widget_empty_name(opts):
    ...

# or, at the top of a file, for every test in it:
TESTIT_TIER = "bug"
```

A per-test/per-file tag pulls that test into its bucket even when the package
sits in another — so `--tier core` visits a `framework` package to run its
`@th.tier("core")` tests. A test whose bucket is not selected is a **counted
skip**, never a silent no-op.

### Legacy mapping (no edits required)

| Legacy TESTIT | Bucket(s) |
|---|---|
| `default_core: True` | `framework` |
| `requires_extra: ["slow"]` | `slow` (opt-in, as before) |
| neither (permissive / consumer) | `framework` (runs by default) |

Declaring both `tier` and `default_core` is an error — one vocabulary per
package.

## The isolation contract per bucket

The fail-closed AST scanner (see [Overview](Overview.md) → *The enforced
isolation policy*) applies a contract keyed on the bucket:

- **`core`** — the strictest. No isolation violation of any kind, `serial`
  forbidden (core runs in the parallel ring), `cold_budget` must be 0, and
  `requires_extra` forbidden (core is always-run, not opt-in). The strict scan
  additionally flags `Setting.set()`/`Setting.remove()` classmethod writes of
  protected keys, `server_settings()` reloads (they freeze every parallel
  worker), and scans `_`-prefixed helper files. A single `@th.tier("core")`
  test (or `TESTIT_TIER = "core"`) lifts its **whole package** to the strict
  scan — a core test runs in the parallel core ring even inside a package that
  declares another tier, and it may call a `_`-prefixed helper, so the whole
  package (helpers included) is held to the core contract, fail-closed.
- **`framework`** (and legacy `default_core`) — the parallel ring: no hot
  isolation violation; `serial` allowed only for a violation-free package that
  is serial for an execution reason. The two-sided `cold_budget` ratchet
  applies. This scan is byte-identical to the pre-tier behavior.
- **`bug` / `extended` / `admin` / `edge` / `slow`** — opt-in ring: `serial:
  True` is mandatory whenever the scan finds mutation; exempt from the
  `cold_budget` ratchet (their isolation story is serial execution).

## Budgets

Wall-clock budgets are declared in `tests/testit.json`:

```json
{ "budgets": { "core": 30, "framework": 90 } }
```

- A run over `budget × 1.25` is a **violation**; under `0.6 × budget` is a
  **stale-warning** (lower the budget so it cannot silently regrow).
- The `core` preset **fails the exit code** when over budget — it is the
  baseline gate. Other presets warn locally.
- `TESTIT_BUDGET_SCALE` (env) multiplies budgets for slow machines.
- A package may declare its own `"time_budget": <secs>` in TESTIT beside
  `cold_budget`; an over-budget module is flagged whatever preset ran it.
- Budgets are skipped for partial runs (`-t` / `--ignore`) — those are not the
  preset's wall clock.

## Diagnostics

Any test that runs longer than `TESTIT_SLOW_DUMP_SECS` (default 15) triggers a
dump of every thread's stack into the agent report (`slow_stacks`) and
`testit.log` — the tool for finding what a hung or pathologically slow test is
blocked on under the parallel suite.

The agent report also carries the selected `preset`, each module's `tier`, and
`budget_violations`. Each preset reports to maestro as its own suite identity so
a `core` run cannot report green over a red `extended` module.

## For consumers (apps built on django-mojo)

An app that runs its own tests through testit gets the same buckets, presets and
isolation contract. What differs from django-mojo's own suite is configured in
**`apps/tests/testit.json`**, and the app test root is **not** flushed between
runs.

### Scaffolding a package

```bash
./bin/run_tests --init <package_name>
```

writes `apps/tests/<package_name>/__init__.py` (a `tier: "core"` example whose
docstring teaches the buckets and the parallel-safety contract) and, if absent,
a starter `apps/tests/testit.json`. It never overwrites an existing file and
creates the `apps/tests` directory chain if needed. The `django-mojo-skeleton`
ships the same example package, and `bin/create_testproject` generates it too.

### `apps/tests/testit.json`

| Key | Meaning |
|---|---|
| `budgets` | Wall-clock budgets per preset, e.g. `{"core": 30}`. |
| `default_preset` | The preset a **bare** `bin/run_tests` selects. Set it to `"framework"`. |
| `isolation` | `"enforce"` opts the app test root into the fail-closed isolation scan. Omit for the historical exemption. |
| `production_prefixes` | Your app's own shared-surface import prefixes (e.g. `["myapp."]`), treated as shared when `isolation` is `"enforce"`. Falls back to `INSTALLED_APPS`. `mojo.`/`django.`/`testit.` are always shared. |

**`default_preset` is not optional for a consumer.** django-mojo's bare run
selects the `core` preset, and a consumer's packages are `framework`-bucket by
default — so a bare `bin/run_tests` with no `default_preset` selects **nothing**
and reads as green. Set `"default_preset": "framework"` so a bare run runs your
tests; `--init` and the skeleton set it for you. An unknown or non-concrete
value falls back to `core`, never to an empty selection.

### Is my test core-eligible?

Put a test in `core` only if ALL of these hold: it is a security or contract
boundary worth running in every fast baseline; it mutates no process-wide shared
state (django settings, `os.environ`, the `mojo.helpers.*` singletons, protected
`Setting` rows); it is parallel-safe; and its package declares `cold_budget: 0`.
Everything else is `framework` (your critical, parallel-safe tests) or an opt-in
bucket (`extended`/`admin`/`edge`/`slow`), which must be `serial: True` whenever
it mutates shared state.

### Cleanup: consumer runners do not flush

django-mojo's own runner flushes Postgres and Redis before each run; **a consumer
runner does not.** Every setup function must delete the rows it is about to create
before creating them, so each test is correct on a long-lived database.

## Current residents (curated in maestro #2792)

Measured walls on the curated suite (`-j8`): **core ≈9s** (the bare run), **framework
≈45s**, **all ≈375s**. The whole suite was ~313s before the epic.

- **core** (~500 tests) — the security/contract boundary tests that must hold for every
  consumer: auth/token validation, permission and tenancy gates, SSRF/redirect guards,
  protected-key denial, model FK/graph permission contracts. Kept small and clean so the
  bare run stays under 30s.
- **framework** — django-mojo's own critical contracts not in the universal baseline
  (model save/serialize, REST dispatch, jobs routing, most of the default_core packages'
  non-core tests).
- **bug** — one isolated regression per fixed bug, tagged in place beside its fixture.
- **extended** — exhaustive input matrices, feature-internal variants, and the heavy
  provider/matrix files (test_assistant, test_maestro_board, most of test_aws, the edge
  onboarding/alias/serving matrices).
- **admin / edge** — admin-portal (`test_account/test_admin_*`, cloud registries) and
  edge-deployment (`test_mojosec`, `test_edge_action`, the AWS infra plane) coverage most
  consumers do not exercise.
- **slow** — real-internet / real-provider tests (live DNS/WHOIS in `test_helpers/domains`,
  live LLM in `test_assistant/3_test_live_assistant`, shortlink scrapers, cloudwatch tail).

Two known follow-ups from the curation: (1) `test_account` and `test_edge` could not take
`core` tags because the whole-package strict scan flags a pre-existing protected `Setting`
write / a `_helpers.py` settings mutation elsewhere in the package — their core-worthy tests
stay in `framework` until those sites are relocated to serial siblings. (2) The `framework`
preset still carries the shared-state flake class (throttle/rate-limit/token/rendition tests
that assert on shared counters); they pass solo and belong in a serial bucket or need a
per-test identity seam.
