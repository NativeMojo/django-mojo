# LLM Agent create_rule uses wrong field names for Rule model

**Type**: bug
**Status**: open
**Date**: 2026-04-07
**Severity**: high

## Description
The LLM Analyzer's `_tool_create_rule` function in `llm_agent.py` passes incorrect keyword arguments when creating `Rule` objects, causing `Rule() got unexpected keyword arguments: 'rule_set', 'operator'` on every attempt.

Two field names are wrong:
- `rule_set=ruleset` should be `parent=ruleset`
- `operator=rule_data.get("operator", "eq")` should be `comparator=rule_data.get("comparator", "==")`

Additionally, the call is missing fields that the assistant tool's version correctly includes: `name`, `index`, and `value_type`.

## Context
This is the LLM Analyzer on Incident — a separate code path from the assistant tool (`security/rules.py`) which works correctly. The LLM Analyzer uses its own `_tool_create_rule` in `llm_agent.py` which was written with stale/wrong field names. Every rule proposal from the LLM Analyzer fails silently — rules are never created, and the LLM reports the error back to the user.

## Acceptance Criteria
- `_tool_create_rule` in `llm_agent.py` uses correct field names: `parent`, `comparator`
- Includes `name`, `index`, and `value_type` fields matching the Rule model
- LLM Analyzer can successfully create rules with child conditions
- Existing tests (if any) for llm_agent rule creation pass

## Investigation
**Likely root cause**: Wrong field names in `Rule.objects.create()` call at `llm_agent.py:662-667`
**Confidence**: confirmed
**Code path**:
- `mojo/apps/incident/handlers/llm_agent.py:637` — `_tool_create_rule(params)` function
- `mojo/apps/incident/handlers/llm_agent.py:662-667` — the broken `Rule.objects.create()` call
- `mojo/apps/incident/models/rule.py:678-705` — Rule model with correct field names: `parent`, `comparator`

**Working reference**: `mojo/apps/assistant/services/tools/security/rules.py:157-166` — the assistant tool's version which uses the correct field names

**Regression test**: not written — fix is a 3-line field rename, straightforward to verify
**Related files**:
- `mojo/apps/incident/handlers/llm_agent.py` — the fix goes here
- `mojo/apps/incident/models/rule.py` — Rule model (reference only)

## Fix
Line 662-667 in `llm_agent.py` should change from:
```python
Rule.objects.create(
    rule_set=ruleset,
    field_name=rule_data.get("field", ""),
    operator=rule_data.get("operator", "eq"),
    value=rule_data.get("value", ""),
)
```

To:
```python
Rule.objects.create(
    parent=ruleset,
    name=rule_data.get("name", ""),
    index=i,
    field_name=rule_data.get("field", ""),
    comparator=rule_data.get("comparator", "=="),
    value=rule_data.get("value", ""),
    value_type=rule_data.get("value_type", "str"),
)
```

(with `for i, rule_data in enumerate(...)` to get the index)
