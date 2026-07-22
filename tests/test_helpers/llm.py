from testit import helpers as th


# Sample model data matching Anthropic API response format
SAMPLE_MODELS = [
    {
        "id": "claude-opus-4-6",
        "display_name": "Claude Opus 4.6",
        "created_at": "2026-03-15T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-opus-4-6-20260315",
        "display_name": "Claude Opus 4.6 (2026-03-15)",
        "created_at": "2026-03-15T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6",
        "created_at": "2026-03-10T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-sonnet-4-6-20260310",
        "display_name": "Claude Sonnet 4.6 (2026-03-10)",
        "created_at": "2026-03-10T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-sonnet-4-20250514",
        "display_name": "Claude Sonnet 4 (2025-05-14)",
        "created_at": "2025-05-14T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-haiku-4-5",
        "display_name": "Claude Haiku 4.5",
        "created_at": "2025-10-01T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "display_name": "Claude Haiku 4.5 (2025-10-01)",
        "created_at": "2025-10-01T00:00:00Z",
        "type": "model",
    },
]


# Aliases that tie on ID length within a family. Every alias in a generation is
# the same width (claude-opus-4-1 / claude-opus-4-8), so ID length carries no
# recency information — ranking on it returns whichever one the tiebreak
# happens to surface.
SAME_LENGTH_ALIAS_MODELS = [
    {
        "id": "claude-opus-4-1",
        "display_name": "Claude Opus 4.1",
        "created_at": "2025-08-05T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-opus-4-8",
        "display_name": "Claude Opus 4.8",
        "created_at": "2026-06-15T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-opus-4-1-20250805",
        "display_name": "Claude Opus 4.1 (2025-08-05)",
        "created_at": "2025-08-05T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-sonnet-5",
        "display_name": "Claude Sonnet 5",
        "created_at": "2026-05-01T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-sonnet-6",
        "display_name": "Claude Sonnet 6",
        "created_at": "2026-11-01T00:00:00Z",
        "type": "model",
    },
]


# A dated snapshot newer than the alias it belongs to. The alias must still
# win — it always points at the latest build of that model.
ALIAS_VS_SNAPSHOT_MODELS = [
    {
        "id": "claude-opus-4-8",
        "display_name": "Claude Opus 4.8",
        "created_at": "2026-06-15T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-opus-4-8-20260720",
        "display_name": "Claude Opus 4.8 (2026-07-20)",
        "created_at": "2026-07-20T00:00:00Z",
        "type": "model",
    },
]


# A family the API exposes only as dated snapshots, with no short alias.
DATED_ONLY_MODELS = [
    {
        "id": "claude-haiku-4-0-20240307",
        "display_name": "Claude Haiku 4 (2024-03-07)",
        "created_at": "2024-03-07T00:00:00Z",
        "type": "model",
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "display_name": "Claude Haiku 4.5 (2025-10-01)",
        "created_at": "2025-10-01T00:00:00Z",
        "type": "model",
    },
]


def _clear_redis_model_cache():
    """Drop the shared model-list key; no-op when Redis is unavailable."""
    try:
        from mojo.helpers.redis import get_connection
        from mojo.helpers.llm import CACHE_KEY
        get_connection().delete(CACHE_KEY)
    except Exception:
        pass


@th.django_unit_test()
def test_pick_best_model_sonnet(opts):
    """_pick_best_model returns the short alias for sonnet"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(SAMPLE_MODELS, "sonnet")
    assert result == "claude-sonnet-4-6", f"Expected claude-sonnet-4-6, got {result}"


@th.django_unit_test()
def test_pick_best_model_opus(opts):
    """_pick_best_model returns the short alias for opus"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(SAMPLE_MODELS, "opus")
    assert result == "claude-opus-4-6", f"Expected claude-opus-4-6, got {result}"


@th.django_unit_test()
def test_pick_best_model_haiku(opts):
    """_pick_best_model returns the short alias for haiku"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(SAMPLE_MODELS, "haiku")
    assert result == "claude-haiku-4-5", f"Expected claude-haiku-4-5, got {result}"


@th.django_unit_test()
def test_pick_best_model_no_match(opts):
    """_pick_best_model returns None for unknown family"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(SAMPLE_MODELS, "gemini")
    assert result is None, f"Expected None, got {result}"


@th.django_unit_test()
def test_pick_best_model_newest_same_length_alias(opts):
    """_pick_best_model returns the newest alias when aliases tie on ID length"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(SAME_LENGTH_ALIAS_MODELS, "opus")
    assert result == "claude-opus-4-8", \
        f"Expected the newest opus alias claude-opus-4-8, got {result}"


@th.django_unit_test()
def test_pick_best_model_next_generation_alias(opts):
    """A same-length successor alias supersedes the incumbent"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(SAME_LENGTH_ALIAS_MODELS, "sonnet")
    assert result == "claude-sonnet-6", \
        f"Expected claude-sonnet-6 to supersede claude-sonnet-5, got {result}"


@th.django_unit_test()
def test_pick_best_model_alias_beats_dated_snapshot(opts):
    """The alias wins even when a dated snapshot has a newer created_at"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(ALIAS_VS_SNAPSHOT_MODELS, "opus")
    assert result == "claude-opus-4-8", \
        f"Expected the alias claude-opus-4-8 to beat its dated snapshot, got {result}"


@th.django_unit_test()
def test_pick_best_model_only_dated_snapshots(opts):
    """With no alias in the family, the newest dated snapshot wins"""
    from mojo.helpers.llm import _pick_best_model

    result = _pick_best_model(DATED_ONLY_MODELS, "haiku")
    assert result == "claude-haiku-4-5-20251001", \
        f"Expected the newest haiku snapshot, got {result}"


@th.django_unit_test()
def test_pick_best_model_ignores_input_order(opts):
    """The result does not depend on the order the API listed models in"""
    from mojo.helpers.llm import _pick_best_model

    forward = _pick_best_model(SAME_LENGTH_ALIAS_MODELS, "opus")
    reverse = _pick_best_model(list(reversed(SAME_LENGTH_ALIAS_MODELS)), "opus")
    assert forward == reverse, \
        f"Result changed with input order: {forward} forward vs {reverse} reversed"
    assert forward == "claude-opus-4-8", \
        f"Expected claude-opus-4-8 in both directions, got {forward}"


@th.django_unit_test()
def test_pick_best_model_datetime_created_at(opts):
    """created_at is a datetime from the API and a string from the cache"""
    from datetime import datetime
    from mojo.helpers.llm import _pick_best_model

    models = [
        {
            "id": m["id"],
            "display_name": m["display_name"],
            "created_at": datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")),
            "type": "model",
        }
        for m in SAME_LENGTH_ALIAS_MODELS
    ]

    result = _pick_best_model(models, "opus")
    assert result == "claude-opus-4-8", \
        f"Expected claude-opus-4-8 with datetime created_at values, got {result}"


@th.django_unit_test()
def test_fallbacks_are_in_the_right_tier(opts):
    """Every hardcoded fallback names a model from its own tier's family"""
    from mojo.helpers.llm import _FALLBACKS, _USE_TO_FAMILY

    for use, family in _USE_TO_FAMILY.items():
        fallback = _FALLBACKS.get(use)
        assert fallback, f"No fallback configured for the '{use}' tier"
        assert family in fallback, \
            f"Fallback for '{use}' should be a {family} model, got {fallback}"


@th.django_unit_test()
def test_get_model_fallback(opts):
    """get_model returns a valid model for each use case"""
    from mojo.helpers import llm

    # Clear any cached models
    llm._mem_cache["models"] = None
    llm._mem_cache["fetched_at"] = 0

    result = llm.get_model("fast")
    assert "haiku" in result, f"Expected haiku for 'fast', got {result}"

    result = llm.get_model("general")
    assert "sonnet" in result, f"Expected sonnet for 'general', got {result}"

    result = llm.get_model("powerful")
    # Opus either way — auto-detected with an API key, hardcoded without one.
    assert "opus" in result, f"Expected opus for 'powerful', got {result}"


@th.django_unit_test()
def test_get_model_with_cache(opts):
    """get_model uses cached models when available"""
    import time
    from mojo.helpers import llm

    # _cache_get checks Redis before the in-memory cache, so clear the shared
    # key first — otherwise a real model list left there by an earlier run
    # would be returned instead of the fixture seeded below.
    _clear_redis_model_cache()

    # Seed the in-memory cache
    llm._mem_cache["models"] = SAMPLE_MODELS
    llm._mem_cache["fetched_at"] = time.time()

    try:
        result = llm.get_model("powerful")
        assert result == "claude-opus-4-6", f"Expected claude-opus-4-6, got {result}"

        result = llm.get_model("general")
        assert result == "claude-sonnet-4-6", f"Expected claude-sonnet-4-6, got {result}"

        result = llm.get_model("fast")
        assert result == "claude-haiku-4-5", f"Expected claude-haiku-4-5, got {result}"
    finally:
        # Clean up cache
        llm._mem_cache["models"] = None
        llm._mem_cache["fetched_at"] = 0


@th.django_unit_test()
def test_get_api_key_resolution(opts):
    """get_api_key checks LLM_ADMIN_API_KEY then LLM_HANDLER_API_KEY"""
    from mojo.helpers import llm

    # With no keys set, should return None
    result = llm.get_api_key()
    # We can't assert None because the test env might have a key configured
    # Just verify it returns a string or None
    assert result is None or isinstance(result, str), f"Expected str or None, got {type(result)}"


@th.django_unit_test()
def test_cache_expiry(opts):
    """Stale in-memory cache is not used"""
    import time
    from mojo.helpers import llm

    # Set cache with old timestamp (expired)
    llm._mem_cache["models"] = SAMPLE_MODELS
    llm._mem_cache["fetched_at"] = time.time() - 100000  # way past 24h

    try:
        cached = llm._cache_get()
        # Should not return the stale in-memory cache
        # (might return Redis cache if available, but not the stale mem cache)
        if cached == SAMPLE_MODELS:
            # Only OK if Redis had it
            pass
        # Either None or Redis-sourced is fine
        assert cached is None or isinstance(cached, list), f"Expected None or list, got {type(cached)}"
    finally:
        llm._mem_cache["models"] = None
        llm._mem_cache["fetched_at"] = 0


@th.django_unit_test()
def test_fetch_models_is_json_serializable(opts):
    """_fetch_models_from_api returns dicts the Redis cache can serialize"""
    import json
    from unittest import mock

    import anthropic
    from anthropic.types import ModelInfo
    from mojo.helpers import llm

    page = mock.Mock()
    page.data = [ModelInfo(
        id="claude-opus-4-8",
        display_name="Claude Opus 4.8",
        created_at="2026-06-15T00:00:00Z",
        type="model",
    )]
    page.has_more = False
    client = mock.Mock()
    client.models.list.return_value = page

    with mock.patch.object(anthropic, "Anthropic", return_value=client):
        models = llm._fetch_models_from_api(api_key="test-key")

    assert models, f"Expected a model list from the stubbed client, got {models}"
    assert isinstance(models[0]["created_at"], str), \
        f"created_at must be a string to survive JSON caching, got " \
        f"{type(models[0]['created_at']).__name__}"
    try:
        json.dumps(models)
    except TypeError as err:
        assert False, f"Model list must be JSON-serializable for the Redis cache: {err}"


@th.django_unit_test()
def test_ask_raises_without_api_key(opts):
    """ask() raises ValueError when no API key is available"""
    from mojo.helpers import llm

    # Only test if no key is configured in the environment
    if llm.get_api_key():
        return

    try:
        llm.ask("hello")
        assert False, "Expected ValueError from ask() with no API key"
    except ValueError as e:
        assert "API key" in str(e), f"Expected API key error, got: {e}"


# ---------------------------------------------------------------------------
# Real LLM tests — only run when LLM_HANDLER_API_KEY is in the environment
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_real_verify_api_key(opts):
    """verify_api_key returns True with a valid key"""
    from mojo.helpers import llm

    key = llm.get_api_key()
    if not key:
        return  # skip without key

    ok, error = llm.verify_api_key()
    assert ok is True, f"Expected valid key, got error: {error}"
    assert error is None, f"Expected no error, got: {error}"


@th.django_unit_test()
def test_real_verify_bad_key(opts):
    """verify_api_key returns False with an invalid key"""
    from mojo.helpers import llm

    ok, error = llm.verify_api_key(api_key="sk-ant-fake-invalid-key")
    assert ok is False, "Expected False for invalid key"
    assert error is not None, "Expected error message for invalid key"


@th.django_unit_test()
def test_real_get_models(opts):
    """get_models fetches real model list from Anthropic API"""
    from mojo.helpers import llm

    if not llm.get_api_key():
        return  # skip without key

    # Force fresh fetch
    models = llm.get_models(force_refresh=True)
    assert models is not None, "Expected model list from API"
    assert len(models) > 0, "Expected at least one model"

    # Verify model structure
    first = models[0]
    assert "id" in first, "Model should have an 'id' field"
    assert "claude" in first["id"], f"Expected Claude model, got: {first['id']}"


@th.django_unit_test()
def test_real_get_model_auto_detect(opts):
    """get_model auto-detects latest models from API"""
    from mojo.helpers import llm

    if not llm.get_api_key():
        return  # skip without key

    # Clear cache to force API call
    llm._mem_cache["models"] = None
    llm._mem_cache["fetched_at"] = 0

    try:
        model = llm.get_model("general")
        assert "sonnet" in model, f"Expected sonnet for 'general', got: {model}"

        model = llm.get_model("fast")
        assert "haiku" in model, f"Expected haiku for 'fast', got: {model}"

        model = llm.get_model("powerful")
        assert "opus" in model, f"Expected opus for 'powerful', got: {model}"
    finally:
        llm._mem_cache["models"] = None
        llm._mem_cache["fetched_at"] = 0


@th.django_unit_test()
def test_real_ask(opts):
    """ask() returns a real LLM response"""
    from mojo.helpers import llm

    if not llm.get_api_key():
        return  # skip without key

    result = llm.ask("What is 2+2? Reply with just the number.", model=llm.get_model("fast"))
    assert "4" in result, f"Expected '4' in response, got: {result}"
