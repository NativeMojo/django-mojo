"""
Tests for request duration tracking on assistant messages.

Verifies that duration_ms is correctly stored on final assistant
messages and returned in the response dict.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


def _get_or_create_test_user():
    """Get or create a test user for duration tests."""
    from mojo.apps.account.models import User
    user, _ = User.objects.get_or_create(
        email="duration_test@example.com",
        defaults={"username": "duration_test"},
    )
    return user


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_duration_ms_field_nullable(opts):
    """Message.duration_ms should default to None."""
    from mojo.apps.assistant.models import Conversation, Message

    user = _get_or_create_test_user()
    conv = Conversation.objects.create(user=user, title="duration test")
    msg = Message.objects.create(
        conversation=conv, role="user", content="hello",
    )
    assert_true(msg.duration_ms is None,
                f"duration_ms should default to None, got {msg.duration_ms}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_duration_ms_stores_value(opts):
    """Message.duration_ms should accept an integer value."""
    from mojo.apps.assistant.models import Conversation, Message

    user = _get_or_create_test_user()
    conv = Conversation.objects.create(user=user, title="duration store test")
    msg = Message.objects.create(
        conversation=conv, role="assistant", content="response",
        duration_ms=1234,
    )
    msg.refresh_from_db()
    assert_eq(msg.duration_ms, 1234,
              f"duration_ms should be 1234, got {msg.duration_ms}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_duration_ms_in_graph(opts):
    """duration_ms should appear in the default REST graph."""
    from mojo.apps.assistant.models import Message

    fields = Message.RestMeta.GRAPHS["default"]["fields"]
    assert_true("duration_ms" in fields,
                f"duration_ms should be in Message default graph, got {fields}")
