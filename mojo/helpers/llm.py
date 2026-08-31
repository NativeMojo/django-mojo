"""
LLM helper — model discovery, API key management, and quick calls.

Usage:
    from mojo.helpers import llm

    # Model selection (auto-discovers latest from Anthropic API, caches 24h)
    model = llm.get_model("general")   # latest Sonnet
    model = llm.get_model("powerful")  # latest Opus
    model = llm.get_model("fast")      # latest Haiku

    # Quick one-shot question
    answer = llm.ask("Summarize this text: ...", feature="scheduled_task")

    # Full messages API call (tool use, multi-turn, etc.)
    response = llm.call(
        messages, system="You are...", tools=[...], feature="assistant")

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
import warnings

from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger(__name__, "llm.log")

# In-memory fallback cache when Redis is unavailable
_mem_cache = {"models": None, "fetched_at": 0}

# Process-level guard so the "caching enabled but prefix too short" warning
# fires once per worker instead of on every call.
_zero_cache_warned = False
_unattributed_warned = False

FEATURES = frozenset({
    "assistant", "incident_triage", "incident_analysis", "incident_ticket",
    "scheduled_task", "memory", "file_analysis", "configuration",
    "model_discovery", "unattributed",
})


def normalize_feature(feature):
    """Return one fixed feature name; omission is transitional, not anonymous."""
    global _unattributed_warned
    if feature is None:
        if not _unattributed_warned:
            _unattributed_warned = True
            warnings.warn(
                "LLM calls without feature= are deprecated; attributed calls are required",
                DeprecationWarning, stacklevel=3)
        return "unattributed"
    if feature not in FEATURES:
        raise ValueError("Unknown LLM feature")
    return feature

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


class LLMExecutionError(Exception):
    """Stable safe-code boundary; provider exception text never crosses it."""

    def __init__(self, code, retry_after=None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def verify_api_key(target="admin"):
    """
    Verify one exact stored Anthropic credential is valid.

    Returns (True, None) on success or (False, "error message") on failure.
    """
    try:
        from mojo.apps.account.services import llm_safety
        llm_safety.verify_stored_key(target)
        return True, None
    except Exception as err:
        code = getattr(err, "code", "provider_unavailable")
        if code == "credential_missing":
            return False, "No API key is configured for this target."
        if code == "provider_authentication":
            return False, "API key is invalid or expired."
        return False, "Could not verify API key."


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def _fetch_models_from_api():
    """Fetch the full model list from Anthropic's /v1/models endpoint."""
    try:
        from mojo.apps.account.services import llm_safety
        return llm_safety.discover_models()
    except Exception:
        logger.warning("LLM model catalogue request failed")
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


def model_choices(refresh=False, loader=None):
    """Suggestions for a model picker: ``[{"id", "label"}]``, never empty.

    Suggestions only. Nothing validates a saved pin against this list — the
    list is network-dependent, so validating against it would reject a
    perfectly good re-save whenever the 24h cache has lapsed and the API is
    unreachable.

    Reads the shared cache by default and does NOT fetch: a settings page that
    fetched on every render would spend an Anthropic round trip to draw a
    dropdown. ``refresh=True`` is the operator's explicit "refresh the
    catalogue" control. ``loader`` is a seam for tests.

    A candidate key is never accepted here: fetching under an unsaved key would
    write the shared 24h cache from a credential the installation is not
    running.
    """
    if loader is None:
        loader = (lambda: get_models(force_refresh=True)) if refresh else _cache_get
    try:
        models = loader()
    except Exception as err:
        logger.warning(f"Model catalogue unavailable for the picker: {str(err)[:200]}")
        models = None

    entries = []
    for model in models or []:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        if not any(family in model["id"] for family in _USE_TO_FAMILY.values()):
            continue
        entries.append(model)

    if not entries:
        # A picker with nothing in it looks broken, and the three aliases are
        # exactly what resolution would fall back to anyway.
        return [{"id": _FALLBACKS[use], "label": _FALLBACKS[use]}
                for use in ("general", "powerful", "fast")]

    entries.sort(key=_rank_key, reverse=True)
    choices = []
    for model in entries[:40]:
        label = model.get("display_name")
        if not isinstance(label, str) or not label:
            label = model["id"]
        choices.append({"id": model["id"], "label": label})
    return choices


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

def call(messages, system=None, tools=None, model=None, max_tokens=4096, *,
         feature=None, operation="call", context=None):
    """
    Call the Anthropic messages API.

    Returns the response as a dict (via model_dump()) including ``usage``.
    Raises on API errors — callers handle their own error logic.

    Prompt caching is enabled by default — adds ``cache_control`` at the
    top level so Anthropic caches the prefix automatically. Disable via
    ``LLM_ADMIN_PROMPT_CACHE_ENABLED=False``.

    Every provider request passes the installation safety policy and ledger.
    """
    global _zero_cache_warned

    normalize_feature(feature)
    if context is not None and not isinstance(context, dict):
        raise ValueError("LLM context must be a dictionary of scalar identifiers")
    if context and any(isinstance(value, (dict, list, tuple, set))
                       for value in context.values()):
        raise ValueError("LLM context identifiers must be scalar")

    cache_enabled = settings.get("LLM_ADMIN_PROMPT_CACHE_ENABLED", True, kind="bool")
    resolved_model = model or "policy-route-model"
    from mojo.apps.account.services import llm_safety
    try:
        result = llm_safety.invoke(
            messages, system=system, tools=tools, model=model,
            max_tokens=max_tokens, feature=normalize_feature(feature),
            operation=operation, context=context)
    except llm_safety.LLMSafetyError as err:
        raise LLMExecutionError(err.code, retry_after=err.retry_after) from None
    except Exception:
        raise LLMExecutionError("safety_unavailable") from None

    # Warn once per worker if caching is enabled but produced no cache activity.
    # Typically means the prefix is below the model's minimum cacheable size
    # (1024 tokens for Sonnet, 4096 for Opus).
    _warn_zero_cache(result, resolved_model, cache_enabled)

    return result


def _warn_zero_cache(result, resolved_model, cache_enabled):
    global _zero_cache_warned
    if cache_enabled and not _zero_cache_warned:
        usage = result.get("usage") or {}
        if usage.get("cache_creation_input_tokens", 0) == 0 and \
                usage.get("cache_read_input_tokens", 0) == 0:
            _zero_cache_warned = True
            logger.warning(
                f"Prompt caching enabled but no cache activity on first call "
                f"(model={resolved_model}). Prefix likely below the model minimum "
                f"(1024 tokens for Sonnet, 4096 for Opus).")


def ask(prompt, system=None, model=None, max_tokens=4096, *, feature=None,
        operation="ask", context=None):
    """
    One-shot LLM question — send a prompt, get a string back.

    Good for summarization, classification, text generation, etc.
    No tools, no conversation history.
    """
    messages = [{"role": "user", "content": prompt}]
    response = call(
        messages, system=system, model=model, max_tokens=max_tokens,
        feature=feature, operation=operation, context=context)
    # Extract text from response content blocks
    parts = []
    for block in response.get("content", []):
        if block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts)
