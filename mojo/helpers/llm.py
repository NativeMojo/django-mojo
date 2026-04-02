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

CACHE_KEY = "mojo:llm:models"
CACHE_TTL = 86400  # 24 hours

# Model family prefixes in priority order (newest families first)
_FAMILY_ORDER = ["claude-opus-4", "claude-sonnet-4", "claude-haiku-4"]

# Hardcoded fallbacks if the API is unreachable
_FALLBACKS = {
    "powerful": "claude-sonnet-4-20250514",
    "general": "claude-sonnet-4-20250514",
    "fast": "claude-haiku-4-5-20251001",
}

# Map use-case to model family keyword
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
        page = client.models.list(limit=100)
        for model in page.data:
            models.append(model.model_dump())
        # Paginate if needed
        while page.has_more:
            page = client.models.list(limit=100, after_id=page.last_id)
            for model in page.data:
                models.append(model.model_dump())
        return models
    except Exception as e:
        logger.warning("Failed to fetch models from Anthropic API: %s", str(e)[:200])
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
        from mojo.helpers.redis import get_connection
        r = get_connection()
        r.setex(CACHE_KEY, CACHE_TTL, json.dumps(models))
    except Exception:
        pass


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


def _pick_best_model(models, family_keyword):
    """
    Pick the best model for a given family keyword (opus/sonnet/haiku).

    Prefers models with shorter IDs (e.g. "claude-sonnet-4-6" over
    "claude-sonnet-4-6-20260301") since the short alias always points
    to the latest.
    """
    candidates = []
    for m in models:
        model_id = m.get("id", "")
        if family_keyword in model_id:
            candidates.append(m)

    if not candidates:
        return None

    # Prefer the shortest ID — that's the alias (e.g., "claude-sonnet-4-6")
    # which Anthropic always points at the latest version.
    # Among equal lengths, prefer the most recently created.
    candidates.sort(key=lambda m: (len(m["id"]), m.get("created_at", "")))
    # But if there's a short alias (no date suffix), strongly prefer it
    for c in candidates:
        mid = c["id"]
        # Alias IDs don't have a date suffix like -20250514
        if not any(ch.isdigit() and len(part) == 8 for part in mid.split("-") for ch in [part]):
            return mid

    # Fallback: pick the one with the newest created_at
    candidates.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return candidates[0]["id"]


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
    family_keyword = _USE_TO_FAMILY.get(use, "sonnet")
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

    Returns the response as a dict (via model_dump()).
    Raises on API errors — callers handle their own error logic.
    """
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

    response = client.messages.create(**kwargs)
    return response.model_dump()


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
