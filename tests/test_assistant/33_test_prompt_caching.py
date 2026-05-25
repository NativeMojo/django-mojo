"""
Tests for Anthropic prompt caching integration.

Covers:
- llm.call() injects cache_control when LLM_ADMIN_PROMPT_CACHE_ENABLED is True
- llm.call() omits cache_control when the setting is False
- llm.call() returns the usage dict from response.model_dump()
- _accumulate_usage sums per-turn counters correctly
- run_assistant() persists summed usage on the final Message
- per-turn cache usage is logged to assistant.log
- Message.usage is in the default REST graph and is nullable
- A zero-cache-usage first call logs a one-time warning
"""
import logging
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = 'cache-test-admin@example.com'
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


# ---------------------------------------------------------------------------
# llm.call() cache_control injection
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_llm_helper_sets_cache_control_when_enabled(opts):
    """call() should add cache_control={'type':'ephemeral'} when the setting is True."""
    from mojo.helpers import llm

    fake_client, fake_messages = _make_fake_client(
        _canned_response(usage={
            "input_tokens": 100, "output_tokens": 20,
            "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 0,
        }),
    )

    with mock.patch("anthropic.Anthropic", return_value=fake_client):
        with mock.patch.object(llm, "get_api_key", return_value="sk-test"):
            llm.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="claude-sonnet-4-test",
            )

    sent = fake_messages.last_kwargs
    assert_true(sent is not None, "messages.create should have been called")
    assert_true(
        "cache_control" in sent,
        f"cache_control should be in kwargs when enabled, got {list(sent.keys())}",
    )
    assert_eq(
        sent["cache_control"], {"type": "ephemeral"},
        f"cache_control should be ephemeral, got {sent['cache_control']!r}",
    )


@th.django_unit_test()
def test_llm_helper_omits_cache_control_when_disabled(opts):
    """call() should NOT add cache_control when the setting is False."""
    from mojo.helpers import llm
    from mojo.helpers.settings import settings as settings_obj

    fake_client, fake_messages = _make_fake_client(_canned_response())

    real_get = settings_obj.get
    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_PROMPT_CACHE_ENABLED":
            return False
        return real_get(name, *args, **kwargs)

    with mock.patch.object(settings_obj, "get", side_effect=patched_get):
        with mock.patch("anthropic.Anthropic", return_value=fake_client):
            with mock.patch.object(llm, "get_api_key", return_value="sk-test"):
                llm.call(
                    messages=[{"role": "user", "content": "hi"}],
                    model="claude-sonnet-4-test",
                )

    sent = fake_messages.last_kwargs
    assert_true(sent is not None, "messages.create should have been called")
    assert_true(
        "cache_control" not in sent,
        f"cache_control should be absent when disabled, got {list(sent.keys())}",
    )


@th.django_unit_test()
def test_llm_helper_returns_usage(opts):
    """call() result should include a usage dict surfaced from response.model_dump()."""
    from mojo.helpers import llm

    expected_usage = {
        "input_tokens": 42, "output_tokens": 7,
        "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 200,
    }
    fake_client, _ = _make_fake_client(_canned_response(usage=expected_usage))

    with mock.patch("anthropic.Anthropic", return_value=fake_client):
        with mock.patch.object(llm, "get_api_key", return_value="sk-test"):
            result = llm.call(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-sonnet-4-test",
            )

    assert_true("usage" in result, f"result should include usage, got keys {list(result.keys())}")
    assert_eq(
        result["usage"], expected_usage,
        f"usage should round-trip from response.model_dump(), got {result['usage']!r}",
    )


# ---------------------------------------------------------------------------
# _accumulate_usage helper
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_accumulate_usage_sums_all_counters(opts):
    """_accumulate_usage should sum every known counter across calls."""
    from mojo.apps.assistant.services.agent import _accumulate_usage

    totals = {}
    _accumulate_usage(totals, {
        "input_tokens": 10, "output_tokens": 5,
        "cache_creation_input_tokens": 100, "cache_read_input_tokens": 200,
    })
    _accumulate_usage(totals, {
        "input_tokens": 3, "output_tokens": 2,
        "cache_creation_input_tokens": 50, "cache_read_input_tokens": 400,
    })

    assert_eq(totals["input_tokens"], 13, f"input_tokens should sum to 13, got {totals['input_tokens']}")
    assert_eq(totals["output_tokens"], 7, f"output_tokens should sum to 7, got {totals['output_tokens']}")
    assert_eq(
        totals["cache_creation_input_tokens"], 150,
        f"cache_creation_input_tokens should sum to 150, got {totals['cache_creation_input_tokens']}",
    )
    assert_eq(
        totals["cache_read_input_tokens"], 600,
        f"cache_read_input_tokens should sum to 600, got {totals['cache_read_input_tokens']}",
    )


@th.django_unit_test()
def test_accumulate_usage_handles_missing_fields(opts):
    """_accumulate_usage should treat missing/None keys as 0 and never raise."""
    from mojo.apps.assistant.services.agent import _accumulate_usage

    totals = {}
    _accumulate_usage(totals, {})  # empty dict
    _accumulate_usage(totals, None)  # None
    _accumulate_usage(totals, {"input_tokens": 5, "cache_read_input_tokens": None})  # None value

    assert_eq(totals.get("input_tokens", 0), 5, f"input_tokens should be 5, got {totals.get('input_tokens')}")
    assert_eq(
        totals.get("cache_read_input_tokens", 0), 0,
        f"None should treat as 0, got {totals.get('cache_read_input_tokens')}",
    )
    assert_eq(
        totals.get("output_tokens", 0), 0,
        f"missing key should treat as 0, got {totals.get('output_tokens')}",
    )


# ---------------------------------------------------------------------------
# Agent loop usage persistence + logging
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_assistant_persists_usage_on_final_message(opts):
    """run_assistant() should sum usage across turns and store on the final Message."""
    from mojo.apps.assistant.services import agent
    from mojo.apps.assistant.models import Message
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

    with mock.patch.object(settings_obj, "get", side_effect=patched_get):
        with mock.patch.object(agent.llm, "call", return_value=turn_1) as mock_call:
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
        with mock.patch.object(settings_obj, "get", side_effect=patched_get):
            with mock.patch.object(agent.llm, "call", return_value=turn):
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


class _ListHandler(logging.Handler):
    """Simple log handler that captures records into a list."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


# ---------------------------------------------------------------------------
# Message model exposure
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_message_usage_in_default_graph(opts):
    """The 'usage' field should be in the default REST graph for Message."""
    from mojo.apps.assistant.models import Message
    fields = Message.RestMeta.GRAPHS["default"]["fields"]
    assert_true(
        "usage" in fields,
        f"'usage' should be in Message default graph, got {fields}",
    )


@th.django_unit_test()
def test_message_usage_field_nullable(opts):
    """Message.usage should default to None and round-trip JSON dicts."""
    from mojo.apps.assistant.models import Conversation, Message

    conv = Conversation.objects.create(user=opts.user, title="usage test")
    msg_null = Message.objects.create(conversation=conv, role="user", content="hello")
    assert_true(msg_null.usage is None, f"usage should default to None, got {msg_null.usage!r}")

    payload = {
        "input_tokens": 1, "output_tokens": 2,
        "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
    }
    msg = Message.objects.create(
        conversation=conv, role="assistant", content="r", usage=payload,
    )
    msg.refresh_from_db()
    assert_eq(msg.usage, payload, f"usage should round-trip JSON, got {msg.usage!r}")


# ---------------------------------------------------------------------------
# Zero-cache warning
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_zero_usage_warning_fires_once(opts):
    """When caching is enabled but both counters are 0, WARN once per process."""
    from mojo.helpers import llm

    # Reset the process-level guard so this test is deterministic.
    llm._zero_cache_warned = False

    fake_client, _ = _make_fake_client(_canned_response(usage={
        "input_tokens": 5, "output_tokens": 3,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }))

    stdlib_logger = llm.logger.logger
    handler = _ListHandler()
    stdlib_logger.addHandler(handler)
    prev_level = stdlib_logger.level
    stdlib_logger.setLevel(logging.WARNING)

    try:
        with mock.patch("anthropic.Anthropic", return_value=fake_client):
            with mock.patch.object(llm, "get_api_key", return_value="sk-test"):
                # Two calls — both return zero cache counters
                llm.call(messages=[{"role": "user", "content": "hi"}], model="m")
                llm.call(messages=[{"role": "user", "content": "hi2"}], model="m")
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
