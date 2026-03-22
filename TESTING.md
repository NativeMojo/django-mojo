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

## Starting the Test Server

```bash
./bin/asgi_local start       # start in background
./bin/asgi_local stop        # stop
./bin/asgi_local restart     # restart
./bin/asgi_local status      # check if running
./bin/asgi_local             # run in foreground (Ctrl-C to stop)
```

Server runs on `http://127.0.0.1:5555`. Redis is started automatically if not already running.

## Running Tests

```bash
./bin/testit.py                        # run all tests
./bin/testit.py -t test_accounts       # run one module
./bin/testit.py -t test_accounts.login # run one test file
./bin/testit.py -q                     # quick tests only
./bin/testit.py -v                     # verbose output
./bin/testit.py -s                     # stop on first failure
```

The test runner flushes the PostgreSQL database and Redis before each run for a clean state.

## Full Test Workflow

```bash
./bin/create_testproject     # first time, or after schema changes
./bin/asgi_local start
./bin/testit.py
./bin/asgi_local stop
```

## Test Layout

Tests live in `tests/` at the repo root, organised by module:

```
tests/
├── test_accounts/      # auth, login, tokens, sessions
├── test_helpers/       # crypto, settings, content_guard, etc.
├── test_security/      # route security audit
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
