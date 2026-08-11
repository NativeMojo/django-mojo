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
./bin/run_tests                        # whole suite, default tier only
./bin/run_tests --all                  # whole suite + every opt-in tier (slow, extended)
./bin/run_tests --agent                # write structured report to testproject/var/test_failures.json
./bin/run_tests -t test_accounts       # run one module
./bin/run_tests -t test_accounts.login # run one test file
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

## Tiers — what runs by default, and what needs `--all`

Two opt-in tiers. `--all` turns on both.

| Tier | Tag | Meaning |
|---|---|---|
| default | *(none)* | Critical. Runs on every invocation. |
| `slow` | `requires_extra: ["slow"]` | Expensive, or only meaningful before a release. |
| `extended` | `@th.requires_extra("extended")` | Correct and worth keeping, but not a critical contract. |

They are separate words on purpose: "why is this opt-in?" should be answerable from the
tag alone. `slow` is a statement about cost, `extended` about criticality.

Currently `slow`: `test_security` (bouncer/rate-limiting, serial), `test_incident`, and
the live-assistant tests.

### Deciding the tier

**Default tier — a test belongs here regardless of what it costs if it covers:**

- a security boundary: permissions, authentication, tenant isolation, secret handling
- a core framework contract: model save/serialize, REST dispatch, graphs, `request.DATA`
- a bug that has already shipped once — the regression test stays where it will be seen
- anything whose failure means the framework is broken for *every* consumer

Cost is never on its own a reason to demote one of these. The slowest single test in the
suite is a shortlink scheme-injection test, and it stays in the default tier.

**`extended` — reasonable to demote:**

- exhaustive input matrices beyond the first representative case
- deep feature-internal variants of one app
- coverage already asserted elsewhere
- tests whose cost *is* a timeout: an assertion that nothing arrives can only pass by
  waiting. Keep the positive path in the default tier and demote the negative one.

**Not a reason to demote:** the app is optional. `requires_apps` already skips a module
whole when a project has not installed that app, so a consumer never pays for an app it
does not use.

Tag it when you write it — that is much cheaper than auditing it back out later.

To move a whole module, set `"requires_extra": ["slow"]` (or `["extended"]`) in its
`__init__.py` TESTIT config. For one test, use the `@th.requires_extra(...)` decorator.

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
