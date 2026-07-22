"""
LLM helper — model discovery, API key management, and quick calls.

Usage:
    from mojo.helpers import llm

    # Model selection (auto-discovers latest from Anthropic API, caches 24h)
    model = llm.get_model("general")   # latest Sonnet
    model = llm.get_model("powerful")  # latest Opus
    model = llm.get_model("fast")      # latest Haiku

    # Quick one-shot question
    answer = llm.ask("Summarize this text: ...")

    # Full messages API call (tool use, multi-turn, etc.)
    response = llm.call(messages, system="You are...", tools=[...])

    # API key helpers
    key = llm.get_api_key()
    ok, error = llm.verify_api_key()

Settings used:
    LLM_ADMIN_API_KEY      # checked first
    LLM_HANDLER_API_KEY    # fallback
    LLM_ADMIN_MODEL        # explicit pin (skips auto-detect)
    LLM_HANDLER_MODEL      # second-tier pin
"""

import json
import time

from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger(__name__, "llm.log")

# In-memory fallback cache when Redis is unavailable
_mem_cache = {"models": None, "fetched_at": 0}

# Process-level guard so the "caching enabled but prefix too short" warning
# fires once per worker instead of on every call.
_zero_cache_warned = False

CACHE_KEY = "mojo:llm:models"
CACHE_TTL = 86400  # 24 hours

# Hardcoded fallbacks if the API is unreachable. Aliases, not dated snapshots —
# an alias follows the latest build and doesn't retire on a snapshot's schedule.
# Revisit when a new generation ships.
_FALLBACKS = {
    "powerful": "claude-opus-4-8",
    "general": "claude-sonnet-5",
    "fast": "claude-haiku-4-5",
}

# Map use-case to model family keyword. These three families are the only ones
# a use-case can resolve to; anything else needs an explicit model= argument or
# an LLM_ADMIN_MODEL pin.
_USE_TO_FAMILY = {
    "powerful": "opus",
    "general": "sonnet",
    "fast": "haiku",
}


# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------

def get_api_key():
    """Resolve LLM API key: LLM_ADMIN_API_KEY -> LLM_HANDLER_API_KEY."""
    key = settings.get("LLM_ADMIN_API_KEY", None)
    if not key:
        key = settings.get("LLM_HANDLER_API_KEY", None)
    return key


def verify_api_key(api_key=None):
    """
    Verify an Anthropic API key is valid.

    Returns (True, None) on success or (False, "error message") on failure.
    """
    import anthropic

    key = api_key or get_api_key()
    if not key:
        return False, "No API key configured. Set LLM_ADMIN_API_KEY or LLM_HANDLER_API_KEY."
    try:
        client = anthropic.Anthropic(api_key=key)
        client.models.list(limit=1)
        return True, None
    except anthropic.AuthenticationError:
        return False, "API key is invalid or expired."
    except Exception as e:
        return False, f"Could not verify API key: {str(e)[:200]}"


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def _fetch_models_from_api(api_key=None):
    """Fetch the full model list from Anthropic's /v1/models endpoint."""
    import anthropic

    key = api_key or get_api_key()
    if not key:
        return None

    try:
        client = anthropic.Anthropic(api_key=key)
        models = []
        # mode="json" keeps created_at an ISO string instead of a datetime, so
        # the list stays JSON-serializable for the Redis cache below.
        page = client.models.list(limit=100)
        for model in page.data:
            models.append(model.model_dump(mode="json"))
        # Paginate if needed
        while page.has_more:
            page = client.models.list(limit=100, after_id=page.last_id)
            for model in page.data:
                models.append(model.model_dump(mode="json"))
        return models
    except Exception as e:
        logger.warning(f"Failed to fetch models from Anthropic API: {str(e)[:200]}")
        return None


def _cache_get():
    """Try to read cached models from Redis, fall back to in-memory."""
    try:
        from mojo.helpers.redis import get_connection
        r = get_connection()
        raw = r.get(CACHE_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    # In-memory fallback
    if _mem_cache["models"] and (time.time() - _mem_cache["fetched_at"]) < CACHE_TTL:
        return _mem_cache["models"]
    return None


def _cache_set(models):
    """Store models in Redis (with TTL) and in-memory."""
    _mem_cache["models"] = models
    _mem_cache["fetched_at"] = time.time()
    try:
        payload = json.dumps(models)
    except TypeError as err:
        # Would otherwise disable the shared cache silently — say so.
        logger.warning(f"Model list is not JSON-serializable, skipping Redis cache: {err}")
        return
    try:
        from mojo.helpers.redis import get_connection
        r = get_connection()
        r.setex(CACHE_KEY, CACHE_TTL, payload)
    except Exception as err:
        # Redis being unavailable is expected — the in-memory cache covers it.
        logger.debug(f"Redis model cache write skipped: {err}")


def get_models(force_refresh=False):
    """
    Return the list of available Anthropic models.

    Cached for 24 hours in Redis (falls back to in-memory).
    Pass force_refresh=True to bypass cache.
    """
    if not force_refresh:
        cached = _cache_get()
        if cached:
            return cached

    models = _fetch_models_from_api()
    if models:
        _cache_set(models)
        return models

    # If API call failed, try stale cache
    if _mem_cache["models"]:
        return _mem_cache["models"]

    return None


def _is_dated_snapshot(model_id):
    """True for IDs ending in a YYYYMMDD build date (claude-opus-4-1-20250805)."""
    tail = model_id.rsplit("-", 1)[-1]
    return len(tail) == 8 and tail.isdigit()


def _version_tuple(model_id):
    """
    Numeric version parts of an ID: claude-opus-4-8 -> (4, 8).

    The guard is deliberately narrower than isdigit(): that accepts characters
    int() rejects (superscripts and the like), and int() also refuses segments
    over 4300 digits. Model IDs are opaque strings from the API, so a segment
    that isn't a plain short ASCII number is skipped rather than converted.
    The length cap also drops the 8-digit date on a snapshot ID.
    """
    return tuple(
        int(part) for part in model_id.split("-")
        if part.isascii() and part.isdigit() and len(part) <= 4
    )


def _created_at_key(model):
    """created_at as a sortable string — the API sends a datetime, the cache a string."""
    value = model.get("created_at") or ""
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    return str(value)


def _rank_key(model):
    """
    Preference key for a model entry — bigger is better.

    1. An alias beats a dated snapshot: the alias tracks the latest build.
    2. Newest created_at wins. This is the API's own recency signal, and the
       only one that survives Anthropic changing how models are named.
    3. Version number, then the ID itself, purely so ties are deterministic.

    Do NOT rank by ID length. Every alias within a generation is the same
    length (claude-opus-4-1 / claude-opus-4-8), so length carries no recency
    information — that assumption is what this function replaced.
    """
    model_id = model.get("id", "")
    return (
        0 if _is_dated_snapshot(model_id) else 1,
        _created_at_key(model),
        _version_tuple(model_id),
        model_id,
    )


def _pick_best_model(models, family_keyword):
    """
    Pick the best model for a given family keyword (opus/sonnet/haiku).

    Returns the family's newest alias, or its newest dated snapshot when the
    family has no alias. None when nothing matches.

    Entries that aren't a dict with a string id are skipped — the list may come
    straight from the API or from the Redis cache, and one malformed row must
    not take down every caller. Resolution stays fail-soft: no match falls
    through to _FALLBACKS.
    """
    candidates = [
        m for m in models
        if isinstance(m, dict) and isinstance(m.get("id"), str)
        and family_keyword in m["id"]
    ]
    if not candidates:
        return None
    return max(candidates, key=_rank_key)["id"]


def get_model(use="general"):
    """
    Return the best model ID for a given use case.

    use:
        "general"  — latest Sonnet (balanced speed + intelligence)
        "powerful" — latest Opus (max intelligence)
        "fast"     — latest Haiku (quick and cheap)

    Resolution order:
        1. Explicit setting pin (LLM_ADMIN_MODEL or LLM_HANDLER_MODEL)
        2. Auto-detect from Anthropic API (cached 24h)
        3. Hardcoded fallback
    """
    # 1. Check for explicit pin
    pinned = settings.get("LLM_ADMIN_MODEL", None)
    if not pinned:
        pinned = settings.get("LLM_HANDLER_MODEL", None)
    if pinned:
        return pinned

    # 2. Auto-detect from API
    family_keyword = _USE_TO_FAMILY.get(use)
    if not family_keyword:
        logger.warning(f"Unknown model tier '{use}' — using 'general'")
        use = "general"
        family_keyword = _USE_TO_FAMILY[use]
    models = get_models()
    if models:
        best = _pick_best_model(models, family_keyword)
        if best:
            return best

    # 3. Hardcoded fallback
    return _FALLBACKS.get(use, _FALLBACKS["general"])


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def call(messages, system=None, tools=None, model=None, max_tokens=4096):
    """
    Call the Anthropic messages API.

    Returns the response as a dict (via model_dump()) including ``usage``.
    Raises on API errors — callers handle their own error logic.

    Prompt caching is enabled by default — adds ``cache_control`` at the
    top level so Anthropic caches the prefix automatically. Disable via
    ``LLM_ADMIN_PROMPT_CACHE_ENABLED=False``.
    """
    global _zero_cache_warned
    import anthropic

    key = get_api_key()
    if not key:
        raise ValueError("No LLM API key configured. Set LLM_ADMIN_API_KEY or LLM_HANDLER_API_KEY.")

    resolved_model = model or get_model("general")
    client = anthropic.Anthropic(api_key=key)

    kwargs = {
        "model": resolved_model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools

    cache_enabled = settings.get("LLM_ADMIN_PROMPT_CACHE_ENABLED", True, kind="bool")
    if cache_enabled:
        kwargs["cache_control"] = {"type": "ephemeral"}

    response = client.messages.create(**kwargs)
    result = response.model_dump()

    # Warn once per worker if caching is enabled but produced no cache activity.
    # Typically means the prefix is below the model's minimum cacheable size
    # (1024 tokens for Sonnet, 4096 for Opus).
    if cache_enabled and not _zero_cache_warned:
        usage = result.get("usage") or {}
        if usage.get("cache_creation_input_tokens", 0) == 0 and \
                usage.get("cache_read_input_tokens", 0) == 0:
            _zero_cache_warned = True
            logger.warning(
                f"Prompt caching enabled but no cache activity on first call "
                f"(model={resolved_model}). Prefix likely below the model minimum "
                f"(1024 tokens for Sonnet, 4096 for Opus)."
            )

    return result


def ask(prompt, system=None, model=None, max_tokens=4096):
    """
    One-shot LLM question — send a prompt, get a string back.

    Good for summarization, classification, text generation, etc.
    No tools, no conversation history.
    """
    messages = [{"role": "user", "content": prompt}]
    response = call(messages, system=system, model=model, max_tokens=max_tokens)
    # Extract text from response content blocks
    parts = []
    for block in response.get("content", []):
        if block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts)
