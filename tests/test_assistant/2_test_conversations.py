"""
Tests for conversation CRUD — create, list, get, delete, owner-only access.
Uses standard RestMeta CRUD via on_rest_request.
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

    # Other user (no view_admin — relies on "owner" fallback in VIEW_PERMS)
    opts.other = User.objects.create_user(
        username=TEST_EMAIL_OTHER, email=TEST_EMAIL_OTHER, password=TEST_PASSWORD,
    )
    opts.other.is_email_verified = True
    opts.other.save()

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
def test_list_conversations_admin_sees_all(opts):
    """Admin user (view_admin) sees all conversations via list endpoint."""
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.create(user=opts.owner, title="Admin conv list")
    Conversation.objects.create(user=opts.other, title="Other conv list")

    opts.client.login(TEST_EMAIL_OWNER, TEST_PASSWORD)
    resp = opts.client.get('/api/assistant/conversation')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")

    data = resp.json.data
    assert_true(isinstance(data, list), f"Expected list, got {type(data)}")
    titles = [c.title for c in data]
    assert_true("Other conv list" in titles,
                f"Admin should see other user's conversations, got: {titles}")


@th.django_unit_test()
def test_list_conversations_owner_only(opts):
    """Non-admin user only sees their own conversations via owner fallback."""
    from mojo.apps.assistant.models import Conversation

    Conversation.objects.create(user=opts.owner, title="Owner private conv")
    Conversation.objects.create(user=opts.other, title="Other own conv")

    # Login as other (no view_admin — uses "owner" fallback)
    opts.client.login(TEST_EMAIL_OTHER, TEST_PASSWORD)
    resp = opts.client.get('/api/assistant/conversation')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")

    data = resp.json.data
    assert_true(isinstance(data, list), f"Expected list, got {type(data)}")
    for conv in data:
        assert_true(conv.title != "Owner private conv",
                    "Non-admin should NOT see other user's conversations")


@th.django_unit_test()
def test_get_conversation_detail_with_messages(opts):
    """GET /api/assistant/conversation/<pk>?graph=detail includes messages."""
    from mojo.apps.assistant.models import Conversation, Message

    conv = Conversation.objects.create(user=opts.owner, title="Detail test")
    Message.objects.create(conversation=conv, role="user", content="What happened today?")
    Message.objects.create(
        conversation=conv, role="assistant", content="Here's a summary...",
        blocks=[{"type": "stat", "items": [{"label": "Events", "value": 10}]}],
    )

    opts.client.login(TEST_EMAIL_OWNER, TEST_PASSWORD)
    resp = opts.client.get(f'/api/assistant/conversation/{conv.pk}?graph=detail')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")

    data = resp.json.data
    assert_eq(data.id, conv.pk, f"Expected conversation id {conv.pk}, got {data.id}")
    assert_true(data.messages is not None, "Expected messages in detail graph")
    assert_eq(len(data.messages), 2, f"Expected 2 messages, got {len(data.messages)}")
    assert_eq(data.messages[0].role, "user", "First message should be user role")
    assert_eq(data.messages[1].role, "assistant", "Second message should be assistant role")

    # Verify blocks are included on the assistant message
    assistant_msg = data.messages[1]
    assert_true(assistant_msg.blocks is not None,
                f"Expected blocks on assistant message, got None")
    assert_eq(assistant_msg.blocks[0]["type"], "stat",
              f"Expected stat block, got {assistant_msg.blocks[0]}")


@th.django_unit_test()
def test_get_conversation_other_user_denied(opts):
    """GET /api/assistant/conversation/<pk> denies access to other user's conversation."""
    from mojo.apps.assistant.models import Conversation

    # Create conversation owned by owner
    conv = Conversation.objects.create(user=opts.owner, title="Owner only")

    # Login as other user — should get 404 (owner check fails)
    opts.client.login(TEST_EMAIL_OTHER, TEST_PASSWORD)
    resp = opts.client.get(f'/api/assistant/conversation/{conv.pk}')
    assert_true(resp.status_code in [403, 404],
                f"Expected 403/404 for other user's conversation, got {resp.status_code}")


@th.django_unit_test()
def test_delete_conversation_owner(opts):
    """DELETE /api/assistant/conversation/<pk> deletes owner's conversation."""
    from mojo.apps.assistant.models import Conversation

    conv = Conversation.objects.create(user=opts.owner, title="To delete")

    opts.client.login(TEST_EMAIL_OWNER, TEST_PASSWORD)
    resp = opts.client.delete(f'/api/assistant/conversation/{conv.pk}')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")

    exists = Conversation.objects.filter(pk=conv.pk).exists()
    assert_true(not exists, "Conversation should be deleted")


@th.django_unit_test()
def test_delete_conversation_other_user_denied(opts):
    """DELETE /api/assistant/conversation/<pk> denied for non-owner."""
    from mojo.apps.assistant.models import Conversation

    conv = Conversation.objects.create(user=opts.owner, title="Protected")

    opts.client.login(TEST_EMAIL_OTHER, TEST_PASSWORD)
    resp = opts.client.delete(f'/api/assistant/conversation/{conv.pk}')
    assert_true(resp.status_code in [403, 404],
                f"Expected 403/404 for other user's conversation, got {resp.status_code}")

    # Conversation should still exist
    exists = Conversation.objects.filter(pk=conv.pk).exists()
    assert_true(exists, "Conversation should NOT be deleted by non-owner")


@th.django_unit_test()
def test_blocks_stored_at_write_time(opts):
    """Message blocks field stores parsed block data."""
    from mojo.apps.assistant.models import Conversation, Message

    conv = Conversation.objects.create(user=opts.owner, title="Blocks test")
    blocks_data = [{"type": "table", "title": "Test", "columns": ["A"], "rows": [["1"]]}]
    msg = Message.objects.create(
        conversation=conv, role="assistant",
        content="Here are results.", blocks=blocks_data,
    )

    msg.refresh_from_db()
    assert_eq(msg.blocks, blocks_data,
              f"Expected blocks to be stored, got {msg.blocks}")
    assert_eq(msg.content, "Here are results.",
              f"Expected clean content, got {msg.content}")


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
