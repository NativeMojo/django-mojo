# Django-MOJO Agent Contract

This file is the top-level operating contract for any AI agent working in this repository.

MOST IMPORTANT: FOLLOW KISS (Keep It Simple, Stupid).

## Mandatory Thread Start (Every New Thread)

1. Read `Agent.md`.
2. Read `CLAUDE.md`.
3. Read `memory.md` (if present) for active context.
4. Choose mode before doing work:
   - Planning task -> use `prompts/planning.md`
   - Build/implementation task -> use `prompts/building.md`
5. If mode is unclear, ask one short clarifying question before proceeding.

Do not skip this startup sequence.

## Source of Truth

| Artifact | Purpose |
|---|---|
| `docs/django_developer/README.md` | Backend/framework developer docs |
| `docs/web_developer/README.md` | REST/web integrator docs |
| `CLAUDE.md` | Detailed coding and architecture conventions |
| `prompts/planning.md` | Prompt-planning mode instructions |
| `prompts/building.md` | Implementation mode instructions |
| `memory.md` | Living context log for decisions, open items, and handoffs |
| `CHANGELOG.md` | Release notes and significant changes |

If docs and assumptions conflict, trust the docs in this order:
1. `docs/django_developer/README.md`
2. `docs/web_developer/README.md`
3. Existing code patterns in the target app
4. `memory.md` for in-progress context (when present)

## Non-Negotiable Rules

- Use `request.DATA` for request input. Never use `request.POST.get()` or `request.GET.get()` directly.
- Keep endpoints and code simple. Prefer standard `Model.on_rest_request(request, pk)` CRUD wiring.
- Never add migration files in this repository.
- Never run Django-project-specific commands in this framework repo.
  - Ask the user to run tests/migrations in their Django project.
- Never use Python type hints in framework code.
- Default to fail-closed security and explicit permission checks.
- Never make blind edits; read target files first.
- Keep `memory.md` pruned: compact, current, and decision-focused.

## Documentation and Change Hygiene

When behavior, API, or conventions change:

1. Add/adjust tests in `tests/` (usually with `testit`).
2. Update relevant docs in both developer tracks when applicable.
3. Update `CHANGELOG.md` with a concise entry.
4. Keep `Agent.md` and `CLAUDE.md` aligned (no contradictory guidance).

## Before Editing Code

1. Read `memory.md` first (if present) for current context and prior decisions.
2. Read files you will modify.
3. Check for related tests and docs.
4. Confirm assumptions with the user when uncertain.
5. Implement the minimal change that solves the problem.
