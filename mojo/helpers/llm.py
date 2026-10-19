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

import re
import warnings

from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.helpers.llm_catalogue import Catalogue

logger = logit.get_logger(__name__, "llm.log")

_catalogue = Catalogue()

# Process-level guard so the "caching enabled but prefix too short" warning
# fires once per worker instead of on every call.
_zero_cache_warned = False
_unattributed_warned = False

FEATURES = frozenset({
    "assistant", "incident_triage", "incident_analysis", "incident_ticket",
    "scheduled_task", "memory", "file_analysis", "configuration",
    "model_discovery", "unattributed",
})

# Names the safety guard uses for its own keys and metrics. A host feature
# called "shared" would share the shared envelope's Redis keys and be charged
# twice; "breaker" and "unknown" are metric categories.
RESERVED_FEATURE_NAMES = frozenset({"shared", "breaker", "unknown"})

# 32 characters is LLMRequest.feature's max_length.
HOST_FEATURE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_UNSET = object()


def host_features(value=_UNSET):
    """Validated LLM_HOST_FEATURES: the host app's own feature names."""
    from django.core.exceptions import ImproperlyConfigured
    if value is _UNSET:
        value = settings.get_static("LLM_HOST_FEATURES", [])
    if not isinstance(value, (list, tuple)):
        raise ImproperlyConfigured("LLM_HOST_FEATURES must be a list of feature names")
    names = set()
    for name in value:
        if not isinstance(name, str) or not HOST_FEATURE_PATTERN.fullmatch(name):
            raise ImproperlyConfigured(
                f"LLM_HOST_FEATURES entry {name!r} must be a lowercase slug of at most "
                "32 characters: a letter, then letters, digits or underscores")
        if name in FEATURES:
            raise ImproperlyConfigured(
                f"LLM_HOST_FEATURES entry {name!r} is a framework feature name")
        if name in RESERVED_FEATURE_NAMES:
            raise ImproperlyConfigured(
                f"LLM_HOST_FEATURES entry {name!r} is reserved by the LLM safety guard")
        if name in names:
            raise ImproperlyConfigured(f"LLM_HOST_FEATURES entry {name!r} is repeated")
        names.add(name)
    return frozenset(names)


def feature_names():
    """Every feature llm.call accepts: the framework's plus LLM_HOST_FEATURES."""
    return FEATURES | host_features()


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
    if feature not in feature_names():
        raise ValueError("Unknown LLM feature")
    return feature

CACHE_KEY = "mojo:llm:models"
CACHE_TTL = 86400  # 24 hours

# Legacy use-case fallbacks only. These IDs are pinned snapshots, not floating
# aliases. The guarded automatic Assistant route never consumes these values.
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

def catalogue_state(*, policy=None, credential=None, redis=None, refresh=False,
                    force=False, loader=None):
    """One scoped catalogue for runtime, owner diagnostics and legacy helpers."""
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.llm_catalogue import safe_code
    try:
        policy = policy or llm_safety.parse_policy()
        discovery = policy["routes"].get("model_discovery")
        if discovery is None:
            llm_safety._deny("route_missing")
        if discovery["provider"] != "anthropic":
            llm_safety._deny("provider_unsupported")
        exact = llm_safety._credential(discovery)
        if credential is not None and credential != exact:
            llm_safety._deny("credential_mismatch")
        fingerprint = llm_safety.credential_fingerprint("anthropic", exact)
        key = f"mojo:llm:catalogue:v2:{policy['hash']}:anthropic:{fingerprint}"
        if redis is None:
            from mojo.helpers.redis import get_connection
            try:
                redis = get_connection()
            except Exception:
                redis = None
        return _catalogue.state(
            redis, key, refresh=refresh, force=force,
            loader=loader or (lambda: llm_safety.discover_models(
                expected_policy_hash=policy["hash"], expected_fingerprint=fingerprint)))
    except Exception as error:
        code = safe_code(error)
        return {"models": [], "discovery": {
            "status": "unavailable", "fetched_at": None, "attempted_at": None,
            "expires_at": None, "max_stale_at": None, "error_code": code},
            "refresh": {"attempted": False, "ok": False if refresh else None,
                        "code": code if refresh else ""}}


def _cache_get():
    return catalogue_state()["models"] or None


def get_models(force_refresh=False):
    """Bounded complete catalogue; refresh on expiry, using stored credentials."""
    return catalogue_state(refresh=True, force=force_refresh)["models"] or None


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
    except Exception:
        logger.warning("Model catalogue unavailable for the picker")
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


def get_model(use="general", *, loader=None, pin_loader=None):
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
    read_pin = pin_loader or settings.get
    pinned = read_pin("LLM_ADMIN_MODEL", None)
    if not pinned:
        pinned = read_pin("LLM_HANDLER_MODEL", None)
    if pinned:
        return pinned

    # 2. Auto-detect from API
    family_keyword = _USE_TO_FAMILY.get(use)
    if not family_keyword:
        logger.warning(f"Unknown model tier '{use}' — using 'general'")
        use = "general"
        family_keyword = _USE_TO_FAMILY[use]
    models = (loader or get_models)()
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
         feature=None, operation="call", context=None, reasoning_effort="off"):
    """
    Call the installation's exact policy route.

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
            operation=operation, context=context, reasoning_effort=reasoning_effort)
    except llm_safety.LLMSafetyError as err:
        raise LLMExecutionError(err.code, retry_after=err.retry_after) from None
    except Exception:
        raise LLMExecutionError("safety_unavailable") from None

    # Warn once per worker if caching is enabled but produced no cache activity.
    # Typically means the prefix is below the model's minimum cacheable size
    # (1024 tokens for Sonnet, 4096 for Opus).
    if result.get("provider") != "bedrock":
        _warn_zero_cache(result, result.get("model") or resolved_model, cache_enabled)

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
        operation="ask", context=None, reasoning_effort="off"):
    """
    One-shot LLM question — send a prompt, get a string back.

    Good for summarization, classification, text generation, etc.
    No tools, no conversation history.
    """
    messages = [{"role": "user", "content": prompt}]
    response = call(
        messages, system=system, model=model, max_tokens=max_tokens,
        feature=feature, operation=operation, context=context,
        reasoning_effort=reasoning_effort)
    # Extract text from response content blocks
    parts = []
    for block in response.get("content", []):
        if block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts)
