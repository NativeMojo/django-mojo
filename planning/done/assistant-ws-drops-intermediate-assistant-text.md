# Assistant WS stream omits intermediate assistant text turns

**Type**: bug
**Status**: resolved
**Date**: 2026-04-25
**Severity**: high

## Description

When the assistant produces a multi-turn response (assistant text → tool calls → tool results → assistant text → tool calls → … → final assistant text), only the **final** assistant turn is emitted to the client over WebSocket via `assistant_response`. Any intermediate assistant turn that interleaves prose with a `tool_use` block is **never sent to the client** — its text is persisted in the `Message` row but has no transport on the realtime stream.

Result: a live user sees the assistant call tools and then sees a closing wrap-up, but never sees the analysis the model wrote in between. Refreshing the page (which goes through the REST history endpoint) reloads the conversation and the missing turn appears as an additional bubble. From the user's perspective, "the assistant only tells me the answer if I refresh."

### Concrete trace from conversation 34

The user asked: *"look at the ip for the login:unknown, has anyone else logged in from this ip?"*

The REST history (`/api/assistant/conversations/34?graph=detail`) contains messages 1506–1517. The substantive answer lives in message **1515**:

```json
{
  "id": 1515, "role": "assistant", "content": "",
  "tool_calls": [
    { "type": "text",
      "text": "Both are totally benign — just users who mistyped their username:\n\n**172.58.128.51 — \"0300dj@gmail.com\"** ..." },
    { "type": "tool_use", "name": "bulk_update_incidents", "input": { ... } }
  ]
}
```

Message **1517** is the closing wrap-up: *"Done. The queue is now fully cleared. Here's the final tally for today's session..."*

The WebSocket events for the same request were:

```
assistant_thinking
assistant_tool_call × 8   (one per tool from messages 1507, 1509, 1511, 1513, 1515)
assistant_response        (carries content from 1517 only; tool_calls_made flat list)
```

Notice what is **not** present: any event carrying the text from message 1515. The frontend has no event type that conveys it — `assistant_tool_call` has no `text` field, and `assistant_response` only fires once at the end.

## Context

- **User-facing impact:** the live assistant chat looks like it skipped the explanation. Users only get the analysis after a manual refresh.
- **Where the gap is in the code:** [`mojo/apps/assistant/services/agent.py:1259-1326`](mojo/apps/assistant/services/agent.py:1259) — the `run_assistant_ws` turn loop. On each `tool_use` stop (line 1284), the code stores the assistant turn:
  ```python
  Message.objects.create(
      conversation=conversation, role="assistant",
      content="", tool_calls=result["content"],
  )
  ```
  The `result["content"]` array can contain `text`-type blocks alongside the `tool_use` blocks (Anthropic returns both when the model thinks aloud before calling a tool). Those `text` blocks are persisted but never passed to `on_event`. The only `on_event` calls during the loop are inside `_execute_tool` (which emits `assistant_tool_call` per tool — see line 700-ish).
- **Where the WS emit goes:** [`mojo/apps/assistant/handler.py:200-278`](mojo/apps/assistant/handler.py:200) — `_run_agent_thread` only emits one `assistant_response` at the very end with the result dict from `run_assistant_ws`, which only carries the final `response` text.

## Acceptance Criteria

- For every assistant message row created in `run_assistant_ws`, any `text`-type content (from `Message.content` or from `text` blocks inside `Message.tool_calls`) is delivered to the client in real time before the next event in the stream.
- Mechanism is consistent: either reuse `assistant_response` (firing once per assistant turn rather than once per request) and let the client distinguish intermediate vs final, **or** add a new `assistant_intermediate_text` event type — pick one and apply it uniformly. The client should not need to reconstruct turn boundaries from the flat `assistant_tool_call` stream.
- The final wrap-up event still fires (preserves the existing `tool_calls_made`/`duration_ms` summary the client uses to re-enable input and clear the thinking indicator).
- Markdown blocks (`assistant_block` fences) inside intermediate text are parsed the same way as final-turn text — `_parse_blocks` should run on intermediate text too.
- The change does not duplicate text in the historical view: `_build_conversation_messages` and the REST `?graph=detail` fetch must continue to produce a non-redundant transcript when the new event format is in use.
- Reliability contract from `_run_agent_thread` is preserved: every code path still ends with either an `assistant_response` or `assistant_error`.

## Investigation

- **Primary root cause:** the WS turn loop in `run_assistant_ws` discards `text`-type blocks from intermediate assistant turns. The Anthropic content array `[{type:"text", text:...}, {type:"tool_use", ...}]` is stored verbatim into `Message.tool_calls`, but only the trailing `tool_use` blocks trigger client-visible WS events (via `_execute_tool`'s `assistant_tool_call`). The `text` block is dropped on the wire.
- **Why it surfaced now:** when the assistant is doing simple "look up X then answer" requests, the model usually emits its prose only on the final turn (no `text` blocks alongside intermediate `tool_use`). With more complex multi-turn workflows where the model summarizes findings *before* taking an action (as in conversation 34: investigate IPs → summarize → bulk-update incidents), the prose ends up on an intermediate turn and gets lost.
- **Confidence:** high. Cross-checked WS trace against REST history sample. Code path in `agent.py:1289-1296` confirms the text is stored but not forwarded.
- **Code path:**
  - [`mojo/apps/assistant/services/agent.py:1259-1296`](mojo/apps/assistant/services/agent.py:1259) — turn loop; `result["content"]` can include `text` blocks that go unforwarded
  - [`mojo/apps/assistant/services/agent.py:1264-1282`](mojo/apps/assistant/services/agent.py:1264) — only the *terminal* turn calls back with text via the returned dict
  - [`mojo/apps/assistant/handler.py:200-278`](mojo/apps/assistant/handler.py:200) — `_run_agent_thread`; single trailing `on_event("response", ...)`
  - [`mojo/apps/assistant/handler.py:32-54`](mojo/apps/assistant/handler.py:32) — `_send_ws_event`; the existing event publish helper (any new event type rides through here)
- **Related design doc:** `mojo/apps/assistant/handler.py:8-21` documents the current event contract; that comment block needs to be updated to reflect whichever fix is chosen.
- **Companion frontend bug:** `web-mojo` has a parallel issue documented at `planning/issues/assistant-tool-calls-dropped-from-ws-response.md` — the client also drops the `tool_calls_made` array from `assistant_response` because of a shape filter mismatch. That fix can land independently, but the design here should consider whether `tool_calls_made`'s shape (`{tool, input}`) should be normalized to the Anthropic `{type:"tool_use", name, input}` shape at the same time, so the client only has to handle one shape.
- **Regression test:** feasible — `tests/test_assistant/3_test_live_assistant.py` and `tests/test_assistant/18_test_parallel_tools.py` already exercise the WS path and capture `on_event` calls. Add a test that constructs a multi-turn response with intermediate `text` + `tool_use` content and asserts the captured event sequence includes the intermediate text.
- **Related files:**
  - `mojo/apps/assistant/services/agent.py`
  - `mojo/apps/assistant/handler.py`
  - `mojo/apps/assistant/rest/assistant.py`
  - `mojo/apps/assistant/models.py` (Message schema for `tool_calls` JSONField)
  - `tests/test_assistant/3_test_live_assistant.py`
  - `tests/test_assistant/18_test_parallel_tools.py`
  - `tests/test_assistant/30_test_tool_error_reporting.py`

## Plan

**Status**: planned
**Planned**: 2026-04-25

### Objective
Stream every assistant text turn (intermediate or final) over WebSocket so the live UI sees the same prose the REST history shows, with `assistant_block` fences parsed consistently and `Message` rows persisted in a clean shape.

### Steps

1. `mojo/apps/assistant/services/agent.py` (lines 1284–1296, WS path):
   - After `llm.call` returns and before `_execute_tools`, split `result["content"]` into text blocks vs tool_use blocks.
   - If text blocks exist, join them, run `_parse_blocks` for fence extraction, and call `on_event("text", {"text": cleaned, "blocks": blocks or None})`.
   - Change the assistant turn `Message.objects.create` to: `content=cleaned_text`, `blocks=blocks or None`, `tool_calls=[only tool_use blocks]` instead of full `result["content"]`. Empty text → empty `content`, no `on_event` call.

2. `mojo/apps/assistant/services/agent.py` (lines 1108–1129, REST `run_assistant` path):
   - Apply the same persistence cleanup so REST and WS produce identical `Message` shapes. No event emit since REST is synchronous.

3. `mojo/apps/assistant/handler.py` (docstring lines 8–21):
   - Add `assistant_text` to the response-types list with payload contract `{conversation_id, text, blocks}`. No code change — `_send_ws_event` already handles arbitrary event types.

4. `tests/test_assistant/31_test_intermediate_text_stream.py` (new):
   - Mock `llm.call` to return a 2-turn sequence: turn 1 = `[text + tool_use]`, turn 2 = `[text]` (final).
   - Drive `run_assistant_ws` with a captured `on_event` callback; assert event sequence: `assistant_text` → `assistant_tool_call` → returned dict carries final text (which `_run_agent_thread` emits as `assistant_response`).
   - Assert persisted `Message` row for the intermediate turn has non-empty `content`, populated `blocks` for fenced JSON, and only `tool_use` blocks in `tool_calls`.

5. `tests/test_assistant/3_test_live_assistant.py`:
   - Extend `_ws_collect_assistant_events` (lines 48–74) to also collect `assistant_text` events so existing live tests don't regress when the new event type appears in the stream.

6. `docs/web_developer/` (verify exact path during build):
   - Document the new `assistant_text` event with payload `{conversation_id, text, blocks}` and emit-order contract.

7. `CHANGELOG.md`:
   - Entry: new `assistant_text` WS event for intermediate assistant prose; `Message.tool_calls` no longer carries text blocks (now in `Message.content` / `Message.blocks`).

### Design Decisions

- **New event type `assistant_text` (not overload `assistant_response`)**: keeps the terminal contract clean — frontend uses `assistant_response` to clear thinking indicator and re-enable input. Adding an `is_final` flag to `assistant_response` would force every existing client into branching logic. Naming aligns with `assistant_thinking` / `assistant_tool_call` / `assistant_response`. Old clients ignore unknown events and keep working.
- **Emit text BEFORE tool_call events for the same turn**: matches Anthropic's content order (model reasons aloud, then calls tools) and the chronological reading order users expect.
- **Clean persisted shape (text → `Message.content` + `Message.blocks`, only tool_use → `Message.tool_calls`)**: REST `?graph=detail` and WS stream return the same shape — no client-side reconstruction. Old conversations remain readable since the frontend already extracts text from `tool_calls` (proven by the "refresh shows missing turn" workaround).
- **`tool_calls_made` shape normalization deferred**: the companion `web-mojo` issue handles the client-side fix independently. Changing the wire shape here would require coordinated frontend release.

### Edge Cases

- **Empty intermediate text** (turn has only tool_use blocks): skip `on_event`, store empty `content` — no spurious event.
- **`text` block appearing AFTER `tool_use` in a turn**: rare per Anthropic spec but valid. Aggregate all `text` blocks into one event before tool_call events to preserve a coherent reading order.
- **Old conversations** with text-buried-in-`tool_calls`: history fetch still renders correctly via existing frontend extraction. Not breaking back-compat for read; new turns get the cleaner shape.
- **Plan-step parallel injection** (lines 1306–1311): synthetic blocks are tool_use only — no extra `assistant_text` events triggered.
- **Reliability contract preserved**: every WS path still ends with `assistant_response` or `assistant_error`. New emit goes through `_send_ws_event` which never raises.
- **web-mojo coupling**: requires a parallel handler update in `web-mojo` to render the new `assistant_text` event. Plan landing requires confirmation that web-mojo handles it correctly (frontend can append a new bubble per `assistant_text`, then update the trailing bubble on `assistant_response`). If web-mojo is not updated, intermediate text is silently ignored — same observable behavior as today, no regression.

### Testing

- New event sequence with intermediate text → `tests/test_assistant/31_test_intermediate_text_stream.py`
- Block parsing on intermediate text → `tests/test_assistant/31_test_intermediate_text_stream.py`
- Persisted `Message` shape (content + blocks + cleaned tool_calls) → `tests/test_assistant/31_test_intermediate_text_stream.py`
- No regression in single-turn / final-only flow → existing `tests/test_assistant/3_test_live_assistant.py`
- No spurious events from parallel plan steps → existing `tests/test_assistant/18_test_parallel_tools.py`
- REST history unchanged in shape (cleaner) → existing `tests/test_assistant/2_test_conversations.py` (verify, may need extension)

### Docs

- `mojo/apps/assistant/handler.py` — docstring response-types list (lines 8–21)
- `docs/web_developer/` — new `assistant_text` event documented (path verified during build)
- `CHANGELOG.md` — new event + persistence shape note


## Resolution

**Status**: resolved
**Date**: 2026-04-25

### What Was Built
Added a new `assistant_text` WebSocket event that fires before each turn's `assistant_tool_call` events whenever the model produces prose alongside `tool_use` blocks in the same turn. Cleaned up the persisted `Message` row shape so intermediate text lives in `Message.content` (with parsed `assistant_block` fences in `Message.blocks`) instead of being buried inside `Message.tool_calls`. Applied identically to both the WS path (`run_assistant_ws`) and REST path (`run_assistant`) so REST `?graph=detail` history matches the WS stream shape going forward.

### Files Changed
- `mojo/apps/assistant/services/agent.py` — split `result["content"]` into text vs tool_use blocks at each tool turn; emit `on_event("text", {text, blocks})` before tool execution; persist text+blocks on the assistant Message and only tool_use blocks in `tool_calls`. Same pattern in WS and REST paths.
- `mojo/apps/assistant/handler.py` — docstring response-types list updated with `assistant_text`, `assistant_plan`, `assistant_plan_update`.
- `tests/test_assistant/3_test_live_assistant.py` — `_ws_collect_assistant_events` now consumes `assistant_text` events without terminating the loop.
- `tests/test_assistant/31_test_intermediate_text_stream.py` — new file, 5 tests.
- `docs/web_developer/assistant/README.md` — new event documented in event table, payload table, client wiring example, and lifecycle description.
- `CHANGELOG.md` — Fixed entry under Unreleased.

### Tests
- `tests/test_assistant/31_test_intermediate_text_stream.py` — covers:
  - intermediate `assistant_text` event fires before `assistant_tool_call`
  - text persists on `Message.content`, only tool_use in `Message.tool_calls`
  - `assistant_block` fences inside intermediate text parse into `Message.blocks` and event payload
  - empty intermediate text does NOT fire a spurious event
  - terminal `assistant_response` return value still includes `message_id` and final text
- Run: `bin/run_tests --agent -t test_assistant.31_test_intermediate_text_stream`
- Full module regression: `bin/run_tests --agent -t test_assistant` → 507 passed, 0 failed, 17 skipped (live API tests)

### Docs Updated
- `docs/web_developer/assistant/README.md` — event table, payload spec, lifecycle, client wiring example
- `mojo/apps/assistant/handler.py` — docstring response-types list
- `CHANGELOG.md` — Unreleased Fixed entry

### Security Review
No new permission boundaries or data flows. The new event publishes via the same `_send_ws_event` → `send_event_to_user` path used by every other assistant event, scoped to the requesting user. Persisted text is the same content already stored under the previous shape, just relocated within the `Message` row.

### Follow-up
- web-mojo client must add an `assistant_text` handler that appends a new assistant bubble. Until web-mojo ships the handler, intermediate text is silently ignored on the wire — same observable behavior as before this fix, no regression.
- Companion frontend `tool_calls_made` shape mismatch tracked separately in web-mojo's `planning/issues/assistant-tool-calls-dropped-from-ws-response.md`.
