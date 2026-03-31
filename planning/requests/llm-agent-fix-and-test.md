# Fix LLM agent job signatures, use Anthropic SDK, add mocked flow tests

**Type**: request
**Status**: planned
**Date**: 2026-03-30

## Description

The LLM security agent (`llm_agent.py`) has three problems:

1. **Same job payload bug we just fixed** — `execute_llm_handler(payload)` and `execute_llm_ticket_reply(payload)` call `payload.get(...)` but the job engine passes a `Job` instance. Both are broken in production (same root cause as execute-handler-job-payload.md).

2. **Raw httpx instead of Anthropic SDK** — `_call_claude()` makes raw HTTP calls with hand-rolled headers. The `anthropic` Python SDK handles retries, error types, versioning, and is the supported client.

3. **No tests** — The agent loop, tool dispatch, prompt building, and DB side effects are completely untested. Since `th.run_pending_jobs()` runs job functions in the test process (not the server), `mock.patch` works here. We can mock `_call_claude` to return scripted tool_use responses while all tool implementations (DB queries, blocks, tickets, incident updates) run for real.

## Acceptance Criteria

### 1. Fix job payload signatures
- `execute_llm_handler(job)` reads `job.payload` (not `payload.get(...)`)
- `execute_llm_ticket_reply(job)` reads `job.payload` (not `payload.get(...)`)
- Both work when dispatched via the job engine

### 2. Replace httpx with anthropic SDK
- Add `anthropic` to pyproject.toml dependencies
- Replace `_call_claude()` raw httpx with `anthropic.Anthropic().messages.create()`
- Remove httpx import from llm_agent.py
- `_call_claude()` returns the same shape used by `_run_agent_loop` (the SDK response object has `.content`, `.stop_reason` etc — adapt the loop to use SDK types or convert to dict)

### 3. Mocked agent flow tests
- Test the full pipeline: `jobs.publish()` → `th.run_pending_jobs()` → mock `_call_claude` → real tool dispatch → assert DB side effects
- Mock returns scripted Claude API responses with tool_use blocks
- Test scenarios:
  - **Investigate and ignore**: agent queries events, queries IP, updates incident to "ignored"
  - **Investigate and block**: agent queries events, blocks IP, updates incident to "resolved"
  - **Investigate and create ticket**: agent queries events, creates ticket, incident stays "investigating"
- Each test asserts real DB state: incident status, GeoLocatedIP.is_blocked, Ticket created, IncidentHistory entries

## Related Files

- `mojo/apps/incident/handlers/llm_agent.py` — main changes (signatures, SDK, _call_claude)
- `mojo/apps/incident/models/ticket.py:94` — publishes `execute_llm_ticket_reply` job
- `mojo/apps/incident/handlers/event_handlers.py:446` — publishes `execute_llm_handler` job
- `mojo/apps/incident/models/event.py:273` — also publishes `execute_llm_handler` job
- `pyproject.toml` — add `anthropic` dependency
- `tests/test_incident/llm_agent.py` — new test file

## Plan

### Step 1: Fix job payload signatures in llm_agent.py

```python
def execute_llm_handler(job):
    payload = job.payload
    # ... rest unchanged

def execute_llm_ticket_reply(job):
    payload = job.payload
    # ... rest unchanged
```

Also update the docstrings and module header (lines 9-10) to reflect `job` not `payload`.

### Step 2: Add anthropic SDK dependency

Add `"anthropic>=0.52.0"` to pyproject.toml dependencies. Run `bin/create_testproject` to sync.

### Step 3: Replace _call_claude with SDK

```python
def _call_claude(messages, system_prompt):
    """Call Claude API with tool use. Returns the response as a dict."""
    import anthropic

    client = anthropic.Anthropic(api_key=_get_llm_api_key())
    response = client.messages.create(
        model=_get_llm_model(),
        max_tokens=4096,
        system=system_prompt,
        tools=TOOLS,
        messages=messages,
    )
    # Convert to dict for compatibility with _run_agent_loop
    return response.model_dump()
```

Remove the `httpx` import. The agent loop code that reads `result["content"]`, `result["stop_reason"]`, `block["type"]`, `block["name"]`, `block["input"]`, `block["id"]` stays the same since `model_dump()` produces the same dict shape.

### Step 4: Write mocked agent flow tests

Create `tests/test_incident/llm_agent.py` with:

**Helper**: `_mock_claude_response(stop_reason, content_blocks)` — builds a dict matching the Claude API response shape.

**Test 1: `test_llm_agent_investigate_and_ignore`**
- Create Event + Incident + RuleSet
- Mock `_call_claude` side_effect returns:
  - Turn 1: tool_use `query_events` + `query_ip_history` → real DB queries run
  - Turn 2: tool_use `update_incident(status="ignored", note="...")` → real DB write
  - Turn 3: end_turn with text summary
- Publish job via `jobs.publish("...execute_llm_handler", ...)`
- Run `th.run_pending_jobs()`
- Assert: incident.status == "ignored", IncidentHistory has LLM entries

**Test 2: `test_llm_agent_investigate_and_block`**
- Mock returns: query_events → block_ip + update_incident(resolved) → end_turn
- Assert: GeoLocatedIP.is_blocked == True, incident.status == "resolved"

**Test 3: `test_llm_agent_create_ticket`**
- Mock returns: query_events → create_ticket → end_turn
- Assert: Ticket exists with llm_linked metadata, incident stays "investigating"

**Test 4: `test_llm_ticket_reply`**
- Create Ticket + TicketNote (human reply), publish execute_llm_ticket_reply job
- Mock returns: end_turn with text
- Assert: new TicketNote created with "[LLM Agent]" prefix

All tests use `from unittest.mock import patch` on `mojo.apps.incident.handlers.llm_agent._call_claude`.

### Step 5: Run tests, commit
