# Django-MOJO Agent Contract

This is the master operating contract for any AI agent working in this repository.
Claude Code auto-loads `CLAUDE.md` which redirects here — this file is the single source of truth.

MOST IMPORTANT: FOLLOW KISS (Keep It Simple, Stupid).

## Mandatory Thread Start (Every New Thread)

1. Read `Agent.md` (this file).
2. Read `memory.md` (if present) for active context.
3. Choose mode before doing work:
   - Planning / scoping task -> use `prompts/planning.md`
   - Build / implementation task -> use `prompts/building.md`
   - Bug fixing task -> use `prompts/bug_fixer.md`
4. If mode is unclear, ask one short clarifying question before proceeding.

Do not skip this startup sequence.

## Source of Truth

| Artifact | Purpose |
|---|---|
| `docs/django_developer/README.md` | Backend/framework developer docs |
| `docs/web_developer/README.md` | REST/web integrator docs |
| `prompts/planning.md` | Feature planning and scoping mode |
| `prompts/building.md` | Implementation mode instructions |
| `prompts/bug_fixer.md` | Bug triage and fix mode |
| `memory.md` | Living context log for decisions, open items, and handoffs |
| `CHANGELOG.md` | Release notes and significant changes |

If docs and assumptions conflict, trust in this order:
1. `docs/django_developer/README.md`
2. `docs/web_developer/README.md`
3. Existing code patterns in the target app
4. `memory.md` for in-progress context

## Non-Negotiable Rules

- Use `request.DATA` for request input. Never `request.POST.get()` or `request.GET.get()`.
- Keep endpoints simple. Prefer `Model.on_rest_request(request, pk)` CRUD wiring.
- Never add migration files in this repository.
- Never run Django-project-specific commands here — ask the user to run them.
- Never use Python type hints in framework code.
- Default to fail-closed security and explicit permission checks.
- Never make blind edits; read target files first.
- Keep `memory.md` pruned: compact, current, and decision-focused.

## Core Framework Conventions

### Models

- Regular models: inherit `models.Model, MojoModel` (in that order).
- Secrets models: inherit `MojoSecrets, MojoModel` (do not include `models.Model`).
- Include `created` and `modified` fields:
  - `created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)`
  - `modified = models.DateTimeField(auto_now=True, db_index=True)`
- One model per file.
- Use `user` (`account.User`) and/or `group` (`account.Group`) where access control requires it.
- Define `RestMeta` with explicit permissions and graphs.

### REST

- Use `request.DATA` only for request inputs.
- Prefer simple CRUD handlers:
  - `@md.URL('resource')`
  - `@md.URL('resource/<int:pk>')`
  - `return Model.on_rest_request(request, pk)`
- No trailing slash on list endpoints.
- URL prefix is the app directory name. Dynamic segments go at the end only.
- For per-instance operations, prefer `POST_SAVE_ACTIONS` + `on_action_<name>` over custom endpoints.

### Services and Helpers

- Domain logic belongs in `app/services/`.
- Shared framework helpers belong in `mojo/helpers/`.
- Keep modules short, explicit, and easy to discover.

### Security

- Default to fail-closed permission behavior.
- Do not weaken owner/group/system permission boundaries.
- Never expose secrets or sensitive internals in REST graphs.

### Testing

- Add or update tests for behavior changes (usually with `testit`).
- This repository is a framework, not a runnable project.
- Ask the user to run tests/migrations in their Django project environment.
- Never create migration files in this repository.

### Python Standards

- Never use Python type hints in framework code.
- Prefer explicit, straightforward implementations over clever abstractions.

## Documentation Rules

- Keep both doc tracks in sync when behavior changes:
  - `docs/django_developer/*` for backend developers
  - `docs/web_developer/*` for API consumers
- Update root indexes when adding new doc areas.
- Update `CHANGELOG.md` for meaningful changes.

## Before Editing Code

1. Read `memory.md` for current context and prior decisions.
2. Read every file you will modify.
3. Check for related tests and docs.
4. Confirm assumptions with the user when uncertain.
5. Implement the minimal change that solves the problem.

## Delivery Checklist

Before closing any task:

1. Edits follow `request.DATA`, security, and endpoint conventions.
2. Tests added/updated where needed.
3. Docs updated for both audiences (when applicable).
4. `CHANGELOG.md` updated if behavior or guidance changed.
5. `memory.md` updated and pruned with key decisions.
6. User told what to run in their Django project to validate.
