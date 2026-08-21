"""Tests for per-turn correlation on assistant WebSocket events.

test_request_id_is_echoed_on_ack_and_streamed_events moved to
tests/test_assistant_extended_serial/35_test_ws_request_correlation.py — it
mock.patches the shared settings singleton and other process-wide surfaces
(maestro item #1839).
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


TEST_EMAIL = "assistant-correlation@example.com"
TEST_PASSWORD = "TestPass1!"


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
