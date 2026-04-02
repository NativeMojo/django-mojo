# Test Runner Modernization

**Type**: request
**Status**: planned
**Date**: 2026-04-02
**Priority**: high

## Description

Modernize the testit runner with parallel module execution, a rich CLI interface, structured LLM agent output, and per-module configuration. Currently 1,118 tests run sequentially in ~94 seconds. The goal is faster runs, better developer UX, and machine-readable failure reports so Claude Code can diagnose and fix failures without scraping terminal output.

## Context

The current runner (`testit/runner.py`) is single-threaded with line-by-line ANSI output. Tests are organized into 16 module directories (e.g., `test_accounts`, `test_incident`). Modules are independent — they create their own test data in setup and don't share state across module boundaries. Within a module, test files may have numeric prefix ordering (`1_test_crypto.py` → `2_test_service.py`) requiring sequential execution.

### Current bottlenecks
- **Sequential execution**: modules run one at a time despite being independent
- **Rate limit interference**: 272 login calls across the suite can exhaust `ip_limit=100/60s` (separate fix in `testit/client.py`)
- **`server_settings()` is a global mutation**: rewrites `var/django.conf` and reloads uvicorn — blocks parallel execution for modules that use it
- **No structured failure output**: LLM agents parse ANSI terminal text to diagnose failures

### Key files
- `testit/runner.py` — test discovery, ordering, execution loop (645 lines)
- `testit/helpers.py` — decorators, state, assertions, `server_settings()` (635 lines)
- `testit/client.py` — REST client with login/logout (187 lines)
- `bin/run_tests` — wrapper script, server lifecycle
- `bin/testit.py` — bootstrap, DB flush, Django setup

### Test distribution (top modules by time)
- `test_accounts`: ~75s total (469 tests) — largest module by far
- `test_incident`: ~20s (135 tests)
- `test_filevault`: ~9s (44 tests)
- `test_shortlink`: ~12s (38 tests)
- `test_helpers`: ~5s (162 tests, mostly fast unit tests)

### Modules using `server_settings()` mid-test
These mutate the shared server config and must run serially or in isolation:
- `test_accounts/bouncer.py`
- `test_accounts/email_change.py` (ALLOW_EMAIL_CHANGE)
- `test_accounts/phone_change.py` (ALLOW_PHONE_CHANGE)
- `test_accounts/verification.py`

## Plan

**Status**: planned
**Planned**: 2026-04-02

### Objective

Add parallel module execution, a compact rich progress UI, structured `--agent` output, and optional per-module `TESTIT` configuration to the testit runner.

### Phase 1: Module Config + Agent Output + Rich UI

#### Step 1: `TESTIT` module config in `__init__.py`

Each test module directory can optionally define a `TESTIT` dict in its `__init__.py`:

```python
# tests/test_accounts/__init__.py
TESTIT = {
    "server_settings": {"ALLOW_PHONE_CHANGE": True},
    "serial": True,           # must run alone (uses server_settings mid-test)
    "requires_apps": ["mojo.apps.account"],
    "requires_extra": ["aws"],
}
```

All fields are optional. Defaults:
- `server_settings`: `{}` (no overrides)
- `serial`: `False` (safe to run in parallel)
- `requires_apps`: `[]` (no app requirements)
- `requires_extra`: `[]` (no extra flags needed)

The runner reads `TESTIT` config from each module's `__init__.py` at discovery time (import or AST scan). Modules without `TESTIT` use defaults.

**Files**:
1. `testit/runner.py` — add `_load_module_config(module_path)` that reads `TESTIT` from `__init__.py`
2. `tests/test_accounts/__init__.py` — add `TESTIT` with `serial: True`
3. `tests/test_*/__init__.py` — add `TESTIT` to modules with known requirements (apps, extras)

#### Step 2: Structured agent output (`--agent`)

Add `--agent` flag that writes structured failure data to `var/test_failures.json` (stdout stays clean for the progress UI).

Per-failure record:
```json
{
  "test_name": "phone/change/confirm: race — number claimed by another account is rejected",
  "module": "test_accounts",
  "test_file": "phone_change",
  "function": "test_confirm_race_number_claimed",
  "file_path": "tests/test_accounts/phone_change.py",
  "line": 372,
  "status": "failed",
  "assertion": "Claimed number must be rejected, got 403",
  "test_source": "<full function body>",
  "setup_source": "<setup function body if exists>",
  "response": {"status_code": 403, "body": {"error": "..."}},
  "server_log_tail": "<last 20 lines of error.log around failure timestamp>"
}
```

This gives the LLM everything it needs to diagnose without reading 5 files first.

**Files**:
1. `testit/runner.py` — add `--agent` flag; on failure, collect structured context
2. `testit/helpers.py` — capture last HTTP response on `opts.client`; extract test source via `inspect.getsource()`; tail server error log
3. `testit/client.py` — stash `self.last_response` on every request (status, body, headers)

#### Step 3: Rich progress UI

Replace line-by-line ANSI output with a `rich` live display:

**Normal mode** (default):
```
 test_accounts  ━━━━━━━━━━━━━━━━━━━╸     467/469  ✓ 465  ✗ 2  ⊘ 0   12.3s
 test_incident  ━━━━━━━━━━━━━━━━━━━━━━━━  135/135  ✓ 135  ✗ 0  ⊘ 0    4.1s  ✔
 test_helpers   ━━━━━━━━━━━━━━━╸          120/162  ✓ 120  ✗ 0  ⊘ 0    2.8s
 test_jobs      ━━━╸                       12/58   ✓ 12   ✗ 0  ⊘ 0    0.4s
 test_filevault (queued)
```

After completion: print a summary table with pass/fail/skip counts per module, total time, and any failure details expanded.

**Verbose mode** (`-v`): falls back to current line-by-line output (no rich panel) for debugging.

**Files**:
1. `testit/runner.py` — replace print calls with `rich.progress` / `rich.live` panel
2. `testit/helpers.py` — route `_run_unit` output through a display abstraction (rich panel vs plain text)
3. `pyproject.toml` — add `rich` dependency

### Phase 2: Parallel Execution

#### Step 4: Parallel module runner

Add `-j N` flag (default 3, or 1 if `-s`/`-v`/`--agent` with stop-on-fail):

```
bin/run_tests -j 4          # 4 modules at a time
bin/run_tests -j 1          # sequential (same as today)
bin/run_tests -s            # stop-on-fail forces -j 1
```

Execution strategy:
1. **Discovery phase**: scan all modules, load `TESTIT` configs
2. **Group by `server_settings`**: modules with identical `server_settings` dicts can run in the same batch
3. **Schedule**:
   - Group 1 (default settings, `serial: False`): run up to `-j N` modules concurrently via `ThreadPoolExecutor`
   - Group 2 (custom settings A): apply settings, run that group's modules (parallel if none are `serial`)
   - Serial modules: run alone in their own slot
4. **Each thread gets**: its own `opts` copy, its own `RestClient`, its own result accumulator
5. **Merge**: after all threads complete, merge results into `TEST_RUN` totals

Thread safety:
- `TEST_RUN` counters protected by a `threading.Lock`
- Each module accumulates `records` into a thread-local list, merged at the end
- `rich` progress panel updated from the main thread via a shared queue

**Files**:
1. `testit/runner.py` — `ThreadPoolExecutor`, module grouping, `-j` flag, thread-safe result merging
2. `testit/helpers.py` — thread-safe `TEST_RUN` (lock around counter increments), per-thread result lists
3. `testit/client.py` — no changes needed (each thread creates its own `RestClient`)

#### Step 5: Response capture for agent diagnostics

Enhance `RestClient` to always capture the last response:

```python
def _make_request(self, method, path, **kwargs):
    ...
    self.last_response = objict(
        method=method,
        path=path,
        status_code=response.status_code,
        body=response_data,
        headers=dict(response.headers),
        elapsed_ms=response.elapsed.total_seconds() * 1000,
    )
```

On test failure in `--agent` mode, the runner reads `opts.client.last_response` and includes it in the failure record.

**Files**:
1. `testit/client.py` — stash `self.last_response` in `_make_request`

### Design Decisions

- **Threads, not processes**: modules share the same DB connection pool and Django setup. Threads are simpler and avoid the overhead of re-bootstrapping Django per process.
- **`-j 1` preserves backward compat**: no behavior change for users who don't opt in. Stop-on-fail and verbose mode auto-force sequential.
- **`TESTIT` config is optional**: modules without it get safe defaults (`serial: False`, no special settings). Existing tests work unchanged.
- **Agent output to file, not stdout**: keeps the rich progress UI clean. The LLM reads `var/test_failures.json` after the run.
- **Server settings grouping**: instead of applying/reverting settings per module, batch modules with the same settings together. Minimizes server reloads (each reload costs ~3-5s).
- **`rich` for UI**: standard library for terminal progress. No point hand-rolling ANSI escape codes.
- **Phase split**: Phase 1 (config + agent + UI) is independently valuable even without parallelism. Phase 2 (parallel) builds on the config infrastructure from Phase 1.

### Edge Cases

- **`server_settings()` mid-test**: modules that call `server_settings()` inside individual tests (not just at module level) must be marked `serial: True`. The runner warns if a non-serial module is detected using `server_settings()` at runtime.
- **Database collisions**: modules must use unique test usernames/data. The runner does not provide per-module DB isolation. If two parallel modules create user "testuser", they collide. Convention: prefix test data with the module name.
- **Rate limits**: `client.login()` already clears login rate limits (separate fix). Parallel modules making concurrent logins to the same server won't interfere because each clear is atomic in Redis.
- **Port contention**: all modules hit the same server on the same port. HTTP is stateless so this is fine, but throughput is bounded by the server's worker count. With 3 concurrent modules, the default uvicorn worker should handle it.
- **`--continue` (resume)**: incompatible with parallel mode. If `-j > 1` and `--continue`, force `-j 1` and warn.
- **Rich not installed**: fall back to plain text output (current behavior). Don't hard-crash on missing dependency.

### Testing

- `tests/test_helpers/test_runner_config.py` — `TESTIT` config loading: default values, partial config, missing `__init__.py`
- `tests/test_helpers/test_runner_agent.py` — agent output format: verify `var/test_failures.json` structure after a deliberate failure
- Manual validation: run `bin/run_tests -j 3` on full suite, compare results to `-j 1`

### Docs

- `docs/django_developer/testit/Overview.md` — add sections for `TESTIT` config, `--agent` flag, `-j` parallel flag, rich UI
- `CHANGELOG.md` — test runner modernization entry
