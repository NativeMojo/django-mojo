# Testing

Django-MOJO is a framework, not a project. To run the test suite a host Django project is required. The `bin/create_testproject` script generates one locally inside `testproject/` (gitignored).

## Prerequisites

- Python 3.10+
- PostgreSQL (with a running server)
- Redis (running locally)
- uv: `pip install uv`

## First-Time Setup

```bash
uv sync
./bin/create_testproject
```

This generates `testproject/`, creates a `mojo_test` PostgreSQL database, and runs migrations. Safe to re-run — it wipes and recreates cleanly each time.

## Running Tests

Use `bin/run_tests` — it starts the server, runs the suite, and stops the server automatically:

```bash
./bin/run_tests                        # the core preset — the ≤30s baseline (~9s)
./bin/run_tests --tier framework       # django-mojo's own critical tier (~45s)
./bin/run_tests --all                  # everything, incl. extended/admin/edge/slow (~6min)
./bin/run_tests --agent                # write structured report to testproject/var/test_failures.json
./bin/run_tests -t test_accounts       # run one module (regardless of tier)
./bin/run_tests -t test_accounts.login # run one test file
./bin/run_tests -v                     # verbose output
./bin/run_tests -s                     # stop on first failure
./bin/run_tests -s --continue          # resume from last failure
```

If the server is already running (e.g. during active development), `bin/run_tests` leaves it running after the tests finish.

The test runner flushes the PostgreSQL database and Redis before each run for a clean state (the migration tracker itself is left alone, so a plain `migrate` stays a no-op on a run that didn't change models). The `--continue` flag skips the flush and resumes from the last failed test file.

## First-Time Workflow

```bash
./bin/create_testproject     # first time, or after schema changes
./bin/run_tests
```

## Managing the Test Server Manually

If you need to control the server directly:

```bash
./bin/asgi_local start       # start in background
./bin/asgi_local stop        # stop
./bin/asgi_local restart     # restart
./bin/asgi_local status      # check if running
./bin/asgi_local wait        # block until the server answers HTTP (30s cap)
./bin/asgi_local             # run in foreground (Ctrl-C to stop)
```

Server runs on `http://127.0.0.1:5555`. Redis is started automatically if not already running.

Both `bin/asgi_local` and `bin/run_tests` isolate the process from your real AWS
account before doing anything else: shared credentials/config files are pointed at
`/dev/null`, IMDS is disabled, and any `AWS_ACCESS_KEY_ID`/`AWS_PROFILE`-family
variable is cleared. Tests seed AWS access through mojo-level `Setting` rows, never
your ambient credential chain — a local `~/.aws/credentials` or `AWS_PROFILE` has no
effect on a test run.

## Tiers — buckets and presets

A test declares the **bucket** it belongs to; a runner selects a **preset**, a named set
of buckets. A bare `bin/run_tests` runs the **`core`** preset — the ≤30s baseline every
consumer runs (~9s today). **[docs/django_developer/testit/Tiers.md](docs/django_developer/testit/Tiers.md)
is the canonical reference** — buckets, presets, the legacy mapping, the per-bucket
isolation contract, and budgets. In brief:

| Preset | Command | Runs | Wall |
|---|---|---|---|
| `core` | `bin/run_tests` | the ≤30s baseline | ~9s |
| `framework` | `bin/run_tests --tier framework` | `core`+`framework`+`bug` — django-mojo's own critical tier | ~45s |
| `all` | `bin/run_tests --all` | everything, incl. `extended`/`admin`/`edge`/`slow` | ~6min |

Buckets: `core` (the small, clean, parallel-safe security/contract baseline), `framework`
(django-mojo's critical contracts), `bug` (one isolated regression per fixed bug),
`extended` (exhaustive matrices, feature-internal variants), `admin`/`edge` (admin-portal
and edge-deployment coverage most consumers skip), `slow` (real-internet / real-provider).

Declare a bucket at the cheapest correct scope — whole package via the TESTIT `tier` key,
whole file via `TESTIT_TIER = "bug"`, one test via `@th.tier("bug")`. See Tiers.md for the
"is my test core-eligible?" checklist and the shared-state / isolation rules. Cost alone
never demotes a security-relevant test out of a critical bucket; put exhaustive variants in
`extended`. Tag it when you write it — auditing it back out later is far more expensive.

## Continuous Integration

GitHub Actions runs the tiered suite (`.github/workflows/tests.yml`, which calls
the reusable `.github/workflows/testit.yml`). The trigger decides the preset:

| Trigger | Preset | Gate |
|---|---|---|
| every push | `core` (~9s) | **blocking** — the fast baseline must stay green |
| pull request | `framework` (~45s) | **advisory** (`continue-on-error`) until #2813 removes the shared-state flakes; deleting that one line makes it blocking |
| nightly (cron) | `all` | reported, not gating |

The reusable workflow stands up Postgres (with pgvector) and Redis service
containers, installs deps with `uv`, runs `bin/create_testproject`, then
`bin/run_tests --agent --tier <preset>`. The `core` preset's 30s budget is a
hard exit-code gate; `TESTIT_BUDGET_SCALE` (set to `2` in the workflow) gives a
cold CI host headroom — calibrate it down after one real run. `--all` also runs
under `publish.py` before a release.

Consumers wire their own CI the same way against their chosen `default_preset`;
scaffold a package with `./bin/run_tests --init <package>` (see
[testit/Tiers.md](docs/django_developer/testit/Tiers.md) → *For consumers*).

## Agent Mode

`--agent` writes `testproject/var/test_failures.json` — a structured JSON report designed for LLM agents and CI pipelines. It includes:

- **Top-level**: `status` (passed/failed), total, passed, failed, skipped, duration
- **modules**: per-module breakdown with tests/passed/failed/skipped/duration
- **failures**: per-failure diagnostics (file path, line number, test source, traceback, server log tail)

LLM agents should always use `--agent` and read the JSON report instead of parsing terminal output.

## Test Layout

Tests live in `tests/` at the repo root, organised by module:

```
tests/
├── test_accounts/      # auth, login, tokens, sessions
├── test_helpers/       # crypto, settings, content_guard, etc.
├── test_security/      # route security audit (opt-in: --all)
└── ...
```

Each test file contains functions prefixed `test_` (or `setup_` for setup steps). The runner discovers and executes them in source-file order.

## Writing Tests

```python
from testit import helpers as th

@th.django_unit_test()
def test_something(opts):
    resp = opts.client.post("/api/account/login", {"username": "...", "password": "..."})
    assert resp.status is True, "login should succeed"
```

- Decorator: `@th.django_unit_test()`
- Function signature: `def test_xxx(opts):`
- `opts.client` — HTTP client pointed at the test server
- Every `assert` must include a failure message
- Import the module under test inside the test function, not at the top of the file
- `opts.client` calls go to a separate server process — mock/patch only affects the test process, not the server
