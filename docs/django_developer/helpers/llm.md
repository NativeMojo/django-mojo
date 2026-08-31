# LLM Helper — Django Developer Reference

Provider-neutral LLM facade with Anthropic as the sole production adapter in
this release. Production requests use the mandatory
[LLM safety boundary](../security/llm_safety.md).

```python
from mojo.helpers import llm
```

## API Key

```python
key = llm.get_api_key()
# Resolution: LLM_ADMIN_API_KEY -> LLM_HANDLER_API_KEY -> None

ok, error = llm.verify_api_key()
# Returns (True, None) or (False, "error message")
# Optionally pass api_key= for the fixed candidate-only configuration probe
```

## Model Selection

```python
model = llm.get_model("general")   # latest Sonnet (balanced)
model = llm.get_model("powerful")  # latest Opus (max intelligence)
model = llm.get_model("fast")      # latest Haiku (quick/cheap)
```

This resolution order supplies picker suggestions and legacy non-guarded
configuration reads:
1. Explicit setting pin (`LLM_ADMIN_MODEL` or `LLM_HANDLER_MODEL`) — if set, returned as-is
2. Auto-detect from Anthropic `/v1/models` endpoint (cached 24h in Redis, in-memory fallback)
3. Hardcoded fallback if API is unreachable

### Tiers

| Use case | Family |
|---|---|
| `"powerful"` | Opus |
| `"general"` | Sonnet |
| `"fast"` | Haiku |

Guarded calls use the model owned by their policy route. `model=` may only
repeat that exact model; it cannot select a different route model. The tier
helpers do not override policy.

### How auto-detect ranks models

Within the tier's family, the newest model wins, decided by the `created_at` timestamp the API returns — **not** by the shape of the model ID. A short alias (`claude-opus-4-8`) always beats a dated snapshot (`claude-opus-4-8-20260720`), even a newer one, because the alias follows the latest build. When a family has no alias, the newest snapshot is used.

ID length is deliberately not part of the ranking. Every alias within a generation is the same width (`claude-opus-4-1`, `claude-opus-4-8`), so it says nothing about recency.

### Cache

Model lists are cached in Redis (`mojo:llm:models`, 24h TTL) and shared across
workers. If Redis is unavailable, a per-process in-memory cache is used
instead. A refresh performs one guarded, timed page of at most 100 models—no
unaccounted pagination. Call `get_models(force_refresh=True)` to bypass the
cache.

```python
models = llm.get_models()               # cached model list (list of dicts)
models = llm.get_models(force_refresh=True)  # force API call
```

## Quick Calls

### `ask()` — One-shot question

```python
answer = llm.ask("Summarize this text: ...", feature="scheduled_task")
answer = llm.ask("Classify this: ...", feature="file_analysis")
```

Returns a string. No tools, no conversation. Good for summarization, classification, text generation.

### `call()` — Full messages API

```python
response = llm.call(
    messages=[{"role": "user", "content": "Hello"}],
    system="You are a helpful assistant.",
    tools=[...],           # optional tool definitions
    model="claude-sonnet-5",  # optional; must exactly match the policy route
    max_tokens=4096,       # optional
    feature="assistant",  # required for framework callers
    context={"conversation_id": 42, "operation_id": "stable-loop-uuid"},
)
# Returns dict (response.model_dump() from anthropic SDK)
```

Raises `LLMExecutionError(code, retry_after=None)` when policy, controls,
budgets, the circuit, persistence, or provider denies the request. Raw
SDK/provider text does not cross the adapter. Multi-call loops must reuse one
unguessable `operation_id`; the guard increments it atomically. Omitted
`feature` temporarily becomes separately budgeted `unattributed`; unknown
explicit features are refused.

## `model_choices()` — picker suggestions

```python
from mojo.helpers import llm

llm.model_choices()               # from the shared 24h cache, no network call
llm.model_choices(refresh=True)   # explicit operator-driven refresh
```

Returns `[{"id", "label"}]`, capped at 40, filtered to the opus/sonnet/haiku
families and sorted newest-first by the same `_rank_key` `get_model()` uses. The
label is the API's `display_name`, or the id when it has none.

**It never returns an empty list.** When the catalogue is unavailable — no key
yet, or the API is unreachable — it returns the three `_FALLBACKS` aliases,
which is exactly what resolution would fall back to anyway. A picker with
nothing in it looks broken.

It reads the cache by default and does **not** fetch: a settings page that
fetched on every render would spend a provider round trip to draw a dropdown.
It also never accepts a candidate key, because fetching under an unsaved
credential would write the shared 24-hour cache from a key the installation is
not running.

These are suggestions only. Nothing validates a saved pin against this list: the
list is network-dependent, so validating against it would refuse a perfectly
good re-save whenever the cache has lapsed and the API is down.

## Settings

| Setting | Purpose |
|---|---|
| `LLM_ADMIN_API_KEY` | Checked first by `get_api_key()` |
| `LLM_HANDLER_API_KEY` | Fallback — the platform key, settable from the built-in Admin's Assistant setup |
| `LLM_ADMIN_MODEL` | If set, `get_model()` returns this (explicit pin) |
| `LLM_HANDLER_MODEL` | Second-tier pin |
| `LLM_SAFETY_POLICY` | Required file-owned provider routes and cost envelopes |
| `LLM_SAFETY_POLICY_EXPECTED_HASH` | Protected owner-activated DB agreement; never edit directly |
| `LLM_EMERGENCY_STOP` | Static OR protected database stop; unknown database state denies |
| `LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED` | Protected owner switch, default off |

If no model setting is pinned, `get_model()` auto-detects from the API.
