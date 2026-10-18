# LLM Helper — Django Developer Reference

Provider-neutral LLM facade with Anthropic and an AWS Bedrock Nova 2 Lite
adapter (text, plus validated image input on an image route). Production requests use the mandatory
[LLM safety boundary](../security/llm_safety.md).

```python
from mojo.helpers import llm
```

## API Key

These key helpers, verification, and model discovery remain Anthropic-only.
Bedrock uses the server's AWS role/profile chain; see
[Bedrock reasoning](#bedrock-reasoning).

```python
key = llm.get_api_key()
# Resolution: LLM_ADMIN_API_KEY -> LLM_HANDLER_API_KEY -> None

ok, error = llm.verify_api_key("admin")
# Returns (True, None) or (False, "error message")
# Pass "handler" to check that exact stored target. Candidate material is
# accepted only by the fresh-owner Assistant setup service.
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
    reasoning_effort="off",  # default; Anthropic accepts only off
    feature="assistant",  # required for framework callers
    context={"conversation_id": 42, "operation_id": "stable-loop-uuid"},
)
# Returns a normalized response dict, including content and usage
```

Raises `LLMExecutionError(code, retry_after=None)` when policy, controls,
budgets, the circuit, persistence, or provider denies the request. Raw
SDK/provider text does not cross the adapter. Multi-call loops must reuse one
unguessable `operation_id`; the guard increments it atomically. Omitted
`feature` temporarily becomes separately budgeted `unattributed`; unknown
explicit features are refused.
A host app adds its own feature names with `LLM_HOST_FEATURES`; see
[Host features](../security/llm_safety.md#host-features).

## Bedrock reasoning

A deployment may deliberately add a Bedrock text route for a host feature;
see the [policy and AWS requirements](../security/llm_safety.md#bedrock-routes).
There is no default Bedrock route or automatic provider/credential fallback.
The installation supplies AWS access, so end users need no individual AI
subscription or computer setup.

```python
response = llm.call(
    messages=[{"role": "user", "content": "Compare these two designs: ..."}],
    system="Return a concise final answer.",
    feature="my_maestro_reasoning",
    operation="reason",
    context={"job_id": queue_job.id, "operation_id": str(personal_job.uid)},
    max_tokens=4096,
    reasoning_effort="low",
)
answer = "\n".join(block["text"] for block in response["content"])
usage = response["usage"]
```

`ask(..., reasoning_effort="low")` also returns final text. The keyword-only
effort defaults to `off`; Bedrock permits only the route's declared subset of
`off`, `low`, and `medium`. Off omits extended thinking; low/medium send the
fixed reasoning configuration. High effort is unsupported because this
adapter always sends an output cap. `max_tokens` must be a positive integer
within both policy caps and the adapter ceiling of 65000, including billed
reasoning.

Messages have exactly `role` (`user` or `assistant`) and `content`; content is
a nonblank string or a nonempty list of `{"type": "text", "text": "..."}`
blocks with nonblank text.
On a route that admits `images`, a user message may also carry image blocks
from `llm.image_block()`; see
[Image input](../security/llm_safety.md#image-input).
System content accepts the same text forms. Tools, streaming,
arbitrary model fields, and explicit cache controls are unsupported. The
adapter sends no Bedrock guardrail configuration or application content filter.

The response has `provider="bedrock"`, the pinned `model`, AWS request `id`,
`stop_reason`, final `content` text blocks, and these usage classes:

| Field | Meaning |
|---|---|
| `input_tokens` | Input outside the cache classes |
| `cache_read_input_tokens` | Implicit cache reads |
| `cache_creation_input_tokens` | Implicit cache writes |
| `output_tokens` | All billed output, including reasoning |

Total input is the sum of the three input classes. Reasoning/redacted blocks
never become answer text; there is no separate exact reasoning-token count
to add to output. `end_turn` is complete; a text-bearing `max_tokens` result
is partial. Empty, reasoning-only, filtered, malformed, over-cap, or over-1-MiB
final output raises a safe provider error, retaining any validated billed
usage in the ledger. No explicit cache checkpoints are sent, but implicit
cache usage is still accounted for.

An application-owned handler published with the existing `jobs.publish` can
make this call; keep AWS credentials out of browser input, job payloads, and
logs. Reuse one stable operation id for one durable application job;
different jobs from the same spoken turn need different ids. The operation
counter expires, so durable attempts need their own bounds. Socket timeouts
do not guarantee cancellation, and ambiguous `provider_*` failures must not
be automatically replayed; see [retry semantics](../security/llm_safety.md#stable-failures-and-retry-semantics).

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
