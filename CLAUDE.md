# Django-MOJO

This file is loaded automatically by Claude Code.

## Project

Django-mojo is a Django backend framework providing models, REST, auth, jobs, metrics, realtime, chat, security, and more. It is a library/framework, not a standalone runnable project.

## How to Work Here

- **Rules** are in `.claude/rules/` and load automatically. Follow them.
- **Skills** are in `.claude/skills/` — invoked with `/<name>` (e.g., `/bug`, `/request`, `/plan`, `/build`, `/memory`).
- **Agents** are in `.claude/agents/` — spawned automatically by the build skill.
- See `AI_DEV.md` for the full developer workflow.
- Read `docs/django_developer/README.md` before building — do not reinvent existing features.

## Planning

Active work is tracked as files in `planning/`:
- `planning/issues/` — open bugs
- `planning/requests/` — open feature requests
- `planning/done/` — resolved items

## Trust Order

When docs and code conflict:
1. `docs/django_developer/README.md`
2. `docs/web_developer/README.md`
3. Existing code patterns in the target app
