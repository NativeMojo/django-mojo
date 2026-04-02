# TestIt Framework

TestIt is the Django-MOJO test harness for API, unit, and integration coverage.  
It is intentionally lightweight so both humans and LLM agents can reason about a suite, spot design issues early, and iterate fast.

---

## Core Ideas
- Keep the filesystem predictable: numbered filenames (`1_test_models.py`, `3_test_flows.py`) control execution order because TestIt sorts alphabetically.
- Prefer reusing state instead of recreating fixtures. Store shared objects on `opts` during setup and tear them down only when reuse is impossible.
- Tests are documentation. If an API feels awkward, pause and call it out rather than embedding new logic in the test.
- Expensive or destructive flows must opt-in via `--extra` or the `@requires_extra` decorator.
- Name test packages `test_<domain>` (for example `test_auth`, `test_user_mgmt`) to avoid module collisions with real Django apps. Splitting a large app across multiple focused packages is encouraged — each package runs in parallel independently.

---

## Project Layout

```
tests/
  test_auth/         # login, magic login, secrets, permissions  (parallel)
    __init__.py
    accounts.py
    magic_login.py
    secrets.py
  test_mfa/          # TOTP, passkeys, verification              (parallel)
    __init__.py
    totp.py
    passkeys.py
  test_oauth/        # OAuth flows — calls server_settings()     (serial)
    __init__.py
    oauth.py
    oauth_apple.py
  test_security/     # bouncer, device tracking, PII   (serial, opt-in: --full)
    __init__.py
    bouncer.py
    device_tracking.py
  test_user_mgmt/    # invite, deactivation, API keys            (parallel)
    __init__.py
    invite_flow.py
    deactivation.py
docs/
  testit/
    examples/
      1_test_models.py
      3_test_flows.py
      testit.config.json
```

- Sorting within a package comes from filenames. Use `1_`, `2_` prefixes when execution order inside a package matters.
- **Do not name test packages identically to the Django app.** Use `tests/test_auth/` instead of `tests/auth/` so imports never collide when the runner appends the folder to `sys.path`.
- Split large app test suites into domain-focused packages. Each package runs in parallel independently, so smaller packages reduce total wall-clock time.
- Each file keeps decorators at the top, followed by related tests in definition order.
- Example files live in `docs/testit/examples/` for quick copy/paste or prompting.

---

## Running TestIt

Use `bin/run_tests` — it handles starting and stopping the test server automatically:

- Run everything:
  `./bin/run_tests`
- Target a module or a specific file:
  `./bin/run_tests -t test_auth`
  `./bin/run_tests -t test_auth.accounts`
- Multiple modules:
  `./bin/run_tests -t test_auth.accounts -t test_billing.3_test_flows`
- Verbose output and stop on first failure:
  `./bin/run_tests -v -s`
- Resume from the last failed test file (skips DB flush, picks up where `-s` stopped):
  `./bin/run_tests -s --continue`
- Show tracebacks without verbosity:
  `./bin/run_tests -e`
- Toggle app scopes:
  `./bin/run_tests --onlymojo` · `./bin/run_tests --nomojo`
- See every declared `@requires_extra` flag (static scan only, no tests executed):
  `./bin/run_tests --list-extras`
- Run modules in parallel (default 4 threads):
  `./bin/run_tests -j 6`
- Include opt-in modules (slow/pre-publish tests):
  `./bin/run_tests --full`
- Force plain text output and disable the rich progress UI:
  `./bin/run_tests --plain`
- Write structured test report for LLM agents:
  `./bin/run_tests --agent`

All arguments are passed directly to `bin/testit.py`. If the server is already running, `bin/run_tests` will not stop it after the suite completes.

### Parallel Execution

By default the runner executes up to 3 modules in parallel using `ThreadPoolExecutor`. Each parallel module gets its own `RestClient` instance. Parallelism is automatically forced to 1 when `-s` (stop on fail), `-v` (verbose), or `--continue` (resume) is active — those modes require sequential output.

Set a specific thread count with `-j N`:

```bash
./bin/run_tests -j 1   # fully sequential (same as --plain behaviour)
./bin/run_tests -j 6   # run up to 6 modules at once
```

Modules marked `serial` in their `TESTIT` config always run sequentially after all parallel modules complete, regardless of `-j`.

### Rich Progress UI

When `rich` is installed and `-j` is greater than 1, the runner shows a live per-module progress table. Use `--plain` to disable it (useful in CI environments that do not handle ANSI codes, or when piping output):

```bash
./bin/run_tests --plain
```

### Agent Mode

`--agent` writes `var/test_failures.json` after the run — a structured JSON report designed for LLM agents and CI pipelines. The report includes:

- **Top-level**: `status` (passed/failed), `total`, `passed`, `failed`, `skipped`, `duration`
- **`modules`**: per-module breakdown — `tests`, `passed`, `failed`, `skipped`, `duration` for each test module
- **`failures`**: per-failure entries with `test_name`, `function`, `status`, `assertion`, `test_source`, `file_path`, `line`, `traceback` (errors only), and `server_log_tail`

LLM agents should always use `--agent` and read the JSON report instead of parsing terminal output. Never use `--plain` for full suite runs — it disables the rich UI but doesn't improve agent output.

### Opt-in Modules

Modules with `"requires_extra": ["slow"]` in their TESTIT config are skipped by default. Include them with `--full` (shortcut for `--extra slow`):

```bash
./bin/run_tests --full          # include all opt-in modules
./bin/run_tests --extra slow    # equivalent
```

Currently opt-in:

| Module | Reason |
|---|---|
| `test_security` | Bouncer/rate-limiting tests (~20s, serial) |

To make a module opt-in, add `"requires_extra": ["slow"]` to its `__init__.py` TESTIT config.

### JSON Config
CLI flags always win, but you can seed defaults through a JSON file:

```bash
./bin/testit.py --config docs/testit/examples/testit.config.json --extra run-backfill
```

Supported keys:

```json
{
  "tests": ["test_auth", "test_helpers.cron"],
  "ignore": ["test_aws"],
  "stop_on_fail": true,
  "show_errors": true,
  "verbose": true,
  "nomojo": true,
  "module": "test_auth",
  "extra": "run-backfill,cleanup"
}
```

- `tests` and `ignore` accept strings or lists.
- `show_errors` is equivalent to `-e`.
- `extra` accepts either a comma-separated string or a JSON list; at runtime it is exposed as `opts.extra_list` (and `opts.extra` remains a comma-joined string for legacy helpers).
- Supply fewer flags in automation scripts; let interactive runs override what is needed.

---

## TESTIT Module Config

Each test package can declare a `TESTIT` dict in its `__init__.py` to control how the runner handles it. The runner reads the file via AST — the module is never imported during config loading, so there are no side effects.

```python
# tests/test_auth/__init__.py  — parallel module (default)
TESTIT = {
    "requires_apps": ["mojo.apps.account"],  # skip if app is not installed
}

# tests/test_oauth/__init__.py  — serial because oauth.py calls th.server_settings()
TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    "serial": True,                          # do not run this module in parallel
    "server_settings": {},                   # dict of Django settings to apply before the module starts
}

# tests/test_security/__init__.py  — opt-in slow module
TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    "serial": True,
    "requires_extra": ["slow"],              # skipped unless --full or --extra slow
}
```

When a large app has many tests, split it into domain-focused packages (`test_auth`, `test_mfa`, `test_user_mgmt`, etc.) rather than one monolithic `test_accounts`. Each package runs in parallel by default; only packages that call `th.server_settings()` mid-run need `"serial": True`.

Supported keys:

| Key | Default | Description |
|---|---|---|
| `serial` | `False` | Force this module to run sequentially, after all parallel modules complete. Use for modules that call `th.server_settings()` mid-run, or that rely on signals bound to the main thread. |
| `requires_apps` | `[]` | List of Django app labels. The module is skipped entirely if any listed app is not in `INSTALLED_APPS`. |
| `server_settings` | `{}` | Django settings dict applied before the module starts (same mechanism as `th.server_settings()`). |
| `requires_extra` | `[]` | List of `--extra` flags. The module is skipped unless at least one flag is present. Use `["slow"]` for opt-in modules included by `--full`. |

All keys are optional. A missing `__init__.py` or a missing `TESTIT` assignment uses defaults (parallel, no app requirements, no server settings).

---

## Decorators & Shared State

```python
from testit import helpers as th

@th.unit_setup()
def setup_shared(opts):
    """Runs once before every test in this file (no Django ORM)."""
    opts.base_payload = {"name": "Acme Co"}
    opts.expected_slug = "acme-co"

@th.django_unit_setup()
def setup_django_records(opts):
    """Runs with Django configured; keep imports inside the function."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    opts.admin = User.objects.create_superuser("admin", "admin@example.com", "secret")

@th.unit_test("slug is normalized")
def test_slugify(opts):
    result = slugify(opts.base_payload["name"])
    assert result == opts.expected_slug, "slugify should normalize company names"

@th.django_unit_test()
def test_admin_can_login(opts):
    response = opts.client.post("/api/login", json={"username": "admin", "password": "secret"})
    assert response.status_code == 200, "admin login must succeed"
```

- `opts` persists for the module. Store objects, IDs, and flags for reuse.
- Import Django models inside `@django_unit_setup` / `@django_unit_test` only.
- Tests run in definition order; keep related assertions grouped.

See `docs/testit/examples/1_test_models.py` for a full reference module.

---

## Gating Expensive or Destructive Tests

Use `--extra` for operator intent and `@requires_extra` for explicit guards:

```python
from testit import helpers as th

@th.requires_extra("run-backfill")
@th.django_unit_test()
def test_backfill_job(opts):
    """Do not enqueue expensive work unless --extra run-backfill is present."""
    response = opts.client.post("/api/jobs/backfill", json={"account_id": opts.account_id})
    assert response.status_code == 202, "Backfill should enqueue when requested"
```

- Without the matching flag the test is logged as `SKIPPED` and not counted.
- Pass multiple extras via comma-separated values (`--extra run-backfill,notify`).
- For tests that just need *any* extra, call `@th.requires_extra()` with no flag.
- Use `opts.extra_list` when you need to iterate over extras; it is always a list even if the value came from CLI. Example flow: `docs/testit/examples/3_test_flows.py`.
- Discover tags up front with `./bin/testit.py --list-extras` before deciding which extras to pass.

---

## Expectations for Every Test

- **Every assert must include a failure message.** No bare `assert x` — always `assert x, "reason"`. The message must state what was expected, what the inputs were, or why the assertion matters. Silent failures waste debugging time for both humans and agents.

  ```python
  # Bad — silent on failure
  assert resp.status_code == 200
  assert isinstance(data, list)

  # Good — tells you exactly what went wrong
  assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.json}"
  assert isinstance(data, list), f"Expected list, got {type(data).__name__}: {data!r}"
  ```

- **No `print()` debugging.** Use `-v` or `-e` for deeper logs.
- **Stay inside the framework.** If behaviour is missing, file a TODO or note it in review instead of patching logic into the test.
- **Call out design friction.** Tests should highlight confusing APIs; they should not cement workarounds.
- **Reuse fixtures.** Prefer creating entities in setup once, mutate through `opts`, and tear down only when required for repeat runs.

---

## Accessing Response Data — objict Key Collision

Testit parses all HTTP responses into `objict`, a `dict` subclass with attribute access. Because `objict` inherits from `dict`, **attribute access for keys that share a name with a built-in dict method will silently return the method instead of the value.**

Affected names: `values`, `keys`, `items`, `get`, `update`, `pop`, `clear`, `copy`, `setdefault`.

```python
# WRONG — data["values"] is [1, 2, 3] but data.values is dict.values (a method)
assert isinstance(resp.response.data.values, list)   # always False — dict method, not your key

# CORRECT — use bracket notation for any key that shadows a dict built-in
assert isinstance(resp.response.data["values"], list), \
    f"Expected list, got {type(resp.response.data['values']).__name__}"
```

**Rule:** for any response key named `values`, `keys`, `items`, `get`, or `update`, always use `obj["key"]` bracket access, never `obj.key` dot access.

All other keys (e.g. `periods`, `slug`, `status`, `data`, `id`) are safe to access with dot notation.

---

## Frequent Mistakes (LLM Watchlist)

- Forgetting alphabetical filenames (`1_`, `2_`, `3_`) and losing ordering guarantees.
- Naming the test package the same as the Django app and shadowing real modules (prefix with `test_`).
- Importing Django models at the module top or inside `@unit_*` functions.
- Creating fresh users/records for every assertion instead of reusing shared state.
- **Writing bare `assert x` with no failure message** — always include a descriptive string.
- **Using `obj.values`, `obj.keys`, or `obj.items` on an `objict`** — returns the dict built-in method, not your key. Use `obj["values"]` instead.
- Skipping the `--extra` gate on expensive tasks (cron jobs, third-party calls).
- Writing custom business logic in tests instead of exercising the real APIs.
- **Calling job functions directly with a plain dict** — job functions receive a `Job` instance (`func(job)`), not a dict. Call `jobs.publish(...)` then `th.run_pending_jobs()` to exercise the real path and catch signature mismatches.

---

## Outputs & Tooling

- A structured run report is written to `var/test_results.json`:
  - `total`, `passed`, `failed`
  - `records[]` with module, file, function, status (`passed`, `failed`, `error`, `skipped`)
  - timestamps (`started_at`, `finished_at`, `duration`)
- Agent report (written when `--agent` is passed): `var/test_failures.json`
  - Top-level `status`, `total`, `passed`, `failed`, `skipped`, `duration`
  - Per-module stats in `modules` dict (tests, passed, failed, skipped, duration)
  - Per-failure diagnostics in `failures` list (test_source, file_path, line, traceback, server_log_tail)
- HTTP helper: `testit.client.RestClient`
  - Reuses auth tokens and integrates with `opts.client`.
  - `opts.client.last_response` — after every request this is set to an `objict` with `method`, `path`, `status_code`, `body`, `headers`, and `elapsed_ms`. Useful for diagnosing failures without re-running the request.
- WebSocket helper: `testit.ws_client.WsClient`
  - Build URL from the HTTP host and wait for typed messages.
- Faker snippets: `testit.faker`
  - Shared generator for generating deterministic-looking fixtures.

---

## Testing Async Jobs — `th.run_pending_jobs()`

```python
count = th.run_pending_jobs(channel=None, status="pending")
```

Executes pending jobs from the database using the same calling convention as the production job engine — `func(job)` where `job` is a `Job` model instance. No Redis or running engine process is required.

**Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `channel` | `None` | Filter to jobs on a specific channel. Omit to run all pending jobs. |
| `status` | `"pending"` | Job status to filter on. |

**Behavior:**
- Queries `Job.objects.filter(status=status)` ordered by `created`
- For each job: resolves the function via `load_job_function(job.func)`, calls `func(job)`
- Marks each job `completed` on success, `failed` on exception
- Returns the count of jobs executed

**Why use this instead of calling job functions directly:**

Job functions receive a `Job` model instance, not a plain dict. Calling a job function directly with a dict bypasses that calling convention and will not catch signature mismatches. Using `th.run_pending_jobs()` exercises the full pipeline — publish, DB row, function dispatch — exactly as production does.

```python
@th.django_unit_test()
def test_handler_fires(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    # Clean up any leftover jobs from previous runs
    Job.objects.filter(channel="default").delete()

    # Publish the job the same way production code does
    jobs.publish(
        "myapp.tasks.send_notification",
        {"user_id": opts.user.pk, "message": "hello"},
        channel="default",
    )

    # Run pending jobs using the real engine calling convention
    executed = th.run_pending_jobs(channel="default")
    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    # Assert side effects here
```

**Setup tip:** delete relevant jobs at the top of your test to prevent leftover rows from previous runs from inflating counts or interfering with assertions.

---

## Prompting / Pairing Checklist

1. Identify the module and create or update numbered files.
2. Draft setups first. Ensure Django imports stay inside decorated functions.
3. Reuse `opts` data; avoid redundant inserts.
4. For high-cost paths, add `@requires_extra("...")` and document the flag.
5. **Every `assert` must include a descriptive failure message string.** No bare asserts.
6. **Use `obj["values"]` not `obj.values` for any key that shadows a dict built-in.**
7. **For async job flows**, call `jobs.publish(...)` then `th.run_pending_jobs()` — never call job functions directly with a plain dict.
8. Before implementing workarounds, question the upstream API — document friction for follow-up.
9. Run with `./bin/run_tests -v -e` (or via config) when validating locally.

Keeping these habits makes the suite predictable for both humans and models, highlights design gaps early, and keeps TestIt simple.
