"""
An Assistant run whose last model turn has no text still answers (maestro
item #11025).

The model can write its answer next to a tool call, then end the run with no
text. The HTTP reply then falls back to that step's text, flagged
``response_repeats_step``; the stored final Message stays empty so history
holds the answer once. Over WS the step text was already sent live, so
``response`` stays as stored and only the flags are added. A run with no text
anywhere gets a fixed message per stop reason. Serial because it patches the
shared settings singleton and ``llm.call``.
"""
from contextlib import contextmanager
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = "empty-final-reply-admin@example.com"
CONV_TITLE = "a11025_empty_final_reply"
ANSWER = "The answer is 42."


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


def _step_then(final):
    """The answer next to a read_memory call, then the given final turn."""
    return [
        {"stop_reason": "tool_use", "content": [
            {"type": "text", "text": ANSWER},
            {"type": "tool_use", "id": "toolu_01E", "name": "read_memory", "input": {}},
        ]},
        final,
    ]


@contextmanager
def _fake_model(turns):
    from mojo.helpers import llm
    from mojo.helpers.settings import settings

    orig_get = settings.get
    replies = iter(turns)

    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return orig_get(name, *args, **kwargs)

    with mock.patch.object(settings, "get", side_effect=patched_get), \
            mock.patch.object(llm, "get_api_key", return_value="test-key"), \
            mock.patch(
                "mojo.apps.account.services.llm_safety.route_state",
                return_value={"ready": True, "error": ""}), \
            mock.patch("mojo.helpers.llm.call", side_effect=lambda *a, **k: next(replies)):
        yield


def _post_assistant(user, message):
    """POST /api/assistant in-process, through the real view and service."""
    import json
    from django.test import Client
    from mojo.apps.account.utils.jwtoken import JWToken

    token = JWToken(user.get_auth_key()).create(uid=user.pk).access_token
    response = Client(HTTP_HOST="127.0.0.1").post(
        "/api/assistant", data=json.dumps({"message": message}),
        content_type="application/json", HTTP_AUTHORIZATION="Bearer " + token)
    return response.status_code, response.json()


def _assistant_messages(conversation_id):
    from mojo.apps.assistant.models import Message
    return list(Message.objects.filter(
        conversation_id=conversation_id, role="assistant").order_by("created", "pk"))


@th.django_unit_test("regression #11025: service result empty when the last turn has no text")
@th.requires_app("mojo.apps.assistant")
def test_service_result_repeats_step_text(opts):
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.agent import run_assistant

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    with _fake_model(_step_then({"stop_reason": "end_turn", "content": []})):
        result = run_assistant(opts.user, "what is the answer?")
    assert_true("error" not in result, f"run should succeed, got: {result}")
    assert_eq(result["response"], ANSWER,
              "an empty last turn must return the answer the run wrote one step earlier")
    assert_eq(result.get("response_repeats_step"), True, "the reply must be flagged as repeating a step")
    assert_eq(result.get("stop_reason"), "end_turn", "the result must carry the stop reason")

    stored = _assistant_messages(result["conversation_id"])
    holding = [m for m in stored if m.content == ANSWER]
    assert_eq(len(holding), 1, f"history must hold the answer once, got {[m.content for m in stored]}")
    assert_eq(result.get("step_message_id"), holding[0].pk, "step_message_id must name the step's Message")
    assert_eq(stored[-1].content, "", "the stored final Message must stay empty")
    Conversation.objects.filter(pk=result["conversation_id"]).delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_ws_reply_keeps_response_and_adds_flags(opts):
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant.services.agent import run_assistant_ws

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    conv = Conversation.objects.create(user=opts.user, title=CONV_TITLE)
    Message.objects.create(conversation=conv, role="user", content="what is the answer?")
    events = []
    with _fake_model(_step_then({"stop_reason": "end_turn", "content": []})):
        result = run_assistant_ws(
            opts.user, "what is the answer?", conv.pk,
            on_event=lambda kind, data: events.append((kind, data)))
    assert_true("error" not in result, f"run should succeed, got: {result}")
    assert_eq([d["text"] for k, d in events if k == "text"], [ANSWER],
              "the step's answer is sent live as a text event")
    assert_eq(result["response"], "", "over WS response stays as stored, so the panel does not show it twice")
    assert_eq(result.get("response_repeats_step"), True, "the WS result must carry the repeat flag")
    stored = _assistant_messages(conv.pk)
    assert_eq(result.get("step_message_id"), stored[0].pk, "step_message_id must name the step's Message")
    assert_eq(result.get("stop_reason"), "end_turn", "the result must carry the stop reason")
    conv.delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_max_tokens_with_no_text_gets_fixed_message(opts):
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.agent import run_assistant, _EMPTY_REPLY_MESSAGES

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    with _fake_model([{"stop_reason": "max_tokens", "content": []}]):
        result = run_assistant(opts.user, "a long question")
    fixed = _EMPTY_REPLY_MESSAGES["max_tokens"]
    assert_eq(result["response"], fixed, "a run with no text anywhere returns the fixed max_tokens message")
    assert_true("response_repeats_step" not in result, "no step to repeat, so no flag")
    stored = _assistant_messages(result["conversation_id"])
    assert_eq([m.content for m in stored], [fixed], "the fixed message is stored once")
    Conversation.objects.filter(pk=result["conversation_id"]).delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_final_turn_with_text_is_unchanged(opts):
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.agent import run_assistant

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Final words."}]}
    with _fake_model(_step_then(final)):
        result = run_assistant(opts.user, "what is the answer?")
    assert_eq(result["response"], "Final words.", "a final turn with text is the reply")
    assert_true("response_repeats_step" not in result, "a reply with its own text carries no flag")
    stored = _assistant_messages(result["conversation_id"])
    assert_eq([m.content for m in stored], [ANSWER, "Final words."], "history is the step then the reply")
    Conversation.objects.filter(pk=result["conversation_id"]).delete()


@th.django_unit_test("regression #11025: POST /api/assistant drops the repeat fields")
@th.requires_app("mojo.apps.assistant")
def test_endpoint_reply_repeats_step_text(opts):
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user).delete()
    with _fake_model(_step_then({"stop_reason": "end_turn", "content": []})):
        status, payload = _post_assistant(opts.user, "what is the answer?")
    assert_eq(status, 200, f"the endpoint should answer, got {status}: {payload}")
    data = payload["data"]
    assert_eq(data["response"], ANSWER, "the endpoint returns the step's answer")
    assert_eq(data.get("response_repeats_step"), True, f"the JSON must carry the repeat flag, got {data}")
    stored = _assistant_messages(data["conversation_id"])
    assert_eq(data.get("step_message_id"), stored[0].pk, "step_message_id must name the step's Message")
    assert_eq(data.get("stop_reason"), "end_turn", f"the JSON must carry stop_reason, got {data}")
    assert_eq([m.content for m in stored], [ANSWER, ""], "history holds the answer once; the final Message is empty")
    Conversation.objects.filter(pk=data["conversation_id"]).delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_endpoint_final_text_has_no_flag(opts):
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.filter(user=opts.user).delete()
    final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Final words."}]}
    with _fake_model(_step_then(final)):
        status, payload = _post_assistant(opts.user, "what is the answer?")
    assert_eq(status, 200, f"the endpoint should answer, got {status}: {payload}")
    data = payload["data"]
    assert_eq(data["response"], "Final words.", "a final turn with text is the reply")
    assert_eq(data.get("stop_reason"), "end_turn", f"the JSON must carry stop_reason, got {data}")
    assert_true("response_repeats_step" not in data and "step_message_id" not in data,
                f"a reply with its own text carries no repeat fields, got {data}")
    Conversation.objects.filter(pk=data["conversation_id"]).delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_ws_handler_event_carries_flags(opts):
    from mojo.apps.assistant import handler
    from mojo.apps.assistant.models import Conversation, Message

    Conversation.objects.filter(user=opts.user, title=CONV_TITLE).delete()
    conv = Conversation.objects.create(user=opts.user, title=CONV_TITLE)
    Message.objects.create(conversation=conv, role="user", content="what is the answer?")
    request_id = "00000000-0000-0000-0000-000000011025"
    events = []
    with _fake_model(_step_then({"stop_reason": "end_turn", "content": []})), \
            mock.patch("mojo.apps.realtime.manager.send_event_to_user",
                       side_effect=lambda topic, uid, event: events.append(event)):
        handler._run_agent_thread(opts.user.pk, conv.pk, "what is the answer?", request_id=request_id)
    terminal = [e for e in events if e.get("type") == "assistant_response"]
    assert_eq(len(terminal), 1, f"exactly one assistant_response event, got {[e.get('type') for e in events]}")
    event = terminal[0]
    stored = _assistant_messages(conv.pk)
    assert_eq(event["response"], "", "over WS response stays as stored")
    assert_eq(event.get("response_repeats_step"), True, f"the event must carry the repeat flag, got {event}")
    assert_eq(event.get("step_message_id"), stored[0].pk, "step_message_id must name the step's Message")
    assert_eq(event.get("stop_reason"), "end_turn", f"the event must carry stop_reason, got {event}")
    assert_eq(event.get("request_id"), request_id, "the event echoes the request id")
    conv.delete()
