# TestIt Framework

TestIt is the Django-MOJO test harness for API, unit, and integration coverage.  
It is intentionally lightweight so both humans and LLM agents can reason about a suite, spot design issues early, and iterate fast.

---


> Each checkout gets its own database, Redis index and dev-server
> port, derived from its path — see [Isolation.md](Isolation.md).

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
  test_oauth/        # OAuth flows — calls server_settings()   (parallel)
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

### Test log lifecycle

After it acquires the run lock, every fresh executing run starts with empty
framework log files under `testproject/var/logs/`. Base `*.log` files are
truncated in place, which keeps open server file descriptors valid, and stale
numbered `*.log.N` rotation backups are removed. A failure to clear one file is
reported as a warning and does not abort the test run. Symlinked, multiply
linked, and other non-regular base-log paths are refused, and file identity is
rechecked before truncation to detect a path changed during the reset.

`--continue` preserves the logs because it resumes the same logical run.
`--list-extras` also leaves them untouched because it executes no tests. The
uvicorn process log at `testproject/var/asgi.log` is outside the framework log
directory and is not part of this reset.

### Dev-server host/port (`dev_server.conf`)

The test server's host and port come from a small `key=value` file with two keys:

```
host=127.0.0.1
port=5555
```

Resolution prefers a local override: if `var/dev_server.conf` exists it is used and `config/dev_server.conf` is ignored entirely (whole-file override — a key the var file omits falls to the built-in default, **not** config's value); otherwise the committed `config/dev_server.conf` is used. `var/` is gitignored, so `var/dev_server.conf` lets you point the test server at a different host/port on your machine without editing the tracked config. All three readers honor this order: `bin/asgi_local` (which binds uvicorn), the runner's `--host` default, and `th.server_settings()`.

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

`--agent` writes `testproject/var/test_failures.json` after the run — a structured JSON report designed for LLM agents and CI pipelines. The report includes:

- **Top-level**: `status` (passed/failed), `total`, `passed`, `failed`, `skipped`, `duration` — summed across every module, **including modules skipped whole** (`requires_apps`/`requires_extra`). This is the baseline-comparison number.
- **`ran`**: the same shape (`total`, `passed`, `failed`, `skipped`) but restricted to what this invocation actually *executed* — whole-skipped modules contribute to the top-level totals but not to `ran`. Use `ran` when you need "how much work did this run really do," e.g. distinguishing a default-tier run from one that also pulled in `--extra` modules.
- **`modules`**: per-module breakdown — `tests`, `passed`, `failed`, `skipped`, `duration` for each test module, plus `skipped_reason` when the whole module was skipped
- **`slowest`**: the 25 slowest individual tests, sorted descending by `duration` — `test_name`, `module`, `test_file`, `status`, `duration`. Module-level `duration` alone can't say which test inside a slow module is the cost; every executed test records its own `duration` (seconds) and this is ranked from it.
- **`failures`**: per-failure entries with `test_name`, `function`, `status`, `assertion`, `test_source`, `file_path`, `line`, `traceback` (errors only), and `server_log_tail`
- **`started_at`**: epoch seconds when the run began (the maestro reporter sends this as the run's start time)
- **`conf_drift`**: names of any `var/django.conf` keys that changed across the run — normally empty. A non-empty list means a `th.server_settings()` context did not restore cleanly and the key is now stranded. Key **names only**; values are never reported, since an override may be a credential.

LLM agents should always use `--agent` and read the JSON report instead of parsing terminal output. Never use `--plain` — it disables the rich UI but doesn't improve agent output.

### Maestro Reporting

A run can report itself to a [maestro](https://maestromojo.com) server, so a teammate or an agent can ask whether a project is passing without access to the machine that ran the tests. The wire format is maestro's published **Test Run Spec v1** (`docs/web_developer/maestro/TestRuns.md` in the maestro repo) — runner-neutral, and documented there rather than restated here.

**There is nothing to configure.** If this machine can already reach maestro, runs report themselves:

```bash
./bin/run_tests --agent          # reports, if maestro is installed
```

All three values it needs are already on the machine, so it reads them rather than asking for them again:

| Value | Discovered from |
|---|---|
| Server URL | the `maestro` MCP server in `~/.claude.json` (`https://host/mcp` → `https://host`) |
| API key | that same server's `Authorization` header, `/mcp/k/<key>` connector url, or `env.MAESTRO_API_KEY` |
| Project id | `.claude/maestro.json` in the repo, walking up from the working directory |

A machine with no maestro installed finds nothing, **says nothing** and reports nothing — which is what keeps a public framework from phoning home. Pass `--maestro` to make a run that found nothing say why instead of staying quiet.

**The Authorization scheme is part of the credential.** mojo's auth middleware routes on it — `apikey` to `ApiKey.validate_token`, `Bearer` to `User.validate_jwt` — so the wrong scheme is a flat 401, not a fallback. maestro issues its long-lived `user_api_key` **as a JWT**, so the thing everyone calls "the api key" authenticates as `Bearer`. The reporter uses the scheme the MCP config declares, and otherwise infers it from the token's shape (a JWT → `Bearer`, anything else → `apikey`). An environment-supplied `MAESTRO_API_KEY` never inherits the MCP entry's scheme, because it is a different credential.

**Turning it off:** `--no-maestro` for one run, or `"maestro": false` in a config file permanently.

> This replaced an opt-in gate that required `--maestro` or a config block *and* a hand-set `MAESTRO_API_KEY`. The feature shipped dark: every repo had the credential installed and none had the flag, so no project ever reported a single run. Discovery is also **strictly safer** than the `MAESTRO_API_KEY` environment variable it replaces — `bin/run_tests` sources `.env` with `set -a`, so an ambient key could pair with an unrelated `MAESTRO_URL` and post to a host that never issued it. A discovered url and key come from the same record, so the key can only travel to the server it was installed for.

Every discovered value can be overridden, so CI can point a run somewhere else:

| Setting | Environment | Config key | Default |
|---|---|---|---|
| Server URL | `MAESTRO_URL` | `maestro.url` | *discovered* |
| API key | `MAESTRO_API_KEY` | **never from config** | *discovered* |
| Project id | `MAESTRO_PROJECT` | `maestro.project` | *discovered* |
| Suite name | `MAESTRO_SUITE` | `maestro.suite` | `"full"` for `--full`, else server-side `"default"` |
| Your version | `MAESTRO_VERSION` | `maestro.version` | *(omitted)* |
| Timeout (s) | `MAESTRO_TIMEOUT` | `maestro.timeout` | `5.0` |
| Send failure detail | — | `maestro.diagnostics` | `true` |

The **key is never read from a config file** — those are committed, and a key in one would be a key in git. Environment or discovery only. `MAESTRO_VERSION` is *your project's* version of the code under test (semver, build or deploy tag), which is not the same thing as the commit or as testit's own version; the spec marks it SHOULD-report.

**A push never changes the exit code.** Every failure — offline, outage, bad key, timeout, even a bug in the reporter itself — degrades to one warning line; the exit code stays a function of the tests alone. One honest limit: the timeout bounds each socket operation, not wall clock, so a wedged DNS resolver can still exceed it.

**Partial runs are not reported.** maestro treats the latest push per suite as the project's status, so a run that is not the suite's verdict would overwrite a real result with a fragment. Refused: `-t` and `--ignore` runs, and any run cut short by `-s` or the abort key. In practice this means the iteration loop — run one module, fix, run it again — never touches the board; only a whole-suite run does.

`--extra`/`--full` runs *are* reported: they are complete runs of a wider tier. A `--full` run reports as **suite `full`** automatically, so its larger totals never alternate with the default tier's — without that, a default run passing after a red `--full` would report green over an extended-module failure and the failure would vanish from the board without being fixed.

**What leaves the machine.** The payload is the contract and nothing more: counters, per-suite stats, and — unless `maestro.diagnostics` is `false` — the first 50 failures with their **assertion messages and tracebacks**. Read that literally: `th.assert_eq` interpolates the actual and expected values into its message, so a failing assertion about a token, password or customer record sends that value to the configured server. Set `"diagnostics": false` to report green/red and counts only. The server error-log tail (`server_log_tail`) and test source (`test_source`) are never sent at any setting.

Only `https` is allowed, except to a loopback host — the request carries a bearer key, and it will not be sent in cleartext to anything else. Redirects are never followed.

### Tiers — default, `slow`, `extended`

Two opt-in tiers; `--full` turns on both.

```bash
./bin/run_tests                        # default tier only
./bin/run_tests --full                 # + slow + extended
./bin/run_tests --extra extended       # + just one tier
```

| Tier | Tag | Meaning |
|---|---|---|
| default | *(none)* | Critical. Runs on every invocation. |
| `slow` | `requires_extra: ["slow"]` | Expensive, or only meaningful before a release. |
| `extended` | `@th.requires_extra("extended")` | Correct and worth keeping, but not a critical contract. |

Two words rather than one because they are chosen on different grounds — `slow` is a
statement about cost, `extended` about criticality — and "why is this opt-in?" should be
answerable from the tag alone.

Currently `slow`: `test_security` (bouncer/rate-limiting, serial), `test_incident`, the
live-assistant tests.

#### Deciding the tier

A test belongs in the **default tier regardless of cost** if it covers a security boundary
(permissions, auth, tenant isolation, secrets), a core framework contract (model
save/serialize, REST dispatch, graphs, `request.DATA`), a bug that has already shipped
once, or anything whose failure means the framework is broken for *every* consumer. The
slowest single test in the suite is a shortlink scheme-injection test and it stays in the
default tier — cost alone never demotes a security test.

**`extended`** is for exhaustive input matrices past the first representative case, deep
feature-internal variants of one app, coverage already asserted elsewhere, and tests whose
cost *is* a timeout — an assertion that nothing arrives can only pass by waiting, so keep
the positive path in the default tier and demote the negative one.

**Not a reason to demote:** the app is optional. `requires_apps` already skips a module
whole when the project has not installed that app.

Tag at authoring time; auditing it back out later is far more expensive.

To move a whole module, add `"requires_extra": ["slow"]` (or `["extended"]`) to its
`__init__.py` TESTIT config. For a single test, use the decorator.

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
  "extra": "run-backfill,cleanup",
  "maestro": {"url": "https://maestro.example.com", "suite": "api"}
}
```

- `tests` and `ignore` accept strings or lists.
- `maestro` is a nested object rather than a flat default; it overrides what discovery found — see [Maestro Reporting](#maestro-reporting) for the keys. An empty object means "defaults"; `false` is the permanent opt-out.
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

# tests/test_job_engine/__init__.py  — serial for a reason OTHER than server_settings()
TESTIT = {
    "requires_apps": ["mojo.apps.jobs"],
    "serial": True,                          # JobEngine/Scheduler use signal handlers (main thread only)
}

# tests/test_oauth/__init__.py  — calls th.server_settings() but stays parallel;
# testit/server_lock.py keeps that restart away from open websockets on its own.
TESTIT = {
    "requires_apps": ["mojo.apps.account"],
}

# tests/test_security/__init__.py  — opt-in slow module
TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    "serial": True,
    "requires_extra": ["slow"],              # skipped unless --full or --extra slow
}
```

When a large app has many tests, split it into domain-focused packages (`test_auth`, `test_mfa`, `test_user_mgmt`, etc.) rather than one monolithic `test_accounts`. Each package runs in parallel by default.

> **`th.server_settings()` no longer requires `"serial": True`.** It restarts the
> server (writing `var/django.conf` for uvicorn to reload), which used to tear
> down any websocket another module had open — the cause of intermittent
> "Connection is already closed" failures during full-suite runs.
> `testit/server_lock.py` now serializes just that hazard: a `WsClient`
> connection holds a **shared** lock for its lifetime, and `server_settings()`
> takes it **exclusively** for both of its reloads. Modules stay parallel and
> only the actual restart windows are excluded — much cheaper than marking every
> caller serial. Use `"serial": True` for the *other* reasons (signal handlers
> bound to the main thread, as in `test_job_engine`; or a stress module that
> intentionally holds many sockets longer than the writer's fail-open timeout,
> as in `test_realtime`).
>
> A `WsClient` opened **inside** a `server_settings()` body is fine: the thread
> already holding the exclusive hold is granted the shared hold immediately
> (it is the restarter — it cannot tear down its own socket behind its own back).

### How `server_settings()` waits — a readiness signal, not a sleep

Both reloads (applying overrides, then restoring them) used to be fixed sleeps —
1.5s in, 3s out — sized to outlast the OLD uvicorn worker, since a plain socket
poll answers from a dying worker just as readily as a fresh one; "the server
responds" never meant "the server responds with my config."

The generated `_asgi.py` now writes `testproject/var/asgi_ready.json` (`{"pid":
..., "conf": sha256(django.conf)}`) once Django finishes loading. `server_settings()`
waits for a signal reporting **both** a different pid and the fingerprint of the
config it just wrote, then confirms the socket answers — an exact answer to "is
the new worker up with my config" instead of a timed guess, so the wait costs
what the reload actually costs. A write that leaves `django.conf` byte-identical
(e.g. restoring a value the file already held) skips the wait entirely: uvicorn
reloads on the write event, not a content diff, so rewriting identical bytes
would only queue a restart the fingerprint check already considers satisfied.

That skip is guarded rather than assumed. When no write was needed, the running
worker's own fingerprint must still match the file — if it does not, someone else
wrote `django.conf` or a previous context stranded a key, and the context raises
instead of running your test against settings nobody chose.

**A failed wait undoes its own write.** The overrides are on disk before the wait
begins, and the wait raises before the context's restore block is reached — so it
restores explicitly on the way out. Without that, a timed-out wait would leave the
override in `django.conf`, and uvicorn (`--reload-include '*.conf'`) would serve it
to every later test in the run.

**Falls back automatically** to the old sleep-based wait when
`testproject/var/asgi_ready.json` does not exist — an older testproject, or a
downstream project whose `_asgi.py` predates this convention. There is nothing
to configure; testit checks for the file on every call.

Assumes a single uvicorn worker, which is what `bin/asgi_local` starts. Under
multiple workers a probe answered by a different, un-restarted worker could
satisfy "different pid" while the old settings were still live on another one.

### How `server_settings()` restores — subtractive, and one at a time

- **It removes only the keys it set.** The context captures the exact source
  lines for its own keys (inside the lock), and on exit puts those lines back —
  or deletes the key when it was not there before — against `var/django.conf`
  **as it stands**. Every other line is left untouched.
- **Whole contexts are serialized process-wide.** Two overlapping contexts mean
  two live reloads fighting over one uvicorn worker; the second one waits.
  Acquisition has a 120s valve that logs loudly and proceeds, so a leaked
  context can never deadlock a run.
- **Why it matters:** the old implementation snapshotted the *whole file* on the
  way in and wrote that snapshot back on the way out. Modules run as threads, so
  a second context could snapshot while the first one's override was live and
  then restore it — stranding the first context's key in `var/django.conf`
  permanently, silently changing every later run.
- **The runner checks.** `testit/runner.py` parses `var/django.conf` at the start
  and end of every run and warns loudly if any key changed, naming the **keys
  only** (never the values). `--agent` reports the same list as `conf_drift`.
  Best-effort: it cannot catch a run killed mid-context.
- **Caution:** overrides are written to `var/django.conf` in **cleartext**. Do
  not pass a real credential you would mind sitting in a gitignored file.

Supported keys:

| Key | Default | Description |
|---|---|---|
| `serial` | `False` | Force this module to run sequentially, after all parallel modules complete. Use for modules that rely on signals bound to the main thread. **Not** needed merely because a module calls `th.server_settings()` — `testit/server_lock.py` handles that hazard without giving up parallelism. |
| `requires_apps` | `[]` | List of Django app labels. The module is skipped entirely if any listed app is not in `INSTALLED_APPS`. |
| `server_settings` | `{}` | **Reserved — not implemented.** The key is accepted by the config loader and then ignored; the runner never applies it. Use `th.server_settings()` inside the tests that need an override. |
| `requires_extra` | `[]` | List of `--extra` flags. The module is skipped unless at least one flag is present. Use `["slow"]` or `["extended"]` for opt-in modules included by `--full` — see Tiers above. |

All keys are optional. A missing `__init__.py` or a missing `TESTIT` assignment uses defaults (parallel, no app requirements).

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

> **Collection is by function-name PREFIX, not by decorator, and there is no
> teardown phase.** The runner collects `setup_*` for the setup phase and
> `test_*` for the test phase — nothing else. (The historical `quick_*` test
> prefix and its `-q`/`--quick` selector were removed; use the `slow`/`extended`
> tiers above to opt tests in or out instead.) A `cleanup_*` or `teardown_*`
> function is therefore dead code no matter how it is decorated (even
> `@django_unit_setup()`): it is never collected and never runs. Put per-module
> cleanup at the **top of `setup_`** instead — delete any leftover rows/secrets
> there before creating fixtures, since the database is long-lived.

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

## The HTTP Client (`opts.client`)

`opts.client` is a `RestClient` making real HTTP requests to the server over
localhost. It wraps `requests.Session`, so it behaves like a browser:

- **Cookies persist** across requests — server-set cookies like `_muid` (device
  identity), `_msid` (session identity) and `mbp` (bouncer pass) are stored and
  resent automatically, exactly like a browser cookie jar.
- **Realistic default headers** — `User-Agent`, `Accept`, `Accept-Language`,
  `Accept-Encoding` are sent, so server-side signal analysis (bouncer scoring and
  friends) sees traffic that resembles the real thing.
- **Cookies survive logout** — `logout()` clears auth tokens but keeps cookies,
  matching a browser where device identity outlives a re-login.
- **`clear_cookies()`** simulates a fresh browser with no history — use it for
  first-visit flows and `muid_missing` signals.

```python
resp = opts.client.get('/api/user/me')
resp = opts.client.post('/api/login', {'username': 'alice', 'password': 'secret'})
resp = opts.client.put('/api/user/42', {'display_name': 'Alice B.'})
resp = opts.client.delete('/api/user/42')
resp = opts.client.get('/api/resource', headers={'X-Custom': 'value'})
```

Every method returns an `objict`:

```python
resp.status_code      # int: HTTP status
resp.json             # objict: parsed JSON body (same as resp.response)
resp.response         # objict: parsed JSON body
resp.text             # str: raw text, when the body is not JSON
resp.error_reason     # str: HTTP reason phrase on non-2xx
token = resp.json.data.access_token     # dot access, courtesy of objict
```

Authentication — a successful `login()` stores the JWT and adds the
`Authorization` header to every later request on that client:

```python
opts.client.login('alice@example.com', 'password123')
th.assert_true(opts.client.is_authenticated, "login failed")
uid = opts.client.jwt_data.uid
opts.client.logout()
```

Read the objict caveat below before pulling fields off a response.

## Assert Helpers

Use the testit helpers rather than bare `assert` — they render expected vs actual
on failure, and `.claude/rules/testing.md` requires every assertion to carry a
descriptive message.

```python
th.assert_true(value, "descriptive message")
th.assert_eq(actual, expected, "descriptive message")   # prints both on failure
th.assert_in(item, container, "descriptive message")

with th.assert_raises(ValueError):
    some_code_that_raises()
```

**Never write a bare `assert condition`.** A failure with no message costs the
next reader a trip into the source to find out what was even being checked.

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

- A structured run report is written to `testproject/var/test_results.json`:
  - `total`, `passed`, `failed`
  - `records[]` with module, file, function, status (`passed`, `failed`, `error`, `skipped`), and `duration` (seconds, when the test actually ran)
  - timestamps (`started_at`, `finished_at`, `duration`)
- Agent report (written when `--agent` is passed): `testproject/var/test_failures.json`
  - Top-level `status`, `total`, `passed`, `failed`, `skipped`, `duration` — summed across all modules, including ones skipped whole
  - `ran` — the same counts restricted to what actually executed this run (excludes whole-skipped modules)
  - Per-module stats in `modules` dict (tests, passed, failed, skipped, duration, plus `skipped_reason` when the module was skipped whole)
  - `slowest` — the 25 slowest individual tests, sorted descending by `duration`
  - Per-failure diagnostics in `failures` list (test_source, file_path, line, traceback, server_log_tail)
  - `conf_drift` — names of `var/django.conf` keys stranded by a settings override (empty on a clean run)
- HTTP helper: `testit.client.RestClient`
  - Reuses auth tokens and integrates with `opts.client`.
  - `opts.client.last_response` — after every request this is set to an `objict` with `method`, `path`, `status_code`, `body`, `headers`, and `elapsed_ms`. Useful for diagnosing failures without re-running the request.
- WebSocket helper: `testit.ws_client.WsClient`
  - Build URL from the HTTP host and wait for typed messages.
- Faker snippets: `testit.faker`
  - Shared generator for generating deterministic-looking fixtures.

---

## Testing Async Jobs

Two helpers run jobs **in the test process**. Both exist because job handlers
are exactly the code that talks to AWS, ACME and DNS providers, so a test has
to be able to `mock.patch` them — and a job-engine daemon runs in a *separate*
process that would never see those patches (the same trap that makes
`override_settings()` useless against `opts.client`).

| | `th.run_jobs()` | `th.run_pending_jobs()` |
|---|---|---|
| Picks jobs from | the real Redis queue | `Job.objects.filter(status=...)` |
| Verifies `publish()` actually enqueued | **yes** | no |
| Honors `run_at` / `delay=` | **yes** — future jobs are left alone | no — runs them immediately |
| State transitions | the engine's own `execute_job` (JobEvents, attempts, `finished_at`, expiry) | sets `completed`/`failed` directly |
| Returns | `objict(count, job_ids, hit_limit)` | `int` |

Reach for **`run_jobs()`** when the dispatch itself is part of what you are
testing, or when scheduling matters. `run_pending_jobs()` is the older, simpler
helper and is fine when you only need the handler to run.

> **Never run a job-engine daemon during the suite.** It would race these
> drains for the same queue, and any job it won would execute in the daemon's
> process, where the test's patches do not apply — producing failures that
> reproduce only under load.

### `th.run_jobs()`

```python
jobs.publish(func="myapp.asyncjobs.do_thing", payload={"id": row.pk})
result = th.run_jobs()          # explicit barrier: publish, drain, assert
assert result.count == 1
row.refresh_from_db()
```

| Parameter | Default | Description |
|---|---|---|
| `channel` | `None` | One channel, a list, or `None` for `JOBS_CHANNELS`. |
| `max_jobs` | `100` | Safety stop, so a handler that re-publishes itself cannot spin forever. `result.hit_limit` reports when it fires. |
| `include_scheduled` | `True` | Promote due `delay=`/`run_at=` jobs first (what the Scheduler daemon does). |

Companions: `th.clear_jobs()` drops queued jobs so a module starts clean (worth
calling in setup — the DB and Redis are long-lived), `th.pending_job_count()`
asserts something *was* queued, and `th.promote_scheduled_jobs()` promotes due
jobs without running them.

**Isolate a job-testing module on its own channel.** The daemon warning above
applies just as much to another *test module*: with no `channel=`, `run_jobs()`
drains every channel in `JOBS_CHANNELS` and `clear_jobs()` wipes every one of
them, rows included. Modules run as parallel threads against one Redis and one
database, so two modules that both drain globally will take each other's jobs —
one deletes the other's rows between publish and drain, or executes its handler
inside the wrong test's assertions. Give a module that leans on the queue a
private channel and pass it everywhere:

```python
CHANNEL = "testit_myapp_jobs"        # any name legal per validate_channel_name

jobs.publish(func=HANDLER, payload={...}, channel=CHANNEL)
th.run_jobs(channel=CHANNEL)
th.clear_jobs(channel=CHANNEL)       # setup AND teardown
```

Keep the name **out of `JOBS_CHANNELS`** — that is what makes it invisible to
everyone else's no-argument drain. `publish()` takes the channel verbatim and
never requires the publishing box to consume it, so an unlisted channel is a
supported target, not a trick. `channel=` scopes the whole of `clear_jobs()`,
Job rows included, so a scoped clear never reaches another channel's rows.

The tradeoff to make deliberately: a module scoped this way no longer exercises
the no-argument "drain everything" path. Leave that to a module that publishes
through a real service and drains globally.

**Addressing handlers:** `publish()` stores a dotted **path** and re-imports it
at execution time, so a handler defined inside a test module must be addressed
by the name that module is actually loaded under. testit imports test files as
`test_pkg.test_file` (tests/ is on `sys.path`), so use `f"{__name__}.handler"`
rather than hardcoding `tests.test_pkg...` — the latter imports a *second copy*
of the module with its own module-level state, immune to your patches.

### `th.run_pending_jobs()`

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

---

## Test-Mode Headers (`X-Mojo-Test-*`)

Several framework hooks (geofence engine, account extension handlers, bouncer decorator) support per-request override headers so tests can change behavior without reloading the server. This is what lets `test_geofence`, `test_register`, `test_public_messages`, and `test_oauth` run in parallel in ~1–3s each.

### What's overridable

| Header | Overrides | Defined in |
|---|---|---|
| `X-Mojo-Test-Geo` (JSON, or the literal `"fail"`) | geoip lookup result (`"fail"` forces a lookup failure) | `mojo.apps.account.services.geofence.engine` |
| `X-Mojo-Test-Geofence-System` (JSON) | `GEOFENCE_SYSTEM_RULES` | `mojo.apps.account.services.geofence.engine` |
| `X-Mojo-Test-Geofence-Allowlist` (JSON list) | `GEOFENCE_ALLOWLIST` | same |
| `X-Mojo-Test-Geofence-Enabled` (`0`/`1`) | `GEOFENCE_ENABLED` | same |
| `X-Mojo-Test-Geofence-Fail-Closed` (`0`/`1`) | `GEOFENCE_FAIL_CLOSED` | same |
| `X-Mojo-Test-Geofence-Fail-Closed-Scopes` (comma list) | `GEOFENCE_FAIL_CLOSED_SCOPES` | same |
| `X-Mojo-Test-Geofence-Allow-Private` (`0`/`1`) | `GEOFENCE_ALLOW_PRIVATE_IPS` | same |
| `X-Mojo-Test-Geofence-Strict` (`0`/`1`) | `GEOFENCE_STRICT_POSTURE` | same |
| `X-Mojo-Test-Geofence-Cache-Ttl` (int) | `GEOFENCE_CACHE_TTL` | same |
| `X-Mojo-Test-Pre-Register-Validator` (dotted path) | `PRE_REGISTER_VALIDATOR` | `mojo.apps.account.services.extensions` |
| `X-Mojo-Test-User-Registered-Handler` (dotted path) | `USER_REGISTERED_HANDLER` | same |
| `X-Mojo-Test-User-Login-Handler` (dotted path) | `USER_LOGIN_HANDLER` | same |
| `X-Mojo-Test-Registration-Extra-Fields` (JSON list) | `REGISTRATION_EXTRA_FIELDS` | same |
| `X-Mojo-Test-Require-Group-On-Registration` (`0`/`1`) | `REQUIRE_GROUP_ON_REGISTRATION` | same |
| `X-Mojo-Test-Allow-User-Registration` (`0`/`1`) | `ALLOW_USER_REGISTRATION` | same |
| `X-Mojo-Test-Bouncer-Require-Token` (`0`/`1`) | `BOUNCER_REQUIRE_TOKEN` | `mojo.decorators.bouncer` |
| `X-Mojo-Test-Fresh-Auth-Window` (int seconds) | `FRESH_AUTH_WINDOW` | `mojo.apps.account.services.fresh_auth` |
| `X-Mojo-Test-Capture-Id` | per-test capture key for handler fixtures | `tests/test_register/_capture.py` |

### Security gate (mandatory)

Some of these headers — the dotted-path handler ones in particular — can load arbitrary importable callables. To prevent an accidental production leak from becoming a remote-code-execution vector, `mojo.helpers.test_mode.is_test_request(request)` is the gate, and EVERY callsite consults it before honoring a header. The gate requires **all three** of:

1. `MOJO_TEST_MODE = True` in Django settings. Defaults to False.
2. `REMOTE_ADDR` is loopback (`127.0.0.1`, `::1`, or `localhost`).
3. NO `X-Forwarded-For`, `Forwarded`, or `Via` header on the request.

Production deployments don't set the flag, AND any LB always adds `X-Forwarded-For`. The gate is closed-by-default and survives accidental flag leaks because external traffic can never satisfy #2 + #3.

`MOJO_TEST_MODE` is read **conf-file-only** (`settings.get_static`) — a DB/Redis `Setting` row (writable via the generic `/api/settings` REST or Redis access) can NOT enable it. The same applies to the geofence `GEOFENCE_TEST_OVERRIDE` knob. Both are deploy-time settings-file values by design; the header plane and geo override can never be armed through the remotely-writable settings plane.

### Enabling for your own project's tests

If you're a consumer of django-mojo writing tests against these endpoints, add ONE line to your test environment settings:

```python
# In your project's local/test settings module:
MOJO_TEST_MODE = True
```

Your tests then send the relevant `X-Mojo-Test-*` headers per-request via the testit client:

```python
resp = opts.client.post("/api/auth/login",
                        {"username": "u", "password": "p"},
                        headers={"X-Mojo-Test-Geo": '{"country_code": "US"}'})
```

The testit `RestClient` merges the `headers=` kwarg with its default auth headers — no extra setup needed beyond the env var.

Most consumer projects don't need this at all — they configure handlers via settings and run tests against that fixed config. Only reach for the headers when you genuinely need per-test handler swapping (e.g. testing the asymmetric error contracts of `USER_REGISTERED_HANDLER` vs `USER_LOGIN_HANDLER`).
