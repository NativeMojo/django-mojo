# LLM Agent Rule Creation Guidance

**Type**: request
**Status**: open
**Date**: 2026-04-09
**Priority**: medium

## Description
Add explicit guidance to the LLM incident agent so it creates rules correctly. Two specific issues:

1. **No `metadata.` prefix on field names** — Rule `field_name` values should use bare names like `http_url`, `level`, `risk_score`. The rule engine (`Rule.check_rule()`) already looks up `event.metadata.get(field_name)` first, then falls back to `getattr(event, field_name)`. Prefixing with `metadata.` causes lookups to fail silently.

2. **Don't use extreme priority values** — LLM-proposed rulesets should use priority 25-75 (mid-range), not 0 or 1. Lower priority numbers run first, and existing hand-crafted defaults use 1-50. Placing LLM rules in the middle leaves room for future rules that need to match before or after them.

## Context
The LLM agent can create rules via `create_rule` tool (always disabled, pending human approval). Currently nothing tells the agent about these conventions, so it may produce rules that silently fail to match (metadata prefix) or that run in the wrong order (priority too low).

## Acceptance Criteria
- ANALYSIS_PROMPT's "Rules for Rule Creation" section includes guidance on both issues
- `create_rule` tool's `field` property description warns against `metadata.` prefix
- `create_rule` tool's `priority` property description specifies 25-75 range and explains why
- Default priority in `_tool_create_rule()` changed from 5 to 50

## Investigation
**What exists**: 
- `Rule.check_rule()` already handles bare field names correctly — looks up `event.metadata[field_name]` then `getattr(event, field_name)`
- `create_rule` tool defaults priority to 5 (too low, competes with hand-crafted defaults at 1-10)
- ANALYSIS_PROMPT has a "Rules for Rule Creation" section — natural place to add guidance
- Default rules use priority 1-50, catch-all at 9999

**What changes**:
- `mojo/apps/incident/handlers/llm_agent.py` — three locations:
  1. ANALYSIS_PROMPT "Rules for Rule Creation" section (~line 91): add two bullets
  2. `create_rule` tool schema (~line 261): update `field` description to warn against prefix
  3. `create_rule` tool schema (~line 209): update `priority` description and default to 50
  4. `_tool_create_rule()` (~line 554): change default from 5 to 50

**Constraints**: Keep prompt additions minimal — two short bullets, not paragraphs. The prompts are well-sized currently.

**Related files**:
- `mojo/apps/incident/handlers/llm_agent.py` (only file that changes)
- `mojo/apps/incident/models/rule.py` (reference — `check_rule()` at line 715, `ensure_default_rules()` for priority examples)

## Tests Required
- No new tests needed — this is prompt/schema text changes only
- Existing rule engine tests in `tests/test_incident/rule_engine_comprehensive.py` already validate that bare field names work

## Out of Scope
- Changes to SYSTEM_PROMPT (only mentions rule creation in passing, not a primary rule-creation path)
- Changes to rule matching logic itself
- Any other LLM agent behavior changes
