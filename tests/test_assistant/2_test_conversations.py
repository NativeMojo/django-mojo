"""
Tests for conversation CRUD — create, list, get, delete, owner-only access.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_OWNER = 'assistant-conv-owner@example.com'
TEST_EMAIL_OTHER = 'assistant-conv-other@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_conversations(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Conversation, Message

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_EMAIL_OWNER, TEST_EMAIL_OTHER]).delete()

    # Owner user
    opts.owner = User.objects.create_user(
        username=TEST_EMAIL_OWNER, email=TEST_EMAIL_OWNER, password=TEST_PASSWORD,
    )
    opts.owner.is_email_verified = True
    opts.owner.save()
    opts.owner.add_permission("view_admin")

    # Other user (also has view_admin, but different user)
    opts.other = User.objects.create_user(
        username=TEST_EMAIL_OTHER, email=TEST_EMAIL_OTHER, password=TEST_PASSWORD,
    )
    opts.other.is_email_verified = True
    opts.other.save()
    opts.other.add_permission("view_admin")

    # Clean up any stale conversations
    Conversation.objects.filter(user__in=[opts.owner, opts.other]).delete()


@th.django_unit_test()
def test_create_conversation_via_model(opts):
    """Conversations can be created directly via the model."""
    from mojo.apps.assistant.models import Conversation, Message

    conv = Conversation.objects.create(user=opts.owner, title="Test conv")
    assert_true(conv.pk, "Expected conversation to have a pk")
    opts.conv_id = conv.pk

    # Add messages
    Message.objects.create(conversation=conv, role="user", content="Hello")
    Message.objects.create(conversation=conv, role="assistant", content="Hi there!")

    msgs = Message.objects.filter(conversation=conv).order_by("created")
    assert_eq(msgs.count(), 2, f"Expected 2 messages, got {msgs.count()}")
    assert_eq(msgs[0].role, "user", f"Expected first message role='user', got {msgs[0].role}")
    assert_eq(msgs[1].role, "assistant", f"Expected second message role='assistant', got {msgs[1].role}")


@th.django_unit_test()
def test_list_conversations_owner_only(opts):
    """GET /api/assistant/conversation should only return the requesting user's conversations."""
    from mojo.apps.assistant.models import Conversation

    # Create a conversation for each user
    Conversation.objects.create(user=opts.owner, title="Owner conv")
    Conversation.objects.create(user=opts.other, title="Other conv")

    # Login as owner
    opts.client.login(TEST_EMAIL_OWNER, TEST_PASSWORD)
    resp = opts.client.get('/api/assistant/conversation')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")

    data = resp.json.data
    assert_true(isinstance(data, list), f"Expected list, got {type(data)}")

    # Should only see owner's conversations
    for conv in data:
        assert_true(conv.title != "Other conv",
                    "Owner should NOT see other user's conversations")


@th.django_unit_test()
def test_get_conversation_with_messages(opts):
    """GET /api/assistant/conversation/<pk> should return conversation with message history."""
    from mojo.apps.assistant.models import Conversation, Message

    conv = Conversation.objects.create(user=opts.owner, title="Detail test")
    Message.objects.create(conversation=conv, role="user", content="What happened today?")
    Message.objects.create(conversation=conv, role="assistant", content="Here's a summary...")

    opts.client.login(TEST_EMAIL_OWNER, TEST_PASSWORD)
    resp = opts.client.get(f'/api/assistant/conversation/{conv.pk}')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")

    data = resp.json.data
    assert_eq(data.id, conv.pk, f"Expected conversation id {conv.pk}, got {data.id}")
    assert_eq(len(data.messages), 2, f"Expected 2 messages, got {len(data.messages)}")
    assert_eq(data.messages[0].role, "user", "First message should be user role")
    assert_eq(data.messages[1].role, "assistant", "Second message should be assistant role")


@th.django_unit_test()
def test_get_conversation_other_user_denied(opts):
    """GET /api/assistant/conversation/<pk> should deny access to other user's conversation."""
    from mojo.apps.assistant.models import Conversation

    # Create conversation owned by owner
    conv = Conversation.objects.create(user=opts.owner, title="Owner only")

    # Login as other user
    opts.client.login(TEST_EMAIL_OTHER, TEST_PASSWORD)
    resp = opts.client.get(f'/api/assistant/conversation/{conv.pk}')
    assert_eq(resp.status_code, 404,
              f"Expected 404 for other user's conversation, got {resp.status_code}")


@th.django_unit_test()
def test_delete_conversation_owner(opts):
    """DELETE /api/assistant/conversation/<pk> should delete owner's conversation."""
    from mojo.apps.assistant.models import Conversation

    conv = Conversation.objects.create(user=opts.owner, title="To delete")

    opts.client.login(TEST_EMAIL_OWNER, TEST_PASSWORD)
    resp = opts.client.delete(f'/api/assistant/conversation/{conv.pk}')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")

    exists = Conversation.objects.filter(pk=conv.pk).exists()
    assert_true(not exists, "Conversation should be deleted")


@th.django_unit_test()
def test_delete_conversation_other_user_denied(opts):
    """DELETE /api/assistant/conversation/<pk> should deny deletion by non-owner."""
    from mojo.apps.assistant.models import Conversation

    conv = Conversation.objects.create(user=opts.owner, title="Protected")

    opts.client.login(TEST_EMAIL_OTHER, TEST_PASSWORD)
    resp = opts.client.delete(f'/api/assistant/conversation/{conv.pk}')
    assert_eq(resp.status_code, 404,
              f"Expected 404 for other user's conversation, got {resp.status_code}")

    # Conversation should still exist
    exists = Conversation.objects.filter(pk=conv.pk).exists()
    assert_true(exists, "Conversation should NOT be deleted by non-owner")


@th.django_unit_test()
def test_conversation_messages_stored_in_order(opts):
    """Messages should be stored and retrieved in chronological order."""
    from mojo.apps.assistant.models import Conversation, Message
    import time

    conv = Conversation.objects.create(user=opts.owner, title="Order test")

    roles = ["user", "assistant", "tool_use", "tool_result", "user", "assistant"]
    for role in roles:
        Message.objects.create(conversation=conv, role=role, content=f"msg-{role}")
        time.sleep(0.01)  # tiny delay to ensure distinct created timestamps

    msgs = list(Message.objects.filter(conversation=conv).order_by("created"))
    assert_eq(len(msgs), len(roles), f"Expected {len(roles)} messages, got {len(msgs)}")
    for i, role in enumerate(roles):
        assert_eq(msgs[i].role, role,
                  f"Message {i} expected role '{role}', got '{msgs[i].role}'")
