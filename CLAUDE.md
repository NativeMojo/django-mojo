# Django-MOJO Detailed Agent Rules

This file contains detailed implementation rules for AI agents and contributors.

MOST IMPORTANT: FOLLOW KISS (Keep It Simple, Stupid).

## Thread Start Protocol (Mandatory)

For every new thread:

1. Read `Agent.md`.
2. Read `CLAUDE.md` (this file).
3. Read `memory.md` (if present) for active context.
4. Select mode:
   - Planning mode -> apply `prompts/planning.md`
   - Building mode -> apply `prompts/building.md`
5. If unclear whether planning or building is expected, ask one brief clarifying question.

Do not begin implementation before this protocol.

## Source of Truth

| Artifact | Authority |
|---|---|
| `docs/django_developer/README.md` | Framework/backend developer documentation |
| `docs/web_developer/README.md` | REST/web integration documentation |
| `Agent.md` | Top-level operating contract |
| `CLAUDE.md` | Detailed coding conventions |
| `prompts/planning.md` | Planning prompt mode |
| `prompts/building.md` | Build/implementation mode |
| `memory.md` | Ongoing decision/context log across threads |

When in doubt:
1. Follow current docs.
2. Follow existing code patterns in the target app.
3. Read `memory.md` for in-progress context (if present).
4. Ask the user before making assumptions.

Memory hygiene requirement:
- Keep `memory.md` compact and pruned (active items only in main sections).
- Move completed but relevant notes to the `Archive` section.

## Core Framework Conventions

### Models

- Regular models: inherit `models.Model, MojoModel` (in that order).
- Secrets models: inherit `MojoSecrets, MojoModel` (do not include `models.Model`).
- Include `created` and `modified` fields:
  - `created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)`
  - `modified = models.DateTimeField(auto_now=True, db_index=True)`
- Keep one model per file.
- Use `user` (`account.User`) and/or `group` (`account.Group`) where access control requires it.
- Define `RestMeta` with explicit permissions and graphs.

### REST

- Use `request.DATA` only for request inputs.
- Prefer simple CRUD handlers:
  - `@md.URL('resource')`
  - `@md.URL('resource/<int:pk>')`
  - `return Model.on_rest_request(request, pk)`
- Keep list endpoints without trailing slash.
- URL prefix is the app directory name.
- Dynamic segments go at the end only.
- For per-instance operations, prefer `POST_SAVE_ACTIONS` + `on_action_<name>` over custom endpoints when possible.

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
- This repository is a Django framework, not a runnable project.
- Do not run Django-project-specific commands here.
- Ask the user to run tests/migrations in their Django project environment.
- Never create migration files in this repository.

### Python Standards

- Never use Python type hints in framework code.
- Prefer explicit, straightforward implementations over clever abstractions.

## Documentation Rules

- Keep both doc tracks in sync when behavior changes:
  - `docs/django_developer/*` for backend developers
  - `docs/web_developer/*` for API consumers
- Do not reference old doc paths like `docs/rest_api/*` in this repo.
- Update root indexes when adding new areas:
  - `docs/django_developer/README.md`
  - `docs/web_developer/README.md`
- Update `CHANGELOG.md` for meaningful changes.

## Delivery Checklist

Before finalizing work:

1. Confirm edits follow `request.DATA`, security, and endpoint conventions.
2. Confirm tests were added/updated where needed.
3. Confirm docs are updated for both audiences when required.
4. Confirm `CHANGELOG.md` is updated if behavior or guidance changed.
5. Update and prune `memory.md` with key decisions and follow-ups.
6. Tell the user what to run in their Django project to validate.
