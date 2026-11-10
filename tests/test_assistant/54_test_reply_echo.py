"""
The Assistant sends the model's reply back in a form Anthropic accepts
(maestro item #10589).

Anthropic refuses a request holding an empty or whitespace-only text block,
and the SDK's ``model_dump`` adds ``None`` fields (``citations``,
``caller``) that are not part of the request format. The reply content sent
back on the next tool-loop turn, and stored tool calls replayed from
history, drop both. Tool calls and non-empty text pass unchanged.
"""
from testit import helpers as th
from testit.helpers import assert_eq


TEST_EMAIL = "reply-echo-admin@example.com"
CONV_TITLE = "a10589_reply_echo"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(title=CONV_TITLE).delete()
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password="TestPass1!")


def _dumped_reply():
    """A tool_use reply as SDK 0.96 model_dump returns it, with a whitespace text block."""
    return [
        {"type": "text", "text": "\n\n", "citations": None},
        {"type": "tool_use", "id": "toolu_01X", "name": "load_tools",
         "input": {"domain": "docit"}, "caller": None},
    ]


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_whitespace_text_and_none_fields_dropped(opts):
    from mojo.apps.assistant.services.agent import _reply_content_for_request

    assert_eq(
        _reply_content_for_request(_dumped_reply()),
        [{"type": "tool_use", "id": "toolu_01X", "name": "load_tools",
          "input": {"domain": "docit"}}],
        "whitespace text block and None fields must be dropped")

    empty = [{"type": "text", "text": ""}, {"type": "text", "text": None}]
    assert_eq(_reply_content_for_request(empty), [],
              "empty and missing text blocks must be dropped")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_wellformed_reply_unchanged(opts):
    from mojo.apps.assistant.services.agent import _reply_content_for_request

    reply = [
        {"type": "text", "text": "  Loading the docs tools. "},
        {"type": "tool_use", "id": "toolu_01Y", "name": "search_docs",
         "input": {"query": "x"}, "caller": {"type": "direct"}},
        {"type": "thinking", "thinking": "t", "signature": "sig"},
    ]
    assert_eq(_reply_content_for_request(reply), reply,
              "non-empty text, set fields and other block types pass unchanged")
    assert_eq(_reply_content_for_request(None), [], "None content gives an empty list")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_history_replays_clean_tool_calls(opts):
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant.services.agent import _build_conversation_messages

    Conversation.objects.filter(title=CONV_TITLE).delete()
    conv = Conversation.objects.create(user=opts.user, title=CONV_TITLE)
    Message.objects.create(conversation=conv, role="user", content="hello")
    Message.objects.create(
        conversation=conv, role="assistant", content="", tool_calls=_dumped_reply())
    Message.objects.create(
        conversation=conv, role="tool_result", content="",
        tool_calls=[{"type": "tool_result", "tool_use_id": "toolu_01X", "content": "{}"}])

    messages = _build_conversation_messages(conv, 20)
    assert_eq([m["role"] for m in messages], ["user", "assistant", "user"],
              "history keeps the turn order")
    assert_eq(
        messages[1]["content"],
        [{"type": "tool_use", "id": "toolu_01X", "name": "load_tools",
          "input": {"domain": "docit"}}],
        "stored tool calls replay without whitespace text or None fields")
    conv.delete()
