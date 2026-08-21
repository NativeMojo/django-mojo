"""Per-turn WS correlation test moved from
tests/test_assistant/35_test_ws_request_correlation.py — it mock.patches the
shared settings singleton (mojo.helpers.settings.settings.get) plus other
process-wide surfaces (threading.Thread, realtime manager) around an
in-process handler call, which is unsafe under the parallel default tier
(maestro item #1839). Runs opt-in (`extended`) and serial.
"""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


TEST_EMAIL = "assistant-correlation-serial@example.com"
TEST_PASSWORD = "TestPass1!"
REQUEST_ID = "5dbeb7d4-44a7-4e26-b6bd-708f146b42d9"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_correlation_serial(opts):
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

    def run_assistant(user, message, conversation_id, on_event,
                      request_meta=None):
        # request_meta is the socket's own context, built by the handler from
        # the consumer's server-stamped `_bearer` (item #2570). The stub takes
        # it so this test keeps asserting request_id correlation rather than
        # the signature.
        assert_eq(request_meta.bearer, None,
                  f"the handler must pass the socket's stamped bearer through, "
                  f"got {request_meta!r}")
        assert_eq(request_meta.key_backed, True,
                  "a message with no `_bearer` stamp must read as key-backed")
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
