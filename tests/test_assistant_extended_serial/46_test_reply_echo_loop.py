"""
Both Assistant tool loops send the model's reply back cleaned (maestro item
#10589): the second model call must not carry the first reply's
whitespace-only text block or its ``None`` SDK fields, which Anthropic
refuses. Serial because it patches the shared settings singleton and
``llm.call``.
"""
import copy
from contextlib import contextmanager
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = "reply-echo-loop-admin@example.com"
CONV_TITLE = "a10589_reply_echo_loop"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(title=CONV_TITLE).delete()
    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password="TestPass1!")
    opts.user.is_email_verified = True
    opts.user.save()
    for perm in ["view_admin", "assistant"]:
        opts.user.add_permission(perm)


def _turns():
    return [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": " \n", "citations": None},
                {"type": "tool_use", "id": "toolu_01X", "name": "read_memory",
                 "input": {}, "caller": None},
            ],
        },
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]},
    ]


@contextmanager
def _fake_model():
    """Enable the Assistant and record a copy of each call's messages."""
    from mojo.helpers import llm
    from mojo.helpers.settings import settings

    orig_get = settings.get
    calls = []
    turns = iter(_turns())

    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return orig_get(name, *args, **kwargs)

    def fake_call(messages, *args, **kwargs):
        calls.append(copy.deepcopy(messages))
        return next(turns)

    with mock.patch.object(settings, "get", side_effect=patched_get), \
            mock.patch.object(llm, "get_api_key", return_value="test-key"), \
            mock.patch(
                "mojo.apps.account.services.llm_safety.route_state",
                return_value={"ready": True, "error": ""}), \
            mock.patch("mojo.helpers.llm.call", side_effect=fake_call):
        yield calls


def _assert_second_call_clean(calls):
    assert_eq(len(calls), 2, f"expected two model calls, got {len(calls)}")
    sent_back = [m for m in calls[1] if m["role"] == "assistant"][-1]
    assert_eq(
        sent_back["content"],
        [{"type": "tool_use", "id": "toolu_01X", "name": "read_memory", "input": {}}],
        "second call must carry the tool call without whitespace text or None fields")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_http_loop_sends_clean_reply(opts):
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.agent import run_assistant

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    with _fake_model() as calls:
        result = run_assistant(opts.user, "hi")
    assert_true("error" not in result, f"run should succeed, got: {result}")
    _assert_second_call_clean(calls)
    Conversation.objects.filter(pk=result.get("conversation_id")).delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_ws_loop_sends_clean_reply(opts):
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant.services.agent import run_assistant_ws

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    conv = Conversation.objects.create(user=opts.user, title=CONV_TITLE)
    Message.objects.create(conversation=conv, role="user", content="hi")
    with _fake_model() as calls:
        result = run_assistant_ws(opts.user, "hi", conv.pk, on_event=lambda *a, **k: None)
    assert_true("error" not in result, f"WS run should succeed, got: {result}")
    _assert_second_call_clean(calls)
    conv.delete()
