"""
The Assistant never replays thinking blocks from stored history, and drops
them from the live conversation when the tool list changes (maestro item
#10589, Part C).

Anthropic signs a thinking block against the system prompt, the tools and
every earlier message. History is rebuilt from stored rows under a tool list
that may differ, so a replayed block can be refused as ``provider_rejected``.
Text and tool calls are kept, in order.
"""
from testit import helpers as th
from testit.helpers import assert_eq


TEST_EMAIL = "thinking-replay-admin@example.com"
CONV_TITLE = "a10589_thinking_replay"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(title=CONV_TITLE).delete()
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password="TestPass1!")


def _tool_use():
    return {"type": "tool_use", "id": "toolu_01T", "name": "load_tools",
            "input": {"domain": "jobs"}}


@th.tier("bug")
@th.django_unit_test("regression #10589: stored thinking replayed under a new tool list")
@th.requires_app("mojo.apps.assistant")
def test_history_replays_no_thinking(opts):
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant.services.agent import _build_conversation_messages

    Conversation.objects.filter(title=CONV_TITLE).delete()
    conv = Conversation.objects.create(user=opts.user, title=CONV_TITLE)
    Message.objects.create(conversation=conv, role="user", content="hello")
    Message.objects.create(
        conversation=conv, role="assistant", content="",
        tool_calls=[
            {"type": "thinking", "thinking": "t", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
            _tool_use(),
        ])
    Message.objects.create(
        conversation=conv, role="tool_result", content="",
        tool_calls=[{"type": "tool_result", "tool_use_id": "toolu_01T", "content": "{}"}])

    messages = _build_conversation_messages(conv, 20)
    assert_eq([m["role"] for m in messages], ["user", "assistant", "user"],
              "history keeps the turn order")
    assert_eq(messages[1]["content"], [_tool_use()],
              "stored thinking and redacted_thinking must not be replayed; tool_use stays")
    conv.delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_strip_thinking_keeps_text_and_tool_use_in_order(opts):
    from mojo.apps.assistant.services.agent import _strip_thinking

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "t", "signature": "sig"},
            {"type": "text", "text": "Loading."},
            {"type": "redacted_thinking", "data": "opaque"},
            _tool_use(),
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_01T", "content": "{}"}]},
        {"role": "assistant", "content": "plain text reply"},
    ]
    _strip_thinking(messages)
    assert_eq(messages[1]["content"], [{"type": "text", "text": "Loading."}, _tool_use()],
              "thinking blocks are removed; text and tool_use keep their order")
    assert_eq(messages[2]["content"][0]["type"], "tool_result",
              "user turns are untouched")
    assert_eq(messages[3]["content"], "plain text reply",
              "string assistant content is untouched")
