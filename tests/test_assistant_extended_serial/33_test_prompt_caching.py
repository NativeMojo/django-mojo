"""
Prompt-caching tests moved from tests/test_assistant/33_test_prompt_caching.py
— they mock.patch the shared settings singleton
(mojo.helpers.settings.settings.get) around in-process llm/agent calls, which
is unsafe under the parallel default tier (maestro item #1839). Runs opt-in
(`extended`) and serial.

Item #2558 moved `test_zero_usage_warning_fires_once` here too: it asserts a
once-PER-PROCESS warning, so it resets the module-global
`mojo.helpers.llm._zero_cache_warned` guard and attaches a handler to the
shared `llm` logger. The process-globalness is the assertion, so there is no
seam to convert it to.

Covers:
- AnthropicProvider omits cache_control when caching is disabled
- run_assistant() persists summed usage on the final Message
- per-turn cache usage is logged to assistant.log
- a zero-cache-usage first call logs a one-time, once-per-process warning
"""
import logging
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = 'cache-test-serial-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user(opts):
    from mojo.apps.account.models import User
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD,
    )
    opts.user.is_email_verified = True
    opts.user.save()
    opts.user.add_permission("view_admin")


class _FakeMessagesAPI:
    """Capture kwargs passed to client.messages.create and return a canned dict."""

    def __init__(self, response_payload):
        self.response_payload = response_payload
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(self.response_payload)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


def _make_fake_client(response_payload):
    """Build a fake anthropic.Anthropic instance whose messages.create is captured."""
    fake_messages = _FakeMessagesAPI(response_payload)
    fake_client = mock.MagicMock()
    fake_client.messages = fake_messages
    return fake_client, fake_messages


def _canned_response(content_text="hello", usage=None):
    """Build a minimal Anthropic response payload dict."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content_text}],
        "model": "claude-sonnet-4-test",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage if usage is not None else {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


class _ListHandler(logging.Handler):
    """Simple log handler that captures records into a list."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@th.django_unit_test()
def test_llm_helper_omits_cache_control_when_disabled(opts):
    """The adapter should omit cache_control when the guard disables it."""
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider

    fake_client, fake_messages = _make_fake_client(_canned_response())

    AnthropicProvider(client=fake_client).call(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-test",
        max_tokens=64,
        cache_enabled=False,
    )

    sent = fake_messages.last_kwargs
    assert_true(sent is not None, "messages.create should have been called")
    assert_true(
        "cache_control" not in sent,
        f"cache_control should be absent when disabled, got {list(sent.keys())}",
    )


@th.django_unit_test()
def test_assistant_persists_usage_on_final_message(opts):
    """run_assistant() should sum usage across turns and store on the final Message."""
    from mojo.apps.assistant.services import agent
    from mojo.apps.assistant.models import Message
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.settings import settings as settings_obj

    real_get = settings_obj.get
    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        if name == "LLM_ADMIN_API_KEY":
            return "sk-fake"
        return real_get(name, *args, **kwargs)

    # Two-turn run: first turn uses a tool, second turn ends with text.
    turn_1 = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-test",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 0,
        },
    }

    with mock.patch.object(settings_obj, "get", side_effect=patched_get), \
            mock.patch.object(
                llm_safety, "route_state", return_value={"ready": True}), \
            mock.patch.object(
                agent.llm, "call", return_value=turn_1) as mock_call:
        result = agent.run_assistant(opts.user, "hello")

    assert_true(mock_call.called, "agent should have called llm.call")
    assert_true("usage" in result, f"result dict should include usage, got keys {list(result.keys())}")
    assert_eq(
        result["usage"]["cache_creation_input_tokens"], 2000,
        f"usage cache_creation_input_tokens should match, got {result['usage']}",
    )

    # The final assistant Message should have the same usage stored.
    msg = Message.objects.filter(
        conversation_id=result["conversation_id"], role="assistant",
    ).order_by("-created").first()
    assert_true(msg is not None, "final assistant message should exist")
    assert_true(msg.usage is not None, f"Message.usage should be populated, got {msg.usage!r}")
    assert_eq(
        msg.usage["cache_creation_input_tokens"], 2000,
        f"Message.usage cache_creation_input_tokens should be 2000, got {msg.usage}",
    )
    assert_eq(
        msg.usage["output_tokens"], 5,
        f"Message.usage output_tokens should be 5, got {msg.usage}",
    )


@th.django_unit_test()
def test_assistant_logs_per_turn_cache_usage(opts):
    """An INFO log line per turn should report cache_read/cache_write/input/output."""
    from mojo.apps.assistant.services import agent
    from mojo.apps.account.services import llm_safety
    from mojo.helpers.settings import settings as settings_obj

    real_get = settings_obj.get
    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        if name == "LLM_ADMIN_API_KEY":
            return "sk-fake"
        return real_get(name, *args, **kwargs)

    turn = {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-test", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {
            "input_tokens": 7, "output_tokens": 3,
            "cache_creation_input_tokens": 1234, "cache_read_input_tokens": 999,
        },
    }

    # logit.get_logger() returns a wrapper; the real stdlib logger lives on
    # the ``.logger`` attribute. Handler/level go on that.
    stdlib_logger = agent.logger.logger
    handler = _ListHandler()
    stdlib_logger.addHandler(handler)
    prev_level = stdlib_logger.level
    stdlib_logger.setLevel(logging.INFO)
    try:
        with mock.patch.object(settings_obj, "get", side_effect=patched_get), \
                mock.patch.object(
                    llm_safety, "route_state", return_value={"ready": True}), \
                mock.patch.object(agent.llm, "call", return_value=turn):
            agent.run_assistant(opts.user, "hi")
    finally:
        stdlib_logger.removeHandler(handler)
        stdlib_logger.setLevel(prev_level)

    matches = [
        r for r in handler.records
        if "llm turn" in r.getMessage() and "cache_read=999" in r.getMessage()
    ]
    assert_true(
        len(matches) >= 1,
        f"Expected at least one INFO log with 'llm turn ... cache_read=999', got {[r.getMessage() for r in handler.records]}",
    )


# ---------------------------------------------------------------------------
# Zero-cache warning
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_zero_usage_warning_fires_once(opts):
    """When caching is enabled but both counters are 0, WARN once per process.

    Moved here from tests/test_assistant/33_test_prompt_caching.py (maestro
    item #2558): the contract is once-PER-PROCESS, so the test must reset the
    module-global ``llm._zero_cache_warned`` guard and attach a handler to the
    shared ``llm`` logger. Both are process-global mutations of
    ``mojo.helpers.llm``, unsafe in the parallel default tier, and neither is
    convertible to a seam — the process-globalness IS what is asserted.
    """
    from mojo.helpers import llm

    # Reset the process-level guard so this test is deterministic.
    llm._zero_cache_warned = False

    response = _canned_response(usage={
        "input_tokens": 5, "output_tokens": 3,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })

    stdlib_logger = llm.logger.logger
    handler = _ListHandler()
    stdlib_logger.addHandler(handler)
    prev_level = stdlib_logger.level
    stdlib_logger.setLevel(logging.WARNING)

    try:
        # Two guarded-call results — both return zero cache counters.
        llm._warn_zero_cache(response, "m", True)
        llm._warn_zero_cache(response, "m", True)
    finally:
        stdlib_logger.removeHandler(handler)
        stdlib_logger.setLevel(prev_level)
        llm._zero_cache_warned = False  # restore for other tests

    warnings = [r for r in handler.records if r.levelno == logging.WARNING and "caching" in r.getMessage().lower()]
    assert_eq(
        len(warnings), 1,
        f"WARNING should fire exactly once across 2 zero-cache calls, got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]}",
    )


@th.django_unit_test()
@th.requires_app('mojo.apps.assistant')
def test_normal_agent_leaves_resolved_model_to_shared_guard(opts):
    from mojo.apps.account.services import llm_safety
    from mojo.apps.assistant.services import agent
    from mojo.helpers.settings import settings
    original = settings.get
    reply = _canned_response('guard-selected answer')
    reply['model'] = 'opaque-provider-successor'
    with mock.patch.object(settings, 'get', side_effect=lambda name, *args, **kwargs:
                           True if name == 'LLM_ADMIN_ENABLED' else original(name, *args, **kwargs)), \
            mock.patch.object(llm_safety, 'route_state', return_value={'ready': True}), \
            mock.patch.object(llm_safety, 'invoke', return_value=reply) as guarded:
        result = agent.run_assistant(opts.user, 'Use the current model')
    assert 'error' not in result and guarded.call_count == 1, \
        'normal Assistant loop must use the shared guarded call exactly once for a terminal turn'
    assert guarded.call_args.kwargs['feature'] == 'assistant' \
        and guarded.call_args.kwargs.get('model') is None, \
        'normal transport must not substitute a legacy/default model for the guarded resolver'
    assert guarded.call_args.kwargs['context']['operation_id'], \
        'normal loop must keep its stable operation identity through the shared guard'


def _automatic_entry_case(expired=False):
    """Actual entry/guard integration with only provider and control-plane fakes."""
    from contextlib import contextmanager

    @contextmanager
    def configured():
        import time
        import uuid
        from mojo.apps.account.models import LLMCircuitBreaker, LLMRequest
        from mojo.apps.account.services import llm_safety
        from mojo.helpers import llm
        from mojo.helpers.llm_catalogue import Catalogue
        from mojo.helpers.redis import get_connection
        from mojo.helpers.settings import settings
        limits = dict(requests_minute=60, requests_hour=120, requests_day=240,
                      tokens_minute=2000000, tokens_hour=4000000, tokens_day=8000000,
                      concurrency=2, max_input_bytes=200000, max_output_tokens=8192,
                      timeout_seconds=10, max_loop_calls=5)
        raw = dict(version=1, routes={
            'assistant': dict(provider='anthropic', model='auto:sonnet', credential='admin',
                              capabilities=['text', 'tools', 'images', 'prompt_cache']),
            'model_discovery': dict(provider='anthropic', model='catalogue', credential='admin',
                                    capabilities=['models'])}, shared=limits,
            features={'assistant': dict(limits), 'model_discovery': dict(limits)},
            breaker=dict(auth_failures=2, rate_failures=2, server_failures=2, open_seconds=60))
        marker = uuid.uuid4().hex
        credential = 'test-owned-entry-' + marker
        fingerprint = llm_safety.credential_fingerprint('anthropic', credential)
        policy = llm_safety.parse_policy(raw)
        redis = get_connection()
        key = f"mojo:llm:catalogue:v2:{policy['hash']}:anthropic:{fingerprint}"
        catalogue = Catalogue()
        model = dict(id='entry-successor', display_name='Entry successor', line='sonnet',
                     lifecycle='active', created_at='2026-10-18T00:00:00Z', max_tokens=8192,
                     capabilities={'image_input': {'supported': True}})
        if expired:
            catalogue._remember(key, dict(models=[dict(model, id='expired-old')],
                                          fetched_at=time.time() - 8 * 86400,
                                          attempted_at=None, error_code=''))
        provider = mock.Mock()
        provider.supports.return_value = True
        provider.list_models_page.return_value = dict(data=[model], has_more=False,
                                                       last_id=model['id'])
        reply = _canned_response('actual guarded entry answer')
        reply['model'] = model['id']
        provider.call.return_value = reply
        original_get, original_static = settings.get, settings.get_static

        def get(name, *args, **kwargs):
            if name == 'LLM_ADMIN_ENABLED':
                return True
            if name == 'LLM_ADMIN_API_KEY':
                return credential
            return original_get(name, *args, **kwargs)

        def static(name, *args, **kwargs):
            return raw if name == 'LLM_SAFETY_POLICY' else original_static(name, *args, **kwargs)

        try:
            with mock.patch.object(settings, 'get', side_effect=get), \
                    mock.patch.object(settings, 'get_static', side_effect=static), \
                    mock.patch.object(llm_safety, '_single_primary_value', return_value=policy['hash']), \
                    mock.patch.object(llm_safety, 'emergency_stopped', return_value=False), \
                    mock.patch.object(llm_safety, 'assistant_pin', return_value=('', 'automatic')), \
                    mock.patch.object(llm_safety, '_record_metrics'), \
                    mock.patch.object(llm_safety, '_record_burn'), \
                    mock.patch.object(llm, '_catalogue', catalogue), \
                    mock.patch('mojo.helpers.llm_providers.get_provider', return_value=provider):
                yield dict(provider=provider, model=model['id'], fingerprint=fingerprint)
        finally:
            keys = list(redis.scan_iter(match=f'mojo:llm:anthropic:{fingerprint}:*', count=1000))
            keys.extend(list(redis.scan_iter(match=key + '*', count=1000)))
            if keys:
                redis.delete(*keys)
            LLMRequest.objects.filter(credential_fingerprint=fingerprint).delete()
            LLMCircuitBreaker.objects.filter(credential_fingerprint=fingerprint).delete()
    return configured()


@th.django_unit_test()
@th.requires_app('mojo.apps.assistant')
def test_cold_and_expired_automatic_normal_entry_reaches_guarded_discovery(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.apps.account.services import llm_safety
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.agent import run_assistant
    for expired in (False, True):
        result = None
        with _automatic_entry_case(expired=expired) as case:
            passive = llm_safety.route_state('assistant')
            assert passive['ready'] is False and passive['model'] == '', \
                'cold/expired passive metadata must remain unresolved before an invocation'
            assert passive['error'] == 'catalogue_unavailable' and passive['can_invoke'] is True, \
                'unavailable data must differ from a completed incompatible catalogue while admitting guarded resolution'
            assert not case['provider'].list_models_page.called, 'passive readiness must make no provider call'
            try:
                result = run_assistant(opts.user, 'Resolve the automatic model on entry')
                assert 'error' not in result, \
                    f'actual normal entry must reach guarded automatic resolution with expired={expired}: {result}'
                assert case['provider'].list_models_page.call_count == case['provider'].call.call_count == 1, \
                    'normal entry must refresh exactly once then invoke the resolved provider model'
                row = LLMRequest.objects.get(credential_fingerprint=case['fingerprint'], feature='assistant')
                assert row.model == case['provider'].call.call_args.kwargs['model'] == case['model'], \
                    'actual entry must bind the newly resolved model to provider and message ledger'
            finally:
                if result and result.get('conversation_id'):
                    Conversation.objects.filter(pk=result['conversation_id'], user=opts.user).delete()


@th.django_unit_test()
@th.requires_app('mojo.apps.assistant')
def test_cold_and_expired_automatic_stream_entry_reaches_guarded_discovery(opts):
    from mojo.apps.account.models import LLMRequest
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant.services.agent import run_assistant_ws
    for expired in (False, True):
        conv = Conversation.objects.create(user=opts.user, title='actual cold automatic stream')
        Message.objects.create(conversation=conv, role='user', content='Resolve on stream entry')
        try:
            with _automatic_entry_case(expired=expired) as case:
                result = run_assistant_ws(opts.user, 'Resolve on stream entry', conv.pk)
                assert 'error' not in result, \
                    f'actual stream entry must reach guarded automatic resolution with expired={expired}: {result}'
                assert case['provider'].list_models_page.call_count == case['provider'].call.call_count == 1, \
                    'stream entry must refresh once and make one resolved message request'
                row = LLMRequest.objects.get(credential_fingerprint=case['fingerprint'], feature='assistant')
                assert row.model == case['provider'].call.call_args.kwargs['model'] == case['model'], \
                    'stream invocation must use the same immutable resolved model in ledger and provider'
        finally:
            conv.delete()


@th.django_unit_test()
@th.requires_app('mojo.apps.assistant')
def test_bootstrap_admits_automatic_cold_route_without_passive_provider_work(opts):
    from mojo.apps.account.services import assistant_setup, llm_safety
    for expired in (False, True):
        with _automatic_entry_case(expired=expired) as case:
            assert llm_safety.route_state('assistant')['ready'] is False, \
                'the passive route must truthfully lack a currently resolved automatic model'
            assert assistant_setup.is_ready() is True, \
                'bootstrap must let a valid automatic route enter the guard to populate its cold/expired cache'
            assert not case['provider'].list_models_page.called and not case['provider'].call.called, \
                'bootstrap admission must remain passive and never discover models or call the provider'


@th.django_unit_test()
@th.requires_app('mojo.apps.assistant')
def test_ws_handler_admits_automatic_cold_route_to_background_guard(opts):
    from mojo.apps.assistant.handler import handle_assistant_message
    from mojo.apps.assistant.models import Conversation
    for expired in (False, True):
        result = None
        try:
            with _automatic_entry_case(expired=expired) as case, \
                    mock.patch('threading.Thread') as thread:
                result = handle_assistant_message(opts.user, dict(
                    type='assistant_message', message='Resolve on socket entry', _bearer='bearer'))
                assert result['type'] == 'assistant_thinking' and thread.return_value.start.call_count == 1, \
                    f'WS handler must admit the background guarded turn for cold/expired automatic state: {result}'
                assert not case['provider'].list_models_page.called, \
                    'the socket preflight must remain passive; only the background guard may refresh'
        finally:
            if result and result.get('conversation_id'):
                Conversation.objects.filter(pk=result['conversation_id'], user=opts.user).delete()


@th.django_unit_test()
@th.requires_app('mojo.apps.assistant')
def test_automatic_invocation_admission_remains_passive_and_control_guarded(opts):
    from mojo.apps.account.services import assistant_setup, llm_safety
    with _automatic_entry_case() as case:
        for boundary, code in (('_policy_agreement', 'policy_mixed'),
                               ('_credential', 'credential_missing'),
                               ('emergency_stopped', 'control_state_unknown')):
            with mock.patch.object(llm_safety, boundary, side_effect=llm_safety.LLMSafetyError(code)):
                route = llm_safety.route_state('assistant')
                assert route['can_invoke'] is False and route['ready'] is False, \
                    f'unknown/denied {boundary} must block both admission and resolved readiness'
                assert assistant_setup.is_ready() is False, \
                    f'bootstrap admission cannot bypass {boundary}'
        with mock.patch.object(llm_safety, 'emergency_stopped', return_value=True):
            assert llm_safety.route_state('assistant')['can_invoke'] is False \
                and assistant_setup.is_ready() is False, \
                'an active emergency stop must still deny cold-route admission'
        assert not case['provider'].list_models_page.called and not case['provider'].call.called, \
            'passive admission checks and denials must never perform provider work'
