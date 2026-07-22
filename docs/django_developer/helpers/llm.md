# LLM Helper — Django Developer Reference

Centralized helpers for Anthropic Claude API integration: model discovery, API key management, and quick calls.

```python
from mojo.helpers import llm
```

## API Key

```python
key = llm.get_api_key()
# Resolution: LLM_ADMIN_API_KEY -> LLM_HANDLER_API_KEY -> None

ok, error = llm.verify_api_key()
# Returns (True, None) or (False, "error message")
# Optionally pass api_key= to verify a specific key
```

## Model Selection

```python
model = llm.get_model("general")   # latest Sonnet (balanced)
model = llm.get_model("powerful")  # latest Opus (max intelligence)
model = llm.get_model("fast")      # latest Haiku (quick/cheap)
```

Resolution order:
1. Explicit setting pin (`LLM_ADMIN_MODEL` or `LLM_HANDLER_MODEL`) — if set, returned as-is
2. Auto-detect from Anthropic `/v1/models` endpoint (cached 24h in Redis, in-memory fallback)
3. Hardcoded fallback if API is unreachable

### Tiers

| Use case | Family |
|---|---|
| `"powerful"` | Opus |
| `"general"` | Sonnet |
| `"fast"` | Haiku |

These three families are the only ones a use case can resolve to. To reach any other model, pass `model=` explicitly or pin `LLM_ADMIN_MODEL`. An unrecognized use case logs a warning and resolves as `"general"`.

### How auto-detect ranks models

Within the tier's family, the newest model wins, decided by the `created_at` timestamp the API returns — **not** by the shape of the model ID. A short alias (`claude-opus-4-8`) always beats a dated snapshot (`claude-opus-4-8-20260720`), even a newer one, because the alias follows the latest build. When a family has no alias, the newest snapshot is used.

ID length is deliberately not part of the ranking. Every alias within a generation is the same width (`claude-opus-4-1`, `claude-opus-4-8`), so it says nothing about recency.

### Cache

Model lists are cached in Redis (`mojo:llm:models`, 24h TTL) and shared across workers. If Redis is unavailable, a per-process in-memory cache is used instead. Call `get_models(force_refresh=True)` to bypass the cache.

```python
models = llm.get_models()               # cached model list (list of dicts)
models = llm.get_models(force_refresh=True)  # force API call
```

## Quick Calls

### `ask()` — One-shot question

```python
answer = llm.ask("Summarize this text: ...")
answer = llm.ask("Classify this: ...", model=llm.get_model("fast"))
```

Returns a string. No tools, no conversation. Good for summarization, classification, text generation.

### `call()` — Full messages API

```python
response = llm.call(
    messages=[{"role": "user", "content": "Hello"}],
    system="You are a helpful assistant.",
    tools=[...],           # optional tool definitions
    model="claude-sonnet-5",  # optional, defaults to get_model("general")
    max_tokens=4096,       # optional
)
# Returns dict (response.model_dump() from anthropic SDK)
```

Raises `ValueError` if no API key is configured. Other API errors propagate from the anthropic SDK.

## Settings

| Setting | Purpose |
|---|---|
| `LLM_ADMIN_API_KEY` | Checked first by `get_api_key()` |
| `LLM_HANDLER_API_KEY` | Fallback |
| `LLM_ADMIN_MODEL` | If set, `get_model()` returns this (explicit pin) |
| `LLM_HANDLER_MODEL` | Second-tier pin |

If no model setting is pinned, `get_model()` auto-detects from the API.
