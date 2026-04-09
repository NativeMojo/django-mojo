# LLM Agent Rule Creation Guidance

**Type**: request
**Status**: resolved
**Date**: 2026-04-09
**Priority**: medium

## Description
Add explicit guidance to the LLM incident agent so it creates rules correctly. Two specific issues:

1. **No `metadata.` prefix on field names** — Rule `field_name` values should use bare names like `http_url`, `level`, `risk_score`. The rule engine (`Rule.check_rule()`) already looks up `event.metadata.get(field_name)` first, then falls back to `getattr(event, field_name)`. Prefixing with `metadata.` causes lookups to fail silently.

2. **Don't use extreme priority values** — LLM-proposed rulesets should use priority 25-75 (mid-range), not 0 or 1. Lower priority numbers run first, and existing hand-crafted defaults use 1-50. Placing LLM rules in the middle leaves room for future rules that need to match before or after them.

## Context
The LLM agent can create rules via `create_rule` tool (always disabled, pending human approval). Currently nothing tells the agent about these conventions, so it may produce rules that silently fail to match (metadata prefix) or that run in the wrong order (priority too low).

## Resolution

**Status**: resolved
**Date**: 2026-04-09

### What Was Built
Instead of adding prompt bloat, fixed the issues in code:
- `Rule.check_rule()` now strips `metadata.` prefix from field names before lookup
- `_tool_create_rule()` also strips the prefix at creation time (defense in depth)
- Default priority for LLM-proposed rulesets changed from 5 to 50
- Added null guard and dunder-attribute rejection in `check_rule()` (security hardening)

### Files Changed
- `mojo/apps/incident/models/rule.py` — `check_rule()` strips `metadata.` prefix, rejects null/dunder field names
- `mojo/apps/incident/handlers/llm_agent.py` — `_tool_create_rule()` strips prefix at creation, default priority 5 -> 50
- `tests/test_incident/rule_engine_comprehensive.py` — added `test_rule_strips_metadata_prefix`
- `docs/django_developer/logging/incidents.md` — documented bare field name convention and priority 50 default

### Tests
- `tests/test_incident/rule_engine_comprehensive.py` — `test_rule_strips_metadata_prefix`
- Run: `bin/run_tests -t test_incident.rule_engine_comprehensive`
- Full suite: 1,544 tests, 0 failures

### Docs Updated
- `docs/django_developer/logging/incidents.md` — RuleSet priority and Rule field_name conventions

### Security Review
- Added null guard for nullable `field_name` (prevents crash)
- Added dunder rejection (`field_name.startswith("_")`) before `getattr` (prevents access to `__dict__`, `__class__`, etc.)
- No critical concerns

### Follow-up
- None
