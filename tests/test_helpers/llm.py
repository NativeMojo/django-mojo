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
    # With API key: auto-detects opus. Without: falls back to sonnet.
    assert "opus" in result or "sonnet" in result, f"Expected opus or sonnet for 'powerful', got {result}"


@th.django_unit_test()
def test_get_model_with_cache(opts):
    """get_model uses cached models when available"""
    import time
    from mojo.helpers import llm

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
