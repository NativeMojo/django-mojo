"""
Tests for Anthropic prompt caching integration.

Covers:
- llm.call() injects cache_control when LLM_ADMIN_PROMPT_CACHE_ENABLED is True
- llm.call() returns the usage dict from response.model_dump()
- _accumulate_usage sums per-turn counters correctly
- Message.usage is in the default REST graph and is nullable

The tests that mock.patch the shared settings singleton (cache_control off,
usage persistence, per-turn usage logging) moved to
tests/test_assistant_extended_serial/33_test_prompt_caching.py (maestro item
#1839).

The provider-format tests exercise the extracted Anthropic adapter directly;
the public llm.call boundary has no client seam that could bypass its guard.
"""
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
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider

    fake_client, fake_messages = _make_fake_client(
        _canned_response(usage={
            "input_tokens": 100, "output_tokens": 20,
            "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 0,
        }),
    )

    AnthropicProvider(client=fake_client).call(
        messages=[{"role": "user", "content": "hi"}],
        system="sys",
        model="claude-sonnet-4-test",
        max_tokens=64,
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


# test_llm_helper_omits_cache_control_when_disabled moved to
# tests/test_assistant_extended_serial/33_test_prompt_caching.py — it
# mock.patches the shared settings singleton (maestro item #1839).


@th.django_unit_test()
def test_llm_helper_returns_usage(opts):
    """call() result should include a usage dict surfaced from response.model_dump()."""
    from mojo.helpers.llm_providers.anthropic import AnthropicProvider

    expected_usage = {
        "input_tokens": 42, "output_tokens": 7,
        "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 200,
    }
    fake_client, _ = _make_fake_client(_canned_response(usage=expected_usage))

    result = AnthropicProvider(client=fake_client).call(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-test",
        max_tokens=64,
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

# test_assistant_persists_usage_on_final_message and
# test_assistant_logs_per_turn_cache_usage moved to
# tests/test_assistant_extended_serial/33_test_prompt_caching.py — they
# mock.patch the shared settings singleton (maestro item #1839).

# test_zero_usage_warning_fires_once moved to the same file (maestro item
# #2558): it asserts a once-PER-PROCESS warning, so it must reset the
# module-global llm._zero_cache_warned guard and attach a handler to the
# shared llm logger. The process-globalness IS the assertion, so there is
# nothing to convert to a seam. `_ListHandler` moved with it.


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
