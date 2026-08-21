"""
Prompt-caching tests moved from tests/test_assistant/33_test_prompt_caching.py
— they mock.patch the shared settings singleton
(mojo.helpers.settings.settings.get) around in-process llm/agent calls, which
is unsafe under the parallel default tier (maestro item #1839). Runs opt-in
(`extended`) and serial.

Covers:
- llm.call() omits cache_control when the setting is False
- run_assistant() persists summed usage on the final Message
- per-turn cache usage is logged to assistant.log
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
