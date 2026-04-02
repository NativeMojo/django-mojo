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
./bin/run_tests                        # run all tests (skips opt-in modules)
./bin/run_tests --full                 # run all tests including opt-in modules
./bin/run_tests --agent                # run all tests, write structured report to var/test_failures.json
./bin/run_tests -t test_accounts       # run one module
./bin/run_tests -t test_accounts.login # run one test file
./bin/run_tests -q                     # quick tests only
./bin/run_tests -v                     # verbose output
./bin/run_tests -s                     # stop on first failure
./bin/run_tests -s --continue          # resume from last failure
```

If the server is already running (e.g. during active development), `bin/run_tests` leaves it running after the tests finish.

The test runner flushes the PostgreSQL database and Redis before each run for a clean state. The `--continue` flag skips the flush and resumes from the last failed test file.

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
./bin/asgi_local             # run in foreground (Ctrl-C to stop)
```

Server runs on `http://127.0.0.1:5555`. Redis is started automatically if not already running.

## Opt-in Modules

Some test modules are slow or only relevant before publishing. They are skipped by default and require `--full` (or `--extra slow`) to run:

| Module | Why opt-in |
|---|---|
| `test_security` | Bouncer/rate-limiting tests (~20s, serial) |

To add more opt-in modules, set `"requires_extra": ["slow"]` in the module's `__init__.py` TESTIT config.

## Agent Mode

`--agent` writes `var/test_failures.json` — a structured JSON report designed for LLM agents and CI pipelines. It includes:

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
├── test_security/      # route security audit (opt-in: --full)
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
