# Assistant Prompt Caching

**Type**: request
**Status**: resolved
**Date**: 2026-05-25
**Priority**: medium

## Description

Enable Anthropic prompt caching on the LLM calls made by the assistant agent loop. The assistant currently sends the same large prefix (system prompt + tool definitions + prior message history) on every turn with no `cache_control` field, so every turn re-processes that prefix at full base input price. Adding automatic caching at the top level of the request reduces cost on the cached portion to ~10% of base price and shortens time-to-first-token on every turn after the first.

Two changes:

1. Pass `cache_control={"type": "ephemeral"}` at the top level of `client.messages.create(...)` in [mojo/helpers/llm.py:245](mojo/helpers/llm.py:245).
2. Surface `cache_creation_input_tokens` / `cache_read_input_tokens` / `input_tokens` from `response.usage` to callers, persist them on the `Message` row written for the final assistant turn, and log them on a per-call basis so we can verify the cache is actually being hit.

## Context

Discovered while auditing the assistant app for caching opportunities. The architecture is a near-textbook case for prompt caching:

- **Tight agent loop**: [agent.py:1189](mojo/apps/assistant/services/agent.py:1189) and [agent.py:1401](mojo/apps/assistant/services/agent.py:1401) loop up to `LLM_ADMIN_MAX_TURNS=25` per user message, each call passing the same system + tools + growing message history.
- **Large static prefix**: `SYSTEM_PROMPT` at [agent.py:382](mojo/apps/assistant/services/agent.py:382) is ~6K tokens before skill catalog and memory injection. Add the full tool schema list from `get_core_tools_for_user()` / `get_domain_tools_for_user()` and the prefix is comfortably above the 1024-token minimum required for Sonnet caching (4096 for Opus).
- **Long history**: [_build_conversation_messages](mojo/apps/assistant/services/agent.py:1075) reloads up to `LLM_ADMIN_MAX_HISTORY=50` prior messages from the DB on every request — all of which would benefit from incremental caching turn-over-turn.

Expected impact: 5–10x cost reduction on long agent runs (each turn after the first reads ~90% of input from cache at 10% price), plus lower time-to-first-token on every follow-up turn. The improvement compounds for users with active multi-turn conversations.

## Acceptance Criteria

- `llm.call(...)` in [mojo/helpers/llm.py:245](mojo/helpers/llm.py:245) sends `cache_control={"type": "ephemeral"}` at the top level of the Anthropic request. Behaviour change is gated by a settings flag (default on) so it can be disabled without code edits.
- `llm.call(...)` returns the `usage` dict from the Anthropic response in addition to the existing content/stop_reason fields, so callers can read `cache_creation_input_tokens` and `cache_read_input_tokens` without re-fetching.
- The agent loop in `run_assistant()` and `run_assistant_ws()` accumulates per-turn cache usage and writes the totals to the final assistant `Message` row (new JSON field, e.g. `usage` on `Message`).
- Per-turn cache usage is logged at INFO level to `assistant.log` (`cache_read_tokens`, `cache_write_tokens`, `uncached_input_tokens`, `output_tokens`, conversation id) so operators can monitor cache effectiveness.
- A first turn on a fresh conversation shows non-zero `cache_creation_input_tokens` and zero reads; a follow-up turn within 5 minutes shows non-zero `cache_read_input_tokens` covering at least the system + tools prefix.
- The change does NOT alter the response shape or content returned to the caller — only adds the cache hint and surfaces usage. Existing tests in `test_assistant/*` continue to pass.
- Docs updated in both tracks describing the new behaviour, the new setting, and the new `usage` field on Messages.
- `CHANGELOG.md` entry.

## Investigation

**What exists**:
- `mojo/helpers/llm.py` — single API call site (`call()`) used by both the assistant agent loop and one-shot helpers like `llm.ask()`. Already discovers models via `/v1/models` and caches results for 24h.
- `mojo/apps/assistant/services/agent.py` — agent loop in `run_assistant()` (HTTP) and `run_assistant_ws()` (WebSocket). Two parallel implementations that both call `llm.call(messages, system=system_prompt, tools=tools)` on every turn.
- `_get_system_prompt(user, group)` ([agent.py:579](mojo/apps/assistant/services/agent.py:579)) — builds the system prompt with `{skill_catalog}` and the memory section injected. Stable within a single agent loop iteration but changes between conversations / when memories or skills change.
- `_build_tools_for_conversation()` ([agent.py:616](mojo/apps/assistant/services/agent.py:616)) — core tools by default, augmented with domain tools when `load_tools` fires mid-conversation.
- `Message.duration_ms` ([conversation.py:57](mojo/apps/assistant/models/conversation.py:57)) — existing per-turn metric, set on the final assistant message. No token accounting yet.
- `llm.ask()` ([llm.py:275](mojo/helpers/llm.py:275)) — one-shot wrapper used by [memory.py:704](mojo/apps/assistant/services/memory.py:704) for memory dreaming. Goes through the same `llm.call()` path.

**What changes**:
- `mojo/helpers/llm.py` — `call()` adds `cache_control` kwarg to the Anthropic request when `LLM_ADMIN_PROMPT_CACHE_ENABLED` is true. Returns `usage` in the dict result. No signature change for existing callers (they ignore the extra key).
- `mojo/apps/assistant/services/agent.py` — both `run_assistant()` and `run_assistant_ws()` accumulate per-turn `usage` across the agent loop and pass the totals into the final `Message.objects.create(...)` call. Log per-turn usage to `assistant.log`.
- `mojo/apps/assistant/models/conversation.py` — add `usage = models.JSONField(null=True, blank=True, default=None)` to `Message`. Add to `default` graph so the frontend can display it. Run `bin/create_testproject` to regenerate the migration.
- `docs/django_developer/assistant/README.md` — section on prompt caching: behaviour, when it invalidates, the new setting, the new `Message.usage` field.
- `docs/web_developer/assistant/README.md` — note that `Message` now includes a `usage` dict for diagnostic display.
- `CHANGELOG.md` — entry.

**Constraints**:
- Caching is an internal optimization — must not alter response content, ordering, or `tool_use` semantics.
- Must remain disable-able for debugging or if Anthropic-side issues appear.
- `llm.ask()` already uses `call()` under the hood — the change applies to it too. Memory-dream one-shots are short prompts (likely below the 1024-token Sonnet minimum), so caching is a no-op for them. Confirm via the `cache_creation_input_tokens == 0 and cache_read_input_tokens == 0` indicator in the response.

**Related files**:
- `mojo/helpers/llm.py`
- `mojo/apps/assistant/services/agent.py`
- `mojo/apps/assistant/services/memory.py` (calls `llm.ask`)
- `mojo/apps/assistant/models/conversation.py`
- `mojo/apps/assistant/__init__.py` (tool list builders — context only, no change)
- `docs/django_developer/assistant/README.md`
- `docs/web_developer/assistant/README.md`
- `tests/test_assistant/` (add new test file)

## Potential Downsides

These are the known trade-offs. None are blockers — most are inherent to the prompt-caching feature and have well-understood mitigations.

### 1. Tool-array changes mid-conversation invalidate the entire cache

The Anthropic cache hierarchy is `tools → system → messages`. A change at any level invalidates that level and everything below. The assistant's `load_tools` tool ([agent.py:656](mojo/apps/assistant/services/agent.py:656)) mutates the active tools list mid-conversation — when this fires, the next turn's tools array is different, so the entire cached prefix is invalidated and rewritten at the 25% premium.

**Mitigation**: Inherent to the feature. Worst case the `load_tools` turn pays one cache-write premium, then the next 24 turns of the agent loop read from the new cache. Net cost is still lower than no caching. Document this so operators understand a "tool load" turn looks more expensive than its neighbours.

### 2. Dynamic system prompt invalidates the system cache

`_get_system_prompt()` injects the skill catalog and the memory section into the prompt. Any mid-conversation `save_skill`, `update_skill`, `delete_skill`, or `write_memory` / `update_memory` / `delete_memory` call rebuilds the system prompt on the next turn — different bytes → cache miss for the system and messages segments. Tools still hit cache because they're earlier in the hierarchy and didn't change.

**Mitigation**: Same as above — the next turn pays a cache-write premium, then subsequent turns hit cache. Skill and memory mutations are infrequent compared to read turns, so the amortized win is still large. Document this. If real-world telemetry shows this is the dominant invalidator we can later move skill/memory into the first user message (so they don't break the system cache) — out of scope for v1.

### 3. 25% write premium on cache misses

Every cache write costs 1.25x base input price (5-minute TTL). For a one-shot conversation that ends after the first turn, caching is a net loss of ~25% on input tokens. Conversations that go more than 2 turns are already net-positive (second turn's read at 10% saves more than the first turn's write premium cost). The 25-turn agent loop guarantees overwhelming net-positive on any non-trivial request.

**Mitigation**: Accept it. The single-turn-only case is rare in practice — most user messages trigger at least 2-3 agent loop turns (model response → tool call → tool result → final response). If telemetry shows a large population of 1-turn conversations, we can add a conditional skip later.

### 4. `usage.input_tokens` semantic shift

With caching enabled, `usage.input_tokens` represents only the tokens **after the last cache breakpoint**, not the total. Total input = `cache_read_input_tokens + cache_creation_input_tokens + input_tokens`. Any external system that consumes `input_tokens` as "total input" will now under-count.

**Mitigation**: We persist all three fields in `Message.usage` so callers can sum them. Today nothing in the codebase reads `input_tokens` from the Anthropic response (we don't even surface `usage` from `llm.call()` yet), so there is no existing consumer to break. Document the breakdown in the django_developer doc and provide a helper if a sum is needed.

### 5. Minimum cacheable prefix

Sonnet caching requires ≥1024 tokens of prefix; Opus requires ≥4096. Below that, `cache_control` is silently ignored (no error, both usage counters return 0). The current `SYSTEM_PROMPT` + tools easily clears 1024 tokens but may sit near 4096 — if someone runs the assistant on Opus with `LLM_ADMIN_SYSTEM_PROMPT` overridden to something tiny, caching becomes a no-op.

**Mitigation**: Document the minimums. Log a one-time warning when a first-turn response returns both counters at 0 despite caching being enabled, so operators know their config produced no cache.

### 6. Privacy / data retention

Per Anthropic docs, prompt caching is ZDR-eligible. KV-cache representations are in-memory only, never stored at rest, expire after the TTL, and are isolated per organization (and per workspace as of 2026-02-05 on the Claude API). No new exposure surface.

**Mitigation**: Nothing required. Note this in the docs to forestall the inevitable question.

### 7. Cache hit not deterministic for concurrent requests

A cache entry only becomes available after the first response begins. If two WebSocket sessions for the same user fire the same turn within milliseconds of each other, both could end up as cache writes. Unlikely in practice (single user, sequential agent turns).

**Mitigation**: Accept it. Negligible cost.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `LLM_ADMIN_PROMPT_CACHE_ENABLED` | `True` | Master switch for `cache_control` injection in `llm.call()`. Set `False` to disable without code changes. |

## Tests Required

- `test_llm_helper_sets_cache_control_when_enabled` — `llm.call()` includes `cache_control={"type":"ephemeral"}` in the Anthropic request when setting is True. Mock the Anthropic client.
- `test_llm_helper_omits_cache_control_when_disabled` — setting False → `cache_control` not in kwargs.
- `test_llm_helper_returns_usage` — `call()` result includes a `usage` dict with `cache_creation_input_tokens`, `cache_read_input_tokens`, `input_tokens`, `output_tokens`.
- `test_assistant_persists_usage_on_final_message` — after a multi-turn agent loop, the final assistant `Message.usage` row is populated with summed `cache_read` / `cache_write` / `input` / `output` tokens across all turns. Mock `llm.call()` to return canned usage per turn.
- `test_assistant_logs_per_turn_cache_usage` — `assistant.log` receives an INFO line per turn with the breakdown. Capture via testit log fixture.
- `test_llm_ask_passes_through_cache_setting` — `llm.ask()` (one-shot) goes through `call()` and inherits the cache setting. Below the cache minimum, both counters return 0 — confirm we handle that gracefully (no log spam beyond the one-time warning).
- `test_message_usage_in_default_graph` — `Message.to_dict()` exposes the new `usage` field so it flows out via REST to the frontend.

## Out of Scope

- Switching to explicit `cache_control` breakpoints with multiple cache segments (tools / system / messages cached separately). Automatic caching is sufficient for v1; explicit breakpoints become worthwhile only if telemetry shows skill/memory mutation is the dominant invalidator.
- Moving skill catalog or memory section out of the system prompt into the first user message to insulate the system cache from skill/memory edits. Defer until v1 telemetry justifies it.
- 1-hour TTL caching (`ttl: "1h"`, 2x write premium). The 5-min default fits the active-agent-loop pattern. Revisit if we see a workload where users idle 5-60 minutes between turns.
- Pre-warming the cache (`max_tokens: 0` warm-up requests). Not relevant — the assistant only runs in response to user input.
- Caching for `llm.ask()` memory dreams as a separate optimisation. They piggyback on the same `call()` change; if they're below the minimum they're a no-op, which is correct.
- Per-user / per-conversation cost dashboards built on the new `Message.usage` data. The data will be available; building dashboards is a separate request.

## Plan

**Status**: planned
**Planned**: 2026-05-25

### Objective
Inject `cache_control={"type": "ephemeral"}` into the Anthropic request in [llm.py:245](mojo/helpers/llm.py:245), and persist per-call cache usage on the final assistant `Message` so cache effectiveness is observable.

### Steps

1. **[mojo/helpers/llm.py:245](mojo/helpers/llm.py:245)** — In `call()`, add a settings-gated `cache_control` kwarg. Default on via new `LLM_ADMIN_PROMPT_CACHE_ENABLED` setting (`True`). Return value unchanged — `response.model_dump()` already includes `usage`. Add a one-time WARNING (process-level guard) when first turn returns both `cache_creation_input_tokens == 0` and `cache_read_input_tokens == 0` despite caching being enabled, so operators know their prompt is below the model minimum.

2. **[mojo/apps/assistant/services/agent.py](mojo/apps/assistant/services/agent.py)** — Add a small helper `_accumulate_usage(totals, response_usage)` near `_dumps_tool_result` that sums the known keys (`cache_read_input_tokens`, `cache_creation_input_tokens`, `input_tokens`, `output_tokens`) and tolerates missing fields. Both `run_assistant()` (around [agent.py:1184](mojo/apps/assistant/services/agent.py:1184)) and `run_assistant_ws()` (around [agent.py:1398](mojo/apps/assistant/services/agent.py:1398)) initialize `usage_totals = {}`, call the helper after each `llm.call()`, log per-turn usage to `assistant.log` at INFO (`conv=X turn=N cache_read=A cache_write=B input=C output=D`), and pass `usage=usage_totals` into the final `Message.objects.create(...)` for both the normal-completion exit and the max-turns exit.

3. **[mojo/apps/assistant/models/conversation.py:41](mojo/apps/assistant/models/conversation.py:41)** — Add `usage = models.JSONField(null=True, blank=True, default=None)` to `Message`. Add `"usage"` to the `default` graph at [conversation.py:69](mojo/apps/assistant/models/conversation.py:69). Run `bin/create_testproject` to regenerate the migration (`0006_message_usage.py`).

4. **[docs/django_developer/assistant/README.md](docs/django_developer/assistant/README.md)** — Add `LLM_ADMIN_PROMPT_CACHE_ENABLED` row to the settings table at line 116. Add a new "Prompt Caching" subsection covering: what's cached (full prefix via automatic caching), what invalidates the cache (tool array changes via `load_tools`, system-prompt changes via skill / memory mutations), the 1024/4096-token minimums per model family, the new `Message.usage` field, and how to read cache effectiveness from `assistant.log`.

5. **[docs/web_developer/assistant/README.md:271](docs/web_developer/assistant/README.md:271)** — Add `usage` row to the Message fields table — summed `{cache_read_input_tokens, cache_creation_input_tokens, input_tokens, output_tokens}` across turns, present only on the final assistant message of each user-message exchange.

6. **`tests/test_assistant/33_test_prompt_caching.py`** — New test file (see Testing section).

7. **CHANGELOG.md** — version bump entry under the next patch release.

### Design Decisions

- **Automatic caching, not explicit breakpoints**: One top-level `cache_control` kwarg matches the agent loop's "system + tools + messages, breakpoint advances as the conversation grows" pattern. Explicit breakpoints add complexity for no v1 benefit.
- **Sum usage across turns onto the final Message**: One DB row per user message gets the totals, matching how `duration_ms` already works. Per-turn breakdown lives in the log only. Confirmed with the user.
- **Helper instead of duplicating accumulation**: `run_assistant()` and `run_assistant_ws()` already duplicate loop logic. The accumulation helper keeps the addition surgical without taking on a larger refactor of the two functions.
- **Setting defaults to True**: net-positive in the dominant case (multi-turn agent loop). Disable switch exists for incident response, not for everyday use.
- **One-time warning on zero-usage caching**: tells operators their prompt is below the model minimum without spamming logs (process-level guard, fires at most once per worker).
- **No change to `llm.ask()` signature**: it goes through `call()` and inherits the behaviour. Memory-dream prompts are short and caching is silently a no-op.

### Edge Cases

- **`response.usage` missing on error paths**: `_accumulate_usage` reads via `.get("usage", {})` and `.get(key, 0)` — never raises.
- **Anthropic SDK adds new usage fields**: helper accumulates by known key list and ignores unknown keys.
- **Cache write fails Anthropic-side**: we never know — we just see the next call as a write. Acceptable.
- **Settings flag flipped mid-loop**: read once at top of `call()`. A flip mid-conversation just means the next call doesn't send `cache_control`; nothing breaks.
- **Old conversations from before the upgrade**: first turn after deploy is a cache write, normal behaviour from there.
- **Test isolation**: tests mock `llm.call` and the underlying `Anthropic.messages.create` — never hit the live API.

### Testing

All scenarios go in `tests/test_assistant/33_test_prompt_caching.py`. Mock the Anthropic client; never call the live API.

- `test_llm_helper_sets_cache_control_when_enabled` — mock `anthropic.Anthropic`, assert `cache_control={"type":"ephemeral"}` is in the kwargs sent to `messages.create()`.
- `test_llm_helper_omits_cache_control_when_disabled` — `th.server_settings(LLM_ADMIN_PROMPT_CACHE_ENABLED=False)`, assert kwarg absent.
- `test_llm_helper_returns_usage` — `call()` result dict includes a `usage` dict surfaced from `response.model_dump()`.
- `test_accumulate_usage_sums_all_counters` — direct unit test: two calls accumulate correctly across all four keys.
- `test_accumulate_usage_handles_missing_fields` — empty / partial usage dict doesn't raise; missing keys treated as 0.
- `test_assistant_persists_usage_on_final_message` — drive `run_assistant()` with mocked `llm.call` returning canned per-turn usage; assert final `Message.usage` equals the sum across turns.
- `test_assistant_logs_per_turn_cache_usage` — capture log records, assert one INFO line per turn with the breakdown.
- `test_message_usage_in_default_graph` — assert `"usage"` is in `Message.RestMeta.GRAPHS["default"]["fields"]`.
- `test_message_usage_field_nullable` — `Message.usage` defaults to None when not provided.
- `test_zero_usage_warning_fires_once` — when caching enabled but both counters return 0, WARN logged on first occurrence only.

### Docs

- `docs/django_developer/assistant/README.md` — settings table row + new "Prompt Caching" subsection.
- `docs/web_developer/assistant/README.md` — `usage` row in Message fields table.
- `CHANGELOG.md` — entry under next patch release.

## Resolution

**Status**: resolved
**Date**: 2026-05-25

### What Was Built
Anthropic automatic prompt caching enabled on every assistant LLM call. Per-turn cache usage accumulated across the agent loop and persisted on the final assistant `Message` for diagnostic display, plus one INFO log line per turn.

### Files Changed
- `mojo/helpers/llm.py` — `call()` injects `cache_control={"type":"ephemeral"}` when `LLM_ADMIN_PROMPT_CACHE_ENABLED` (default `True`); one-time WARN when caching enabled but both cache counters return 0 on the first call.
- `mojo/apps/assistant/services/agent.py` — `_accumulate_usage()` helper, per-turn INFO log, `usage_totals` threaded through `run_assistant()` and `run_assistant_ws()` into the final `Message.objects.create(...)` (success exit + max-turns exit). Result dict gains a `usage` key.
- `mojo/apps/assistant/models/conversation.py` — `Message.usage` JSONField; added to `default` REST graph.
- `mojo/apps/assistant/migrations/0006_message_usage.py` — schema migration (generated by `bin/create_testproject`).
- `docs/django_developer/assistant/README.md` — settings table row + new "Prompt Caching" subsection.
- `docs/web_developer/assistant/README.md` — `usage` row in Message fields table.
- `CHANGELOG.md` — v1.2.25 entry.

### Tests
- `tests/test_assistant/33_test_prompt_caching.py` — 10 tests covering cache_control injection (enabled / disabled), usage round-trip, `_accumulate_usage` summing and missing-field tolerance, end-to-end usage persistence on the final Message, per-turn INFO logging, Message.usage graph exposure + nullability, and the one-time zero-cache warning.
- Run: `bin/run_tests --agent -t test_assistant.33_test_prompt_caching` — 10 passed.

### Security Review
(pending — security-review agent spawned after commit)

### Follow-up
None for v1. If telemetry from `Message.usage` shows skill/memory mutation as the dominant cache invalidator, consider moving the skill catalog + memory section out of the system prompt and into the first user message to insulate the system cache from those edits (called out as out-of-scope above).
