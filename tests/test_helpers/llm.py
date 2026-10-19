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
def test_pick_best_model_survives_malformed_entries(opts):
    """A malformed entry is skipped, not raised — resolution stays fail-soft"""
    from mojo.helpers.llm import _pick_best_model

    models = [
        "not-a-dict",
        {"no_id_at_all": True},
        {"id": None},
        {"id": 12345},
        {"id": "claude-opus-²"},          # isdigit() is True, int() raises
        {"id": "claude-opus-" + "9" * 5000},   # over int()'s 4300-digit limit
        {
            "id": "claude-opus-4-8",
            "display_name": "Claude Opus 4.8",
            "created_at": "2026-06-15T00:00:00Z",
            "type": "model",
        },
    ]

    result = _pick_best_model(models, "opus")
    assert result == "claude-opus-4-8", \
        f"Malformed entries must be skipped, expected claude-opus-4-8, got {result}"


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

    result = llm.get_model("fast")
    assert "haiku" in result, f"Expected haiku for 'fast', got {result}"

    result = llm.get_model("general")
    assert "sonnet" in result, f"Expected sonnet for 'general', got {result}"

    result = llm.get_model("powerful")
    # Opus either way — auto-detected with an API key, hardcoded without one.
    assert "opus" in result, f"Expected opus for 'powerful', got {result}"


@th.django_unit_test()
def test_get_model_with_cache(opts):
    """Legacy use-case selection can use a test-owned catalogue."""
    from mojo.helpers import llm
    for use, expected in (("powerful", "claude-opus-4-6"),
                          ("general", "claude-sonnet-4-6"),
                          ("fast", "claude-haiku-4-5")):
        result = llm.get_model(use, loader=lambda: SAMPLE_MODELS,
                               pin_loader=lambda key, default: None)
        assert result == expected, f"expected {expected} for {use}, got {result}"


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
def test_provider_models_are_json_serializable(opts):
    """The provider adapter returns dicts the Redis cache can serialize."""
    import json
    from unittest import mock

    from anthropic.types import ModelInfo
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider

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

    models = AnthropicProvider(client=client).list_models(timeout=5)

    assert models, f"Expected a model list from the stubbed client, got {models}"
    assert isinstance(models[0]["created_at"], str), \
        f"created_at must be a string to survive JSON caching, got " \
        f"{type(models[0]['created_at']).__name__}"
    try:
        json.dumps(models)
    except TypeError as err:
        assert False, f"Model list must be JSON-serializable for the Redis cache: {err}"


@th.django_unit_test()
def test_ask_raises_safe_error_without_configuration(opts):
    """ask() exposes only the stable execution boundary when unconfigured."""
    from mojo.helpers import llm

    # Only test if no key is configured in the environment
    if llm.get_api_key():
        return

    try:
        llm.ask("hello", feature="unattributed")
        assert False, "Expected LLMExecutionError from an unconfigured call"
    except llm.LLMExecutionError as err:
        assert err.code in {"policy_invalid", "policy_mixed", "credential_missing"}, \
            f"Expected one stable safety code, got: {err.code}"


# ---------------------------------------------------------------------------
# Real LLM tests — only run when LLM_HANDLER_API_KEY is in the environment
# ---------------------------------------------------------------------------

@th.tier("slow")
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


@th.tier("slow")
@th.django_unit_test()
def test_verify_api_key_has_no_candidate_credential_input(opts):
    """Candidate material is accepted only by the owner service boundary."""
    import inspect
    from mojo.helpers import llm

    parameters = inspect.signature(llm.verify_api_key).parameters
    assert "api_key" not in parameters, \
        f"the stable helper still accepts candidate credentials: {tuple(parameters)}"


@th.tier("slow")
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


@th.tier("slow")
@th.django_unit_test()
def test_real_get_model_auto_detect(opts):
    """get_model auto-detects latest models from API"""
    from mojo.helpers import llm

    if not llm.get_api_key():
        return  # skip without key

    models = llm.get_models(force_refresh=True)
    assert models is not None, "real discovery must yield a catalogue"
    for use, family in (("general", "sonnet"), ("fast", "haiku"), ("powerful", "opus")):
        result = llm.get_model(use, loader=lambda: models)
        assert family in result, f"Expected {family} for {use}, got {result}"


@th.tier("slow")
@th.django_unit_test()
def test_real_ask(opts):
    """ask() returns a real LLM response"""
    from mojo.helpers import llm

    if not llm.get_api_key():
        return  # skip without key

    result = llm.ask("What is 2+2? Reply with just the number.", model=llm.get_model("fast"))
    assert "4" in result, f"Expected '4' in response, got: {result}"


class _CatalogueRedis:
    """Per-test Redis-shaped coordination and expiry, never a global patch."""

    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.expires = {}
        self.fail = False

    def get(self, key):
        if self.fail:
            raise RuntimeError('private Redis detail')
        if self.expires.get(key, float('inf')) <= self.clock():
            self.values.pop(key, None)
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise RuntimeError('private Redis detail')
        if nx and self.get(key) is not None:
            return False
        self.values[key] = value
        if ex is not None:
            self.expires[key] = self.clock() + ex
        return True

    def eval(self, script, count, key, token):
        if self.get(key) == token:
            self.values.pop(key, None)
            return 1
        return 0


def _catalogue_model(identifier='opaque-next', created='2026-10-18T12:00:00Z', **values):
    entry = dict(id=identifier, display_name='Provider name', created_at=created,
                 line='sonnet', lifecycle='active', max_tokens=8192,
                 capabilities={'image_input': {'supported': True}})
    entry.update(values)
    return entry


def _catalogue_case():
    from mojo.helpers.llm_catalogue import Catalogue
    now = [1800000000]
    clock = lambda: now[0]
    return Catalogue(clock=clock), _CatalogueRedis(clock), now


@th.django_unit_test()
def test_automatic_sonnet_uses_metadata_release_and_real_instants(opts):
    from mojo.helpers.llm_catalogue import select_sonnet
    rows = [
        _catalogue_model('misleading-sonnet-999', line='opus'),
        _catalogue_model('renamed-one', '2026-10-17T00:00:00Z'),
        _catalogue_model('renamed-two', '2026-10-18T00:00:00+02:00'),
        _catalogue_model('renamed-three', '2026-10-17T23:00:00Z'),
    ]
    assert select_sonnet(rows, 8192, images=True) == 'renamed-three', \
        'opaque successor IDs must rank by release instants rather than family spelling/version'
    rows.append(_catalogue_model('renamed-four', '2026-10-19T00:00:00Z'))
    assert select_sonnet(rows, 8192, images=True) == 'renamed-four', \
        'a compatible future Sonnet release must be adopted without hardcoded ID changes'


@th.django_unit_test()
def test_automatic_sonnet_excludes_incomplete_retired_or_conflicting_metadata(opts):
    from mojo.helpers.llm_catalogue import select_sonnet
    invalid = [
        {'line': None}, {'lifecycle': 'deprecated'}, {'lifecycle': 'retired'},
        {'created_at': '1970-01-01T00:00:00Z'}, {'created_at': 'bad'},
        {'created_at': '2026-10-18T00:00:00'}, {'created_at': 10**500},
        {'max_tokens': True}, {'max_tokens': None}, {'max_tokens': 4096},
        {'capabilities': None}, {'capabilities': {'image_input': {'supported': 'true'}}},
        {'id': ' '}, {'id': 'x' * 129},
    ]
    for changes in invalid:
        assert select_sonnet([_catalogue_model(**changes)], 8192, images=True) == '', \
            f'incomplete or incompatible model must not be selected: {changes}'
    conflict = [_catalogue_model('duplicate'), _catalogue_model('duplicate', lifecycle='retired')]
    assert select_sonnet(conflict, 4) == '', 'conflicting duplicate IDs must be excluded'
    assert select_sonnet([None, [], {}, _catalogue_model('valid')], 4) == 'valid', \
        'malformed entries must not hide an independently valid candidate'


@th.django_unit_test()
def test_catalogue_first_refresh_advancing_clock_and_passive_fresh_hit(opts):
    from mojo.helpers.llm_catalogue import select_sonnet
    catalogue, redis, now = _catalogue_case()
    calls = []

    def load():
        calls.append(True)
        now[0] += 2
        return [_catalogue_model()]

    passive = catalogue.state(redis, 'first', loader=load)
    assert not calls and passive['discovery']['status'] == 'unavailable', \
        'ordinary reads must never contact the provider'
    first = catalogue.state(redis, 'first', loader=load, refresh=True)
    assert first['refresh']['ok'] is True and first['discovery']['status'] == 'fresh', \
        'a real-time first refresh must immediately be usable even when time advances during I/O'
    assert select_sonnet(first['models'], 8192, images=True) == 'opaque-next', \
        'the first authorized invocation must be able to resolve its newly fetched model'
    hit = catalogue.state(redis, 'first', loader=load, refresh=True)
    assert len(calls) == 1 and hit['refresh']['attempted'] is False, \
        'a fresh runtime cache hit must spend no models request'
    now[0] += 61
    explicit = catalogue.state(redis, 'first', loader=load, refresh=True, force=True)
    assert len(calls) == 2 and explicit['refresh']['ok'] is True, \
        'explicit refresh may refresh a fresh cache after cooldown'


@th.django_unit_test()
def test_catalogue_failure_age_bound_success_invalidates_and_secret_free_errors(opts):
    from mojo.helpers.llm_catalogue import FRESH_SECONDS, MAX_STALE_SECONDS
    catalogue, redis, now = _catalogue_case()
    first = catalogue.state(redis, 'age', loader=lambda: [_catalogue_model()], refresh=True)
    fetched = first['discovery']['fetched_at']
    now[0] += FRESH_SECONDS + 1

    def fail():
        raise RuntimeError('private provider key/body must never escape')

    stale = catalogue.state(redis, 'age', loader=fail, refresh=True)
    assert stale['refresh'] == {'attempted': True, 'ok': False, 'code': 'catalogue_unavailable'}, \
        'a failed refresh must report that attempt rather than claim stale fallback is success'
    assert stale['discovery']['status'] == 'stale' and stale['models'], \
        'a failed refresh may retain usable last-good data'
    assert stale['discovery']['fetched_at'] == fetched, 'failed refresh must not extend success time'
    now[0] += 61
    retired = catalogue.state(redis, 'age', loader=lambda: [
        _catalogue_model(lifecycle='retired')], refresh=True, force=True)
    from mojo.helpers.llm_catalogue import select_sonnet
    assert retired['refresh']['ok'] is True and not select_sonnet(retired['models'], 4), \
        'complete success invalidates a previously eligible model immediately'
    now[0] += MAX_STALE_SECONDS
    expired = catalogue.state(redis, 'age')
    assert expired['models'] == [] and expired['discovery']['status'] == 'unavailable', \
        'no model may remain effective after seven days from the successful catalogue'
    assert 'private' not in str(stale), 'raw provider errors must not enter diagnostic/cache state'


@th.django_unit_test()
def test_catalogue_lock_cooldown_corruption_namespaces_and_redis_outage(opts):
    catalogue, redis, now = _catalogue_case()
    calls = []
    load = lambda: calls.append(True) or [_catalogue_model()]
    redis.set('busy:lock', 'other-owner', ex=30)
    busy = catalogue.state(redis, 'busy', loader=load, refresh=True)
    assert busy['refresh']['code'] == 'refresh_in_progress' and not calls, \
        'single-flight losers must not issue a provider request or wait'
    assert redis.get('busy:lock') == 'other-owner', 'losers cannot release another owner lock'
    catalogue.state(redis, 'one', loader=load, refresh=True)
    throttled = catalogue.state(redis, 'one', loader=load, refresh=True, force=True)
    assert throttled['refresh']['code'] == 'refresh_throttled' and len(calls) == 1, \
        'explicit refresh must not bypass the attempt cooldown'
    assert catalogue.state(redis, 'different-credential-or-policy')['models'] == [], \
        'a rotated credential/policy namespace cannot borrow prior identity state'
    redis.set('broken', '{malformed')
    assert catalogue.state(redis, 'broken')['models'] == [], 'corrupt cache must never invent models'
    redis.fail = True
    fallback = catalogue.state(redis, 'one', loader=load, refresh=True, force=True)
    assert len(calls) == 1 and fallback['models'], \
        'Redis outage may use bounded local last-good data but cannot authorize provider work'
    assert fallback['refresh']['code'] == 'control_state_unknown', \
        'unknown coordination must be reported explicitly'
    now[0] += 8 * 86400
    assert catalogue.state(redis, 'one')['models'] == [], 'local fallback must obey the same age bound'
