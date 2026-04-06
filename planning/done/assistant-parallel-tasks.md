# Assistant Parallel Tool Execution

**Type**: request
**Status**: resolved
**Date**: 2026-04-06
**Priority**: medium
**Depends on**: assistant-task-planning (for plan-aware batching in phase 2)

## Description

Speed up assistant responses by executing independent tool calls concurrently instead of serially. Today the agent loop processes tool calls one at a time — if the LLM requests 4 tools in a single turn, each waits for the previous to finish. With a `ThreadPoolExecutor`, they run concurrently within the same thread/job.

Phase 2 adds plan-aware batching: when the LLM creates a plan with parallel steps, the agent loop maps each step to its tool call and executes all parallel steps concurrently — even if the LLM would normally serialize them across turns.

## Pain Point

Complex requests like "audit security and check job health" result in 8-15 sequential tool calls. Each tool call is a DB query or API call taking 0.5-3 seconds. Total wall time: 15-30+ seconds of the user staring at "thinking..." The tools themselves are independent — there's no reason to wait for `query_incidents` to finish before starting `query_jobs`.

## Current Code

`agent.py` lines 428-431 — tool calls processed serially:

```python
# Process tool calls with permission gate
tool_results = []
for block in result["content"]:
    if block.get("type") != "tool_use":
        continue
    # ... permission check, execute, append result (one at a time)
```

Claude already supports returning multiple `tool_use` blocks in a single response. We just don't execute them concurrently.

## Phase 1: Parallel Tool Execution (no dependencies)

Replace the serial `for` loop with a `ThreadPoolExecutor` when multiple tool_use blocks are present in a single LLM response.

### Design

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

tool_blocks = [b for b in result["content"] if b.get("type") == "tool_use"]

if len(tool_blocks) == 1:
    # Single tool — execute inline, no thread overhead
    tool_results = [_execute_tool(tool_blocks[0], registry, user, ...)]
else:
    # Multiple tools — execute concurrently
    max_workers = settings.get("LLM_ADMIN_MAX_PARALLEL_TOOLS", 4, kind="int")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for block in tool_blocks:
            future = pool.submit(_execute_tool, block, registry, user, ...)
            futures[future] = block["id"]

        tool_results = []
        for future in as_completed(futures):
            tool_results.append(future.result())
```

Key details:
- `_execute_tool()` is extracted from the existing inline code — permission check, handler call, error handling, event reporting. Pure refactor, no behavior change.
- Results are collected in completion order but keyed by `tool_use_id` so order doesn't matter for the LLM response.
- WS `assistant_tool_call` events fire as each tool completes (already happens from the handler call).
- Single-tool turns skip the executor entirely — no thread overhead for the common case.
- Meta-tools (`load_tools`, `create_plan`, `update_plan`) that have side effects on the agent loop still work — their side effects are applied after all tools in the turn complete, same as today.

### System Prompt Addition

Encourage the LLM to batch independent calls:

```
## Parallel Execution
When you need data from multiple independent sources (e.g., incidents AND jobs AND users),
call all the tools in a single turn rather than one at a time. The system executes
concurrent tool calls in parallel for faster results. Only serialize when one tool's
result informs the next tool's input.
```

### What This Buys

If the LLM requests 4 tools in one turn and each takes 2 seconds:
- **Before**: 8 seconds (serial)
- **After**: ~2 seconds (parallel, bounded by slowest tool)

The limitation: this only helps when the LLM *chooses* to batch. Claude is often cautious and serializes across turns even when tools are independent. The system prompt nudge helps but isn't guaranteed. Phase 2 solves this.

## Phase 2: Plan-Aware Batching (depends on task-planning request)

When the LLM creates a plan with `parallel: true` steps, the agent loop takes control of execution order. Instead of letting the LLM call tools one turn at a time, the loop identifies the tool call each parallel step needs, executes them all concurrently, and feeds the combined results back to the LLM in a single turn.

### How It Works

```
LLM turn 1: create_plan(steps=[
    {description: "Check open incidents", parallel: true, tool: "query_incidents", tool_input: {status: "open"}},
    {description: "Review blocked IPs", parallel: true, tool: "query_blocked_ips", tool_input: {}},
    {description: "Check job failures", parallel: true, tool: "query_jobs", tool_input: {status: "failed", minutes: 1440}},
    {description: "Summarize findings", parallel: false}
])

Agent loop detects plan with parallel steps:
  → Executes query_incidents, query_blocked_ips, query_jobs concurrently via ThreadPoolExecutor
  → Updates plan steps to "done" with summaries
  → Pushes WS progress events for each completion
  → Injects all results into the next LLM turn as tool_results

LLM turn 2: (receives 3 tool results at once) → synthesizes final response
```

### Extended create_plan Schema

```json
{
  "title": "Security Audit (24h)",
  "steps": [
    {
      "description": "Check open incidents",
      "parallel": true,
      "tool": "query_incidents",
      "tool_input": {"status": "open", "minutes": 1440}
    },
    {
      "description": "Review blocked IPs",
      "parallel": true,
      "tool": "query_blocked_ips",
      "tool_input": {}
    },
    {
      "description": "Summarize findings",
      "parallel": false
    }
  ]
}
```

Each parallel step includes `tool` and `tool_input` — the exact tool call to make. The agent loop executes these directly without another LLM turn. Sequential steps (no `tool` field) are handled normally by the LLM in subsequent turns.

### Agent Loop Changes

After detecting `create_plan` with parallel steps:

1. Extract parallel steps that have `tool` + `tool_input`.
2. Permission-check each tool (same gate as normal execution).
3. Execute all via `ThreadPoolExecutor` — same `_execute_tool()` from phase 1.
4. For each completed tool: update plan step to `done`, push `assistant_plan_update` WS event, record summary.
5. Build `tool_results` array with all results, inject into messages as if the LLM had called them.
6. Continue the agent loop — LLM gets all results in one turn and synthesizes.

This is a ~40 line addition to the existing agent loop, not a new module. Extract to `_execute_parallel_steps(plan, registry, user, on_event)` helper shared between `run_assistant()` and `run_assistant_ws()`.

### What This Buys Over Phase 1

- **Guaranteed parallelism** — doesn't depend on the LLM choosing to batch. The plan declares independence, the loop enforces it.
- **Fewer LLM turns** — instead of 4 turns (one per tool), it's 2 turns (plan + synthesize). Saves both time and tokens.
- **Visible progress** — plan steps update in real time via WS events. The user sees "checking incidents... done. checking jobs... done." instead of silence.

### System Prompt Addition (Phase 2)

```
## Task Planning with Parallel Steps
For complex requests requiring 3+ independent queries, create a plan with parallel steps.
Include the tool name and input for each parallel step — the system will execute them
concurrently and return all results at once.

Parallel steps must be fully independent — no step should need another step's result.
The final synthesis step should be sequential (parallel: false, no tool field).
```

## Safeguards

- **Max concurrent tools**: `LLM_ADMIN_MAX_PARALLEL_TOOLS` (int, default 4). If more tools than this, batch in groups.
- **Per-tool timeout**: Tools already have their own timeouts. The `ThreadPoolExecutor` adds a global timeout via `future.result(timeout=30)`.
- **Error isolation**: One tool failing doesn't cancel others. Failed tools return error results to the LLM as today.
- **No mutating tools in parallel plan steps**: The agent loop rejects parallel steps that reference mutating tools — those must go through normal LLM turns with user confirmation (action blocks).
- **Meta-tool ordering**: If a turn includes both `load_tools` and a regular tool, `load_tools` executes first (serial), then remaining tools execute concurrently. Meta-tools are never parallelized with regular tools.

## Implementation Steps

### Phase 1

1. **`mojo/apps/assistant/services/agent.py`** — Extract `_execute_tool(block, registry, user, conversation, on_event)` from the inline tool execution code. Pure refactor — same permission check, handler call, error handling, event reporting.

2. **`mojo/apps/assistant/services/agent.py`** — Replace the serial tool loop with concurrent execution when `len(tool_blocks) > 1`. Use `ThreadPoolExecutor`. Apply in both `run_assistant()` and `run_assistant_ws()`.

3. **`mojo/apps/assistant/services/agent.py`** — Update system prompt with parallel execution guidance.

4. **Settings**: `LLM_ADMIN_MAX_PARALLEL_TOOLS` (int, default 4).

5. **Tests**:
   - `test_single_tool_no_threadpool` — single tool call executes inline, no executor
   - `test_multiple_tools_concurrent` — 3+ tools in one turn execute concurrently (timing assertion)
   - `test_tool_error_doesnt_cancel_others` — one tool failing, others still complete
   - `test_meta_tool_executes_first` — `load_tools` in a batch runs before other tools
   - `test_results_keyed_by_tool_use_id` — results match correct tool_use_ids regardless of completion order

### Phase 2

6. **`mojo/apps/assistant/services/tools/planning.py`** — Extend `create_plan` to accept `parallel`, `tool`, and `tool_input` fields per step.

7. **`mojo/apps/assistant/services/agent.py`** — Add `_execute_parallel_steps(plan, registry, user, conversation, on_event)` helper. Called after `create_plan` when parallel steps with tool fields are detected.

8. **`mojo/apps/assistant/services/agent.py`** — Update system prompt with plan-aware parallel guidance.

9. **Tests**:
   - `test_plan_parallel_steps_execute_concurrently` — parallel steps with tool fields run concurrently
   - `test_plan_sequential_step_waits` — sequential steps run after parallel steps complete
   - `test_plan_mutating_tool_rejected_in_parallel` — mutating tools in parallel steps return error
   - `test_plan_results_injected_as_tool_results` — LLM receives all parallel results in next turn
   - `test_plan_step_progress_events` — WS events fire as each parallel step completes
   - `test_simple_request_no_plan` — single-tool queries bypass planning entirely

### Docs

10. Update both doc tracks: parallel execution behavior, `LLM_ADMIN_MAX_PARALLEL_TOOLS` setting, plan-aware batching.

## Resolution

**Status**: resolved
**Date**: 2026-04-06

### What Was Built
Phase 1 (ThreadPoolExecutor for concurrent tool calls) and Phase 2 (plan-aware parallel step execution) — both implemented in the existing agent loop with no new modules or Redis coordination.

### Files Changed
- `mojo/apps/assistant/services/agent.py` — _execute_tool(), _execute_tools(), _execute_parallel_plan_steps(), _summarize_tool_result(), META_TOOLS ordering, system prompt parallel guidance
- `docs/django_developer/assistant/README.md` — Parallel execution section, LLM_ADMIN_MAX_PARALLEL_TOOLS setting
- `docs/web_developer/assistant/README.md` — Plan tracker, WS events for step progress
- `CHANGELOG.md` — v1.1.14 entry

### Tests
- `tests/test_assistant/18_test_parallel_tools.py` — 10 tests covering single/multi tool execution, meta-tool ordering, error isolation, parallel plan steps, skip logic, result summarization
- Run: `bin/run_tests --agent -t test_assistant.18_test_parallel_tools`

### Security Review
Thread safety: each tool call gets its own DB connection via Django's connection-per-thread model. No shared mutable state between concurrent tools.

### Follow-up
None
