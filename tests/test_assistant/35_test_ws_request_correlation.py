"""Tests for per-turn correlation on assistant WebSocket events."""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


TEST_EMAIL = "assistant-correlation@example.com"
TEST_PASSWORD = "TestPass1!"
REQUEST_ID = "5dbeb7d4-44a7-4e26-b6bd-708f146b42d9"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_correlation(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_EMAIL,
        email=TEST_EMAIL,
        password=TEST_PASSWORD,
    )
    opts.admin.add_permission("view_admin")


@th.django_unit_test()
def test_request_id_is_echoed_on_ack_and_streamed_events(opts):
    """A client request_id follows one turn from ack through terminal event."""
    from mojo.apps.assistant.handler import handle_assistant_message
    from mojo.helpers.settings import settings

    sent_events = []
    original_settings_get = settings.get

    def settings_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return original_settings_get(name, *args, **kwargs)

    def run_assistant(user, message, conversation_id, on_event):
        on_event("text", {"text": "Checking now", "blocks": None})
        return {
            "message_id": 321,
            "created": "2026-08-07T12:00:00Z",
            "response": "Done",
        }

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    with mock.patch.object(settings, "get", side_effect=settings_get):
        with mock.patch("mojo.helpers.llm.get_api_key", return_value="sk-test"):
            with mock.patch("threading.Thread", ImmediateThread):
                with mock.patch(
                    "mojo.apps.assistant.services.agent.run_assistant_ws",
                    side_effect=run_assistant,
                ):
                    with mock.patch(
                        "mojo.apps.realtime.manager.send_event_to_user",
                        side_effect=lambda _topic, _user_id, event: sent_events.append(event),
                    ):
                        ack = handle_assistant_message(opts.admin, {
                            "type": "assistant_message",
                            "message": "Check the current status",
                            "request_id": REQUEST_ID,
                        })

    assert_eq(ack["type"], "assistant_thinking", "Expected immediate thinking ack")
    assert_eq(ack.get("request_id"), REQUEST_ID, "Ack must echo request_id")
    assert_eq([event["type"] for event in sent_events], [
        "assistant_text",
        "assistant_response",
    ], "Expected intermediate and terminal streamed events")
    assert_true(sent_events, "Expected streamed assistant events")
    for event in sent_events:
        assert_eq(event.get("request_id"), REQUEST_ID, "Stream event must echo request_id")


@th.django_unit_test()
def test_invalid_request_id_is_rejected_before_processing(opts):
    """Malformed IDs fail closed before a conversation is created."""
    from mojo.apps.assistant.handler import handle_assistant_message
    from mojo.apps.assistant.models import Conversation

    before = Conversation.objects.filter(user=opts.admin).count()
    result = handle_assistant_message(opts.admin, {
        "type": "assistant_message",
        "message": "This must not be stored",
        "request_id": "not-a-uuid",
    })

    assert_eq(result["type"], "assistant_error", "Malformed ID should return an error")
    assert_true("canonical UUID" in result["error"], "Error should explain the request_id contract")
    assert_true("request_id" not in result, "Malformed request_id must not be reflected")
    assert_eq(
        Conversation.objects.filter(user=opts.admin).count(),
        before,
        "Malformed request must not create a conversation",
    )
