# Core Rules

These rules apply to all work in this repository. Non-negotiable.

## Input Handling
- Use `request.DATA` for request input. Never `request.POST.get()` or `request.GET.get()`.

## Forbidden Actions
- Never use Python type hints in framework code.
- Never make blind edits — read target files first.
- Never use `import logging` or `logging.getLogger()`. Use `from mojo.helpers import logit` instead — `logit.info()`, `logit.error()`, etc. for convenience, or `logger = logit.get_logger(name, "app.log")` for named loggers. The only file allowed to import stdlib logging is `mojo/helpers/logit.py`.

## Migrations & Test Infrastructure
- django-mojo ships its own migrations. After adding or changing models, run `bin/create_testproject` to regenerate the test project with proper migrations.
- Use `bin/create_testproject` after schema changes — it runs `makemigrations` and `migrate` automatically.
- Use `bin/run_tests` to run the test suite. Never hack around missing tables with raw SQL.
- See `TESTING.md` for the full workflow.

## Security
- Default to fail-closed security and explicit permission checks.
- Do not weaken owner/group/system permission boundaries.
- Never expose secrets or sensitive internals in REST graphs.

## Philosophy
- KISS — prefer the minimal change that solves the problem. No clever abstractions.
- Keep modules short, explicit, and easy to discover.
- Domain logic belongs in `app/services/`. Shared framework helpers belong in `mojo/helpers/`.
- Check `mojo/helpers/` before writing new utilities or importing third-party libs — dates, crypto, settings, logit, response helpers are already there.
- Prefer `objict` over dataclasses/namedtuples when the object is just data with no behavior.
- Confirm assumptions with the user when uncertain — don't guess on design decisions.

## Trust Order
When docs and code conflict, trust in this order:
1. `docs/django_developer/README.md`
2. `docs/web_developer/README.md`
3. Existing code patterns in the target app

## Delivery Checklist
Before closing any task:
1. Edits follow `request.DATA`, security, and endpoint conventions
2. If models changed, run `bin/create_testproject` then `bin/run_tests`
3. Tests added/updated where needed
4. Docs updated for both audiences when applicable
5. `CHANGELOG.md` updated if behavior or guidance changed
