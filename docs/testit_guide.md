# testit — Testing Framework Guide

**Read this before writing any test.** This guide covers every pattern, constraint, and
sharp edge in the testit framework. Skipping it leads to tests that appear to work but
silently do nothing, or tests that work locally but fail for the wrong reasons.

---

## Architecture Overview

testit runs tests against a **live, separate server process** (`asgi_local`). This is
not Django's `TestClient`. It is a real uvicorn process running the full Django stack.
Tests are ordinary Python functions collected and run by the testit runner.

```
┌─────────────────────────────────────────────────┐
│  testit runner (your terminal)                  │
│                                                 │
│  bin/run_tests [options]                      │
│    └── runner.py                                │
│         ├── opts.client = RestClient(host)      │  ← HTTP over the network
│         └── test functions run here            │
└────────────────────────────────┬────────────────┘
                                 │ real HTTP calls
                                 ▼
┌─────────────────────────────────────────────────┐
│  asgi_local (separate process)                  │
│                                                 │
│  bin/asgi_local start                           │
│    └── uvicorn _asgi:application --reload       │
│         └── full Django + PostgreSQL + Redis    │
└─────────────────────────────────────────────────┘
```

**Critical consequence**: anything you do in the test process — `mock.patch`,
`override_settings`, module-level state changes — has **zero effect** on the server.
They are different Python processes. See the [Process Isolation](#process-isolation)
section before writing any test that involves settings, mocking, or time travel.

---

## File Layout

Tests live in `tests/` at the repo root, organized into modules:

```
tests/
  test_accounts/
    accounts.py
    bouncer.py
    oauth.py
  test_helpers/
    content_guard.py
    crypto.py
  test_realtime/
    websocket.py
```

**Rules:**
- One module = one directory under `tests/`
- One test file = one area of functionality (not one-test-per-file)
- Files starting with `_` are skipped by the runner
- Files named `__init__.py` or `setup.py` are skipped
- Numeric prefix sorts files: `01_models.py`, `02_api.py` run before `accounts.py`

---

## Test Function Structure

Every test is a plain function:

```python
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

@th.django_unit_test()
def test_something(opts):
    resp = opts.client.post('/api/login', {'username': 'alice', 'password': 'secret'})
    assert_eq(resp.status_code, 200, "expected 200 on valid login")
    assert_true(resp.json.data.access_token, "expected access_token in response")
```

**Rules:**
- Decorator: `@th.django_unit_test()` — always use this for Django tests
- Signature: `def test_xxx(opts):` — always one argument named `opts`
- Name prefix: `test_` — only functions starting with `test_` are collected
- Imports: **import the module under test inside the test function**, not at the top of
  the file. Django may not be configured when the file is first imported.
- No type hints anywhere in test files (project-wide rule)
- Every `assert` must include a descriptive message — bare `assert` is forbidden

---

## The `opts` Object

`opts` is an `objict` (dot-access dict) threaded through every test. It is created once
per test module by the runner before the setup function runs.

| `opts` attribute | Type | Description |
|-----------------|------|-------------|
| `opts.client` | `RestClient` | HTTP client pointed at the running server |
| `opts.host` | `str` | Base URL, e.g. `http://127.0.0.1:5555` |
| `opts.verbose` | `bool` | True if `--verbose` was passed |
| `opts.extra` | `str` | Value from `--extra` flag |
| `opts.extra_list` | `list` | Parsed list from `--extra` |
| `opts.logger` | logger | testit logger instance |

You can attach your own state to `opts` in setup or earlier tests:

```python
@th.django_unit_setup()
def setup_users(opts):
    opts.alice_id = create_test_user('alice@example.com')

@th.django_unit_test()
def test_alice_profile(opts):
    resp = opts.client.get(f'/api/user/{opts.alice_id}')
    assert_eq(resp.status_code, 200, "expected 200")
```

Tests in the same file run in **source order** and share the same `opts` instance, so
state set by earlier tests is visible to later ones.

---

## Setup Functions

Setup functions run once before all tests in the file. Use them for database seeding,
clearing state, and creating shared fixtures.

```python
@th.django_unit_setup()
def setup_bouncer(opts):
    from mojo.apps.account.models import BouncerDevice
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1')
    BouncerDevice.objects.filter(duid__startswith='test-').delete()
```

**Rules:**
- Function name must start with `setup_`
- Decorator: `@th.django_unit_setup()`
- Runs in the same process as the test functions (direct Django ORM access works here)
- Multiple setup functions in one file are all run, in source order

---

## The HTTP Client (`opts.client`)

`opts.client` is a `RestClient` making real HTTP requests to the server over localhost.
It uses `requests.Session` internally, so it behaves like a real browser:

- **Cookies persist** across requests — server-set cookies like `_muid` (device identity),
  `_msid` (session identity), and `mbp` (bouncer pass) are automatically stored and sent
  on every subsequent request, just like a browser cookie jar.
- **Realistic default headers** — sends `User-Agent`, `Accept`, `Accept-Language`, and
  `Accept-Encoding` so server-side signal analysis (bouncer scoring, etc.) produces
  results that match real-world traffic.
- **Cookies survive logout** — `opts.client.logout()` clears auth tokens but keeps
  cookies, just like a browser where device identity persists across re-logins.
- **`opts.client.clear_cookies()`** — clears all cookies to simulate a fresh browser
  with no history (useful for testing `muid_missing` signals, first-visit flows, etc.).

### Making requests

```python
# GET
resp = opts.client.get('/api/user/me')

# POST with JSON body
resp = opts.client.post('/api/login', {'username': 'alice', 'password': 'secret'})

# PUT
resp = opts.client.put('/api/user/42', {'display_name': 'Alice B.'})

# DELETE
resp = opts.client.delete('/api/user/42')
```

### The response object

All methods return an `objict`:

```python
resp.status_code      # int: HTTP status
resp.json             # objict: parsed JSON body (same as resp.response)
resp.response         # objict: parsed JSON body
resp.text             # str: raw text (when body is not JSON)
resp.error_reason     # str: HTTP reason phrase on non-2xx
```

Accessing JSON fields uses dot notation thanks to `objict`:

```python
data = resp.json.data
token = resp.json.data.access_token
```

### Authentication

```python
# Login and store JWT — subsequent requests automatically include Authorization header
opts.client.login('alice@example.com', 'password123')
assert_true(opts.client.is_authenticated, "login failed")

# JWT data is parsed and available
uid = opts.client.jwt_data.uid

# Log out (clears token and Authorization header)
opts.client.logout()
```

### Passing extra headers

```python
resp = opts.client.get('/api/resource', headers={'X-Custom': 'value'})
```

---

## Assert Helpers

Always use the testit helpers instead of bare `assert`. They produce clear failure messages.

```python
from testit.helpers import assert_true, assert_eq, assert_in

# Boolean check
assert_true(value, "descriptive message")

# Equality check — shows expected vs actual on failure
assert_eq(actual, expected, "descriptive message")

# Membership check
assert_in(item, container, "descriptive message")

# Expect exception
with th.assert_raises(ValueError):
    some_code_that_raises()
```

**Never write** `assert condition` — always pass a message.

---

## Process Isolation — The Most Important Section

`opts.client` talks to a **separate uvicorn process**. The test process and the server
process do not share memory. This has three major consequences:

### 1. `mock.patch` does NOT affect the server

```python
# WRONG — patching in the test process; server never sees it
from unittest.mock import patch
with patch('mojo.helpers.dates.utcnow', return_value=future_time):
    resp = opts.client.post('/api/something', {...})  # server runs un-patched code

# RIGHT — call the function directly in the test process where the patch applies
with patch('mojo.helpers.dates.utcnow', return_value=future_time):
    from mojo.apps.account.services import tokens
    result = tokens.verify_token(tok)  # runs in test process, patch works
```

### 2. `override_settings()` does NOT affect the server

```python
# WRONG — override_settings only patches the test process Django; server is unaffected
from django.test import override_settings
with override_settings(BOUNCER_REQUIRE_TOKEN=True):
    resp = opts.client.post('/api/login', {...})  # server still uses original settings

# RIGHT — use th.server_settings() to write to var/django.conf and reload the server
with th.server_settings(BOUNCER_REQUIRE_TOKEN=True):
    resp = opts.client.post('/api/login', {...})  # server has reloaded with new setting
```

### 3. In-process DB changes are invisible to the server

The test process and server share the same PostgreSQL database, so ORM writes in setup
functions **are** visible to the server (committed immediately). But if you modify
in-memory state or module globals in the test process, the server doesn't see it.

---

## `th.server_settings(**overrides)` — Live Server Settings Override

For tests that require the running server to have a different Django setting:

```python
with th.server_settings(BOUNCER_REQUIRE_TOKEN=True):
    resp = opts.client.post('/api/login', {'username': 'x', 'password': 'y'})
    assert_eq(resp.status_code, 403, "expected 403 when token enforcement is on")
```

**What it does:**
1. Writes overrides into `testproject/var/django.conf` (merging with existing values)
2. Waits ~1.5s for uvicorn's file watcher to detect the change
3. Polls until the server comes back up (up to 10s)
4. Yields — your test runs against the live reloaded server
5. Restores the original `django.conf`
6. Waits for the server to reload again

**Why this works:** `asgi_local` starts uvicorn with `--reload --reload-include '*.conf'`,
so any change to `var/django.conf` triggers a full Django reload.

**Cost:** Each `server_settings` call takes ~3–5 seconds (two reload cycles). Use it
only when genuinely necessary — for most tests the default settings are correct.

**Supported value types:**
```python
th.server_settings(
    BOUNCER_REQUIRE_TOKEN=True,          # bool
    BOUNCER_TOKEN_TTL=300,               # int
    BOUNCER_API_BASE='https://x.com',    # str
)
```

---

## Direct Service Testing (Bypassing HTTP)

For logic that is hard or slow to test over HTTP (time-sensitive behavior, error paths,
internal service APIs), call the Django service directly inside a `@th.django_unit_test`.
Django is already configured in the test process.

```python
@th.django_unit_test()
def test_token_expired(opts):
    import time, hmac, hashlib, json
    from mojo.apps.account.services.bouncer.token_manager import (
        TokenManager, _b64url_encode, _get_signing_key,
    )
    # Craft an expired token directly — no server needed
    payload = {
        'duid': 'test-duid',
        'ip': '127.0.0.1',
        'expires_at': int(time.time()) - 100,
        'nonce': 'expired-nonce',
    }
    p64 = _b64url_encode(json.dumps(payload, separators=(',', ':')))
    sig = hmac.new(_get_signing_key(), p64.encode(), hashlib.sha256).digest()
    token = f"{p64}.{_b64url_encode(sig)}"

    try:
        TokenManager.validate(token, request_ip='127.0.0.1')
        assert_true(False, "expected ValueError for expired token")
    except ValueError as exc:
        assert_eq(str(exc), 'expired', f"expected 'expired', got {exc}")
```

This pattern is the right choice whenever:
- The behavior depends on mocking (time, external calls)
- The test is about a service method's return value or exception, not an HTTP contract
- The setup would be prohibitively complex over HTTP

---

## Conditional Tests with `@th.requires_extra`

Skip tests unless a specific `--extra` flag is provided:

```python
@th.django_unit_test()
@th.requires_extra('stripe')
def test_stripe_webhook(opts):
    # Only runs when: bin/run_tests --extra stripe
    ...
```

List all declared flags without running:
```bash
bin/run_tests --list-extras
```

---

## Running Tests

```bash
# bin/run_tests starts and stops asgi_local automatically.
# No need to run bin/asgi_local manually.

# Run all tests
bin/run_tests

# Run a specific module
bin/run_tests -t test_accounts

# Run a specific file within a module
bin/run_tests -t test_accounts.bouncer

# Stop on first failure and save checkpoint
bin/run_tests -s

# Resume from checkpoint after fixing the failure
bin/run_tests --continue

# Verbose output (show tracebacks)
bin/run_tests -v

# Pass extra flags to conditional tests
bin/run_tests --extra stripe,webhooks

# Quick mode — only runs quick_ prefixed functions
bin/run_tests -q
```

---

## WebSocket Testing

```python
from testit.ws_client import WsClient

@th.django_unit_test()
def test_realtime_subscription(opts):
    opts.client.login('alice@example.com', 'password123')

    ws_url = WsClient.build_url_from_host(opts.host, path='ws/realtime/')
    with WsClient(ws_url) as ws:
        auth = ws.authenticate(opts.client.access_token)
        ws.subscribe(f"user:{auth['instance_id']}")
        msg = ws.wait_for_type('subscribed', timeout=5)
        assert_eq(msg.data['type'], 'subscribed', "expected subscribed confirmation")
```

`WsClient` is a context manager — `__enter__` calls `connect()`, `__exit__` calls
`close()`. Use `wait_for_type(type, timeout)` instead of raw polling.

---

## Generating Test Data

```python
from testit import faker

person = faker.generate_person()
# {'first_name': 'Alice', 'last_name': 'Smith', 'dob': date(...), ...}

name = faker.generate_name()   # e.g. 'Streamlined radical hierarchy'
text = faker.generate_text()   # Lorem-like paragraph
```

---

## Common Patterns

### Creating and cleaning up test records

```python
TEST_EMAIL = 'bouncer-test@example.com'

@th.django_unit_setup()
def setup_user(opts):
    from mojo.apps.account.models import User
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password='TestPass1!'
    )

@th.django_unit_test()
def test_login(opts):
    resp = opts.client.post('/api/login', {'username': TEST_EMAIL, 'password': 'TestPass1!'})
    assert_eq(resp.status_code, 200, "expected 200 on valid login")
```

### Chaining state across tests

Tests run in source order and share `opts`:

```python
@th.django_unit_test()
def test_create_resource(opts):
    resp = opts.client.post('/api/items', {'name': 'test item'})
    assert_eq(resp.status_code, 201, "expected 201")
    opts.item_id = resp.json.data.id   # store for next test

@th.django_unit_test()
def test_read_resource(opts):
    resp = opts.client.get(f'/api/items/{opts.item_id}')
    assert_eq(resp.status_code, 200, "expected 200")
```

### Testing 4xx responses

```python
@th.django_unit_test()
def test_unauthorized(opts):
    opts.client.logout()
    resp = opts.client.get('/api/user/me')
    assert_eq(resp.status_code, 403, "expected 403 when not authenticated")
    assert_true(resp.json.is_authenticated is False, "expected is_authenticated=False")
```

### Clearing rate limits between tests

```python
@th.django_unit_setup()
def setup(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1')
```

---

## What NOT to Do

| Wrong | Right |
|-------|-------|
| `assert condition` | `assert_true(condition, "message")` |
| `from django.test import override_settings` in HTTP tests | `with th.server_settings(KEY=val):` |
| `mock.patch(...)` wrapping `opts.client` calls | Call service directly in test process |
| Import module under test at file top level | Import inside each test function |
| Add type hints | No type hints anywhere |
| Write tests that assert the bug is still present | Tests must fail while broken, pass when fixed |
| Rely on test execution order across files | Each file is independent; use setup functions |
| Use `th.django_unit_test` without parentheses | Always `@th.django_unit_test()` |

---

## Test Quality Checklist

Before submitting any test, verify:

- [ ] Every `assert` has a descriptive message
- [ ] Module under test is imported inside the test function, not at file top
- [ ] No `mock.patch` wrapped around `opts.client` HTTP calls
- [ ] No `override_settings` wrapped around `opts.client` HTTP calls
- [ ] Settings-dependent server behavior uses `th.server_settings()`
- [ ] Setup function cleans up before creating — no assumption about prior state
- [ ] Tests are independent enough that removing any one test does not break others
- [ ] Tests fail when the feature is broken and pass when it is correct
- [ ] No type hints
