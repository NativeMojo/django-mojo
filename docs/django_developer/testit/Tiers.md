# TestIt Tiers — buckets, presets, and budgets

A test declares the **bucket** it belongs to; a runner selects a **preset**, a
named set of buckets. This replaces the old two-axis "default tier vs. `--all`"
model, which had no middle ground between "everything critical" and "everything
at all" and let the default tier grow to thousands of tests.

> **Status.** The mechanism ships in Phase 1 (maestro #2790). Until Phase 3
> curates tests into buckets, every package still uses the legacy
> `default_core` / `requires_extra` keys, which map onto the buckets
> automatically (below), and **a bare `bin/run_tests` still runs the framework
> preset** — byte-identical to the old default tier. Phase 3 populates `core`
> and flips the bare run to it.

## Buckets

| Bucket | What belongs here |
|---|---|
| `core` | The ≤30s baseline every consumer runs. Security boundaries, the handful of framework contracts whose failure means django-mojo is broken for everyone. Held to the strictest isolation contract. |
| `framework` | django-mojo's own critical contracts — today's default tier. Parallel-safe. |
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
| `core` | `core` | 30s (hard-fail) |
| `framework` | `core` + `framework` + `bug` | 90s (warn locally, fail in CI once Phase 4 wires it) |
| `all` | every bucket | — |

```bash
./bin/run_tests                          # the framework preset (the default)
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
  forbidden (core runs in the parallel ring), `cold_budget` must be 0. The
  strict scan additionally flags `Setting.set()`/`Setting.remove()` classmethod
  writes of protected keys, `server_settings()` reloads (they freeze every
  parallel worker), and scans `_`-prefixed helper files.
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
