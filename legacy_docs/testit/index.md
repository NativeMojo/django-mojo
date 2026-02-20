# TestIt Framework

TestIt is the Django-MOJO test harness for API, unit, and integration coverage.  
It is intentionally lightweight so both humans and LLM agents can reason about a suite, spot design issues early, and iterate fast.

---

## Core Ideas
- Keep the filesystem predictable: numbered filenames (`1_test_models.py`, `3_test_flows.py`) control execution order because TestIt sorts alphabetically.
- Prefer reusing state instead of recreating fixtures. Store shared objects on `opts` during setup and tear them down only when reuse is impossible.
- Tests are documentation. If an API feels awkward, pause and call it out rather than embedding new logic in the test.
- Expensive or destructive flows must opt-in via `--extra` or the `@requires_extra` decorator.
- Name test packages `test_<app>` (for example `test_accounts`) to avoid module collisions with real Django apps.

---

## Project Layout

```
apps/
  tests/
    test_accounts/
      __init__.py
      1_test_models.py
      2_test_views.py
      3_test_flows.py
docs/
  testit/
    index.md
    examples/
      1_test_models.py
      3_test_flows.py
      testit.config.json
```

- Sorting comes from prefixes. Adjust numbers (or add `_suffix`) to control module order.
- **Do not name test packages identically to the Django app.** Use `tests/test_accounts/` instead of `tests/accounts/` so imports never collide when the runner appends the folder to `sys.path`.
- Each file keeps decorators at the top, followed by related tests in definition order.
- Example files live in `docs/testit/examples/` for quick copy/paste or prompting.

---

## Running TestIt

- Run everything (Mojo apps + local project):  
  `./bin/testit.py`
- Target a module or a specific file:  
  `./bin/testit.py -m test_accounts`  
  `./bin/testit.py -m test_accounts.1_test_models`
- Append multiple modules:  
  `./bin/testit.py -t test_accounts.1_test_models -t test_billing.3_test_flows`
- Verbose output and early exit:  
  `./bin/testit.py -v -s`
- Show tracebacks without verbosity:  
  `./bin/testit.py -e`
- Toggle app scopes:  
  `./bin/testit.py --onlymojo` · `./bin/testit.py --nomojo`
- See every declared `@requires_extra` flag (respects filters like `-m` and `-t`; static scan only, no tests executed):  
  `./bin/testit.py --list-extras`

### JSON Config
CLI flags always win, but you can seed defaults through a JSON file:

```bash
./bin/testit.py --config docs/testit/examples/testit.config.json --extra run-backfill
```

Supported keys:

```json
{
  "tests": ["test_accounts", "test_helpers.cron"],
  "ignore": ["test_aws"],
  "stop_on_fail": true,
  "show_errors": true,
  "verbose": true,
  "nomojo": true,
  "module": "test_accounts",
  "extra": "run-backfill,cleanup"
}
```

- `tests` and `ignore` accept strings or lists.
- `show_errors` is equivalent to `-e`.  
- `extra` accepts either a comma-separated string or a JSON list; at runtime it is exposed as `opts.extra_list` (and `opts.extra` remains a comma-joined string for legacy helpers).
- Supply fewer flags in automation scripts; let interactive runs override what is needed.

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

- **Asserts must explain the failure.** Include context, inputs, or expected behaviour.
- **No `print()` debugging.** Use `-v` or `-e` for deeper logs.
- **Stay inside the framework.** If behaviour is missing, file a TODO or note it in review instead of patching logic into the test.
- **Call out design friction.** Tests should highlight confusing APIs; they should not cement workarounds.
- **Reuse fixtures.** Prefer creating entities in setup once, mutate through `opts`, and tear down only when required for repeat runs.

---

## Frequent Mistakes (LLM Watchlist)

- Forgetting alphabetical filenames (`1_`, `2_`, `3_`) and losing ordering guarantees.
- Naming the test package the same as the Django app and shadowing real modules (prefix with `test_`).
- Importing Django models at the module top or inside `@unit_*` functions.
- Creating fresh users/records for every assertion instead of reusing shared state.
- Missing assertion messages, making failures opaque for operators.
- Skipping the `--extra` gate on expensive tasks (cron jobs, third-party calls).
- Writing custom business logic in tests instead of exercising the real APIs.

---

## Outputs & Tooling

- A structured run report is written to `var/test_results.json`:
  - `total`, `passed`, `failed`
  - `records[]` with module, file, function, status (`passed`, `failed`, `error`, `skipped`)
  - timestamps (`started_at`, `finished_at`, `duration`)
- HTTP helper: `testit.client.RestClient`
  - Reuses auth tokens, and integrates with `opts.client`.
- WebSocket helper: `testit.ws_client.WsClient`
  - Build URL from the HTTP host and wait for typed messages.
- Faker snippets: `testit.faker`
  - Shared generator for generating deterministic-looking fixtures.

---

## Prompting / Pairing Checklist

1. Identify the module and create or update numbered files.
2. Draft setups first. Ensure Django imports stay inside decorated functions.
3. Reuse `opts` data; avoid redundant inserts.
4. For high-cost paths, add `@requires_extra("...")` and document the flag.
5. Write asserts with actionable failure reasons.
6. Before implementing workarounds, question the upstream API — document friction for follow-up.
7. Run with `./bin/testit.py -v -e` (or via config) when validating locally.

Keeping these habits makes the suite predictable for both humans and models, highlights design gaps early, and keeps TestIt simple.
