# Assistant tool_result serialization fails on datetime values

**Type**: bug
**Status**: resolved
**Date**: 2026-04-20
**Severity**: high

## Description
The assistant WS agent crashes whenever a tool returns a payload that contains a `datetime` (or other non-JSON-native type). `_execute_tool` serializes the tool result with `ujson.dumps`, which does not know how to encode `datetime.datetime`, so the entire agent turn aborts with:

```
TypeError: datetime.datetime(2026, 4, 15, 14, 51, 17, 38364, tzinfo=datetime.timezone.utc) is not JSON serializable
```

The error surfaces to the user as `Assistant error: ... is not JSON serializable` and the conversation stalls.

## Context
Many assistant tools return model data (created/modified timestamps, scheduled-at fields, last-login, etc.). Any such tool currently breaks the WS agent flow. This makes a broad class of read/query tools unusable and is user-facing.

## Acceptance Criteria
- A tool handler that returns a dict containing a `datetime` (aware or naive), `date`, `Decimal`, or `UUID` serializes successfully into the `tool_result.content` string.
- The serialized datetime is a stable ISO-8601 string that the model can read back.
- All three `ujson.dumps(...)` call sites in `agent.py` (success path, timeout/failure path, and parallel-plan failure path) go through the same safe-serialize helper.
- A regression test exercises a tool that returns a datetime and asserts the agent produces a `tool_result` block instead of raising.

## Investigation
**Likely root cause**: `ujson.dumps` at [mojo/apps/assistant/services/agent.py:671](mojo/apps/assistant/services/agent.py:671) has no `default=` hook and ujson does not natively serialize `datetime`. Tool handlers returning anything containing a datetime (very common — model serializers, job records, metrics) blow up the whole agent turn rather than being coerced to a string.

**Confidence**: confirmed (from the traceback and direct code read).

**Code path**:
- [mojo/apps/assistant/services/agent.py:1126](mojo/apps/assistant/services/agent.py:1126) — `run_assistant_ws` calls `_execute_tools`
- [mojo/apps/assistant/services/agent.py:661](mojo/apps/assistant/services/agent.py:661) — `_execute_tools` dispatches `_execute_tool`
- [mojo/apps/assistant/services/agent.py:628-672](mojo/apps/assistant/services/agent.py:628) — `_call_handler` returns `tool_result`, then `ujson.dumps(tool_result)` raises
- Secondary call sites with the same flaw: [agent.py:724](mojo/apps/assistant/services/agent.py:724), [agent.py:825](mojo/apps/assistant/services/agent.py:825)

**Regression test**: not written in this session — will be added during the fix. Straightforward to write against a stub tool registered in the registry that returns `{"ts": timezone.now()}`.

**Related files**:
- `mojo/apps/assistant/services/agent.py` — all three `ujson.dumps` sites
- `mojo/helpers/` — candidate home for a shared `json_safe_dumps(obj)` helper (e.g., wrap `json.dumps(..., default=str)` or use `DjangoJSONEncoder`) since other callers likely want the same behavior
- `tests/test_assistant/` — regression test location

## Suggested Fix Direction
Introduce a single helper (e.g. `mojo.helpers.response.safe_json_dumps` or colocated in assistant services) that falls back to `str()` / `isoformat()` for datetimes and other non-JSON types, and replace all three `ujson.dumps` call sites with it. Do not silently swallow the error — we want the data through, not a dropped tool result.

## Plan

**Status**: planned
**Planned**: 2026-04-20

### Objective
Make the assistant tool-result boundary serialization-safe for datetime/Decimal/UUID/Model/QuerySet, and route every assistant agent/tool failure through the incident-event system so users and operators see what broke.

### Steps
1. `mojo/apps/assistant/services/agent.py` — Tool-boundary safety + incidents
   - Add `_dumps_tool_result(obj, *, user, conversation, tool_name=None)` helper: stdlib `json.dumps(obj, default=_json_default)`. `_json_default` coerces `datetime`/`date` → `.isoformat()`, `Decimal`/`UUID` → `str`, Django `Model` → `obj.to_dict()` if available else `repr`, `QuerySet` → `list(obj.values())`, final fallback `str(obj)`. If serialization still raises, call `_report_event("assistant:error:serialize", 7, …)` with `traceback.format_exc()[:2000]`, `tool_name`, `user`, `conversation`, and return `json.dumps({"error": "Tool result could not be serialized: <msg>"})`.
   - Replace the three `ujson.dumps` call sites at [mojo/apps/assistant/services/agent.py:671](mojo/apps/assistant/services/agent.py:671), [:724](mojo/apps/assistant/services/agent.py:724), [:825](mojo/apps/assistant/services/agent.py:825) with this helper.
   - Enrich the existing tool-exception branch at [agent.py:657-666](mojo/apps/assistant/services/agent.py:657): include `traceback.format_exc()[:2000]` and `list(tool_input.keys())` (keys only — avoid leaking PII/secrets) in the incident `details`.

2. `mojo/apps/assistant/services/agent.py` — Top-level + parallel safety net
   - Wrap the main loops in `run_assistant` and `run_assistant_ws` with an outer `try/except Exception as e` that emits a single `assistant:error:unhandled` (level 8) incident with `traceback.format_exc()[:2000]`, `user`, `conversation.pk`, and returns a user-facing error dict. Keeps the existing LLM-specific `_report_event` calls at [agent.py:1071-1089](mojo/apps/assistant/services/agent.py:1071) / [:1215-1227](mojo/apps/assistant/services/agent.py:1215); this is a net to catch anything else (e.g. an exception in `_execute_tools` outside the per-tool try).
   - Update parallel-tool failure at [agent.py:718](mojo/apps/assistant/services/agent.py:718) and plan-step failure at [:820](mojo/apps/assistant/services/agent.py:820) to also call `_report_event("assistant:error:parallel", 6, …)` — today they only `logger.exception`.

3. `mojo/serializers/core/serializer.py` — Investigate datetime leak (read-only in this step)
   - Grep `OptimizedGraphSerializer` for fields that emit raw `datetime`. `query_model` already routes through `queryset_to_dict`, so if datetimes leak through, the optimized serializer is suspect.
   - If the fix is a one-line `isoformat()`, apply it. If broader, file a follow-up issue and record the finding in the Investigation section of this file.

4. `tests/test_assistant/30_test_tool_error_reporting.py` — new
   - Register a stub tool returning `{"ts": timezone.now(), "amount": Decimal("1.00")}` → assert `tool_result.content` round-trips through `json.loads` with string values, no exception.
   - Register a stub tool that raises → monkeypatch `mojo.apps.incident.report_event`, assert it was called with category `assistant:error` and details containing the exception text.
   - Register a stub tool returning an unserializable sentinel (open socket / custom object without `__str__` override is fine) → assert fallback error payload returned AND `assistant:error:serialize` incident raised.
   - Force an exception inside `_execute_tools` outer loop (e.g. mock `_execute_tool` to raise) → assert `assistant:error:unhandled` incident raised and user-facing error returned.

### Design Decisions
- **Stdlib `json.dumps(default=…)` over `ujson`**: `ujson` has no `default=` hook. Performance cost is negligible at tool-turn frequency vs correctness.
- **Boundary helper over per-tool discipline**: individual tools should still prefer `queryset_to_dict`/`to_dict`, but the boundary must be bulletproof — tool authors won't remember every type.
- **Incident levels**: 7 for serialization failure, 6 for tool exception / parallel failure, 8 for unhandled agent-loop exception (hardest to diagnose).
- **Don't log `tool_input` values**: may contain user PII or query text. Log keys only in incident details.
- **Scope discipline**: framework-wide "every error → incident" sweep filed as a separate request (see follow-up below), not bundled here.

### Edge Cases
- `_report_event` itself raises → already swallowed at [agent.py:75](mojo/apps/assistant/services/agent.py:75); no double-fault risk.
- Oversized results / tracebacks → truncated to 2000 chars in `details`.
- Tool returns a raw Django `Model` instance → `_json_default` coerces via `to_dict()` (soft-correct) and we also raise an info-level incident so the offending tool can be fixed later.
- Naive vs aware datetimes → both handled by `.isoformat()`.

### Testing
- Datetime/Decimal round-trip → `tests/test_assistant/30_test_tool_error_reporting.py`
- Tool exception → incident raised → same file
- Serialization fallback + incident → same file
- Outer-loop unhandled exception → incident raised → same file
- Run with `bin/run_tests --agent -t test_assistant.30_test_tool_error_reporting`; read `var/test_failures.json` for diagnostics.

### Docs
- `CHANGELOG.md` — bullet for the bug fix + error-to-incident hardening.
- No `docs/django_developer/` or `docs/web_developer/` changes (internal hardening; no public API shift).

### Follow-ups (separate planning items)
- `planning/requests/framework-wide-error-to-incident-sweep.md` — broader audit that every framework-level exception raises an incident (not just assistant).
- Possible `planning/issues/optimized-serializer-datetime-leak.md` if step 3 confirms the serializer is the root source.

## Resolution

**Status**: resolved
**Date**: 2026-04-20

### What Was Built
Replaced the three `ujson.dumps(tool_result)` sites in the assistant agent with a new `_dumps_tool_result` helper that uses stdlib `json.dumps` + a `_json_default` fallback for datetime/date/Decimal/UUID/set/bytes. For Django Model instances, `_json_default` uses `MojoModel.to_dict()` — the RestMeta graph system already governs which fields are exposed, so sensitive fields (password hashes, tokens) are filtered by the default graph. Falls back to `{pk, model}` only when `to_dict()` is unavailable or raises. Serialization failures, tool-handler exceptions, parallel-tool / plan-step failures, and unhandled agent-loop exceptions all emit enriched incidents (new categories: `assistant:error:serialize` L7, `assistant:error:parallel` L6, `assistant:error:unhandled` L8). Tool-exception details now include traceback and `tool_input` keys (values excluded). Step 3 investigation: `OptimizedGraphSerializer` already uses `.isoformat()` when `SERIALIZE_DATETIME_TO_ISO` is set, so it was not the leak source — the fault was at the boundary.

### Files Changed
- `mojo/apps/assistant/services/agent.py` — `_json_default`, `_dumps_tool_result`, three `ujson.dumps` replacements, enriched incident reporting at the five error sites.
- `tests/test_assistant/30_test_tool_error_reporting.py` — new regression suite.
- `CHANGELOG.md` — v1.1.28 entry.
- `docs/django_developer/assistant/README.md` — three new incident categories in the Event Categories table.

### Tests
- `tests/test_assistant/30_test_tool_error_reporting.py` — 7 tests, all passing. Covers: datetime `_json_default`; Decimal/UUID/set coercion; `_dumps_tool_result` datetime round-trip; fallback + `assistant:error:serialize` incident on unserializable input; `_execute_tool` datetime round-trip; `_execute_tool` exception → incident with traceback + `input_keys`; Model instance soft-coercion without leaking `password`.
- Run: `bin/run_tests --agent -t test_assistant.30_test_tool_error_reporting`
- Full suite regression: 1773 tests, 1717 passed, 0 failed, 56 skipped (opt-in slow only).

### Docs Updated
- `CHANGELOG.md` — v1.1.28 bug fix entry.
- `docs/django_developer/assistant/README.md` — incident category table extended.
- No `docs/web_developer/` changes (internal hardening).

### Security Review
Concerns raised by security-review:
1. **Model data leakage via `to_dict()`** — reviewed and dismissed. `_json_default` uses `MojoModel.to_dict()` which is gated by the RestMeta graph system; the default graph already excludes sensitive fields (password hashes, tokens). Regression test asserts no `password` field appears when a User is accidentally returned.
2. **Exception-message leakage** — noted as a framework-wide concern (exception `repr` and traceback still appear in incident details) and carried into `planning/requests/framework-wide-error-to-incident-sweep.md` for the broader sweep.
3. **Incident flooding** — `_report_event` has no rate limiting. A deterministically failing tool can emit one incident per turn. Deferred to the framework-wide sweep.

### Follow-up
- `planning/requests/framework-wide-error-to-incident-sweep.md` — broader error-to-incident audit, incident rate limiting, exception-message sanitization convention.
