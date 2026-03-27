"""
Tests for chat room CRUD, membership, join/leave, and permissions.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_1 = 'chat-test-user1@example.com'
TEST_EMAIL_2 = 'chat-test-user2@example.com'
TEST_EMAIL_3 = 'chat-test-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
def setup_chat_rooms(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom, ChatMembership, ChatMessage

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_EMAIL_1, TEST_EMAIL_2, TEST_EMAIL_3]).delete()
    ChatRoom.objects.filter(name__startswith="test-chat-").delete()

    # Create test users
    opts.user1 = User.objects.create_user(
        username=TEST_EMAIL_1, email=TEST_EMAIL_1, password=TEST_PASSWORD,
    )
    opts.user2 = User.objects.create_user(
        username=TEST_EMAIL_2, email=TEST_EMAIL_2, password=TEST_PASSWORD,
    )
    opts.admin_user = User.objects.create_user(
        username=TEST_EMAIL_3, email=TEST_EMAIL_3, password=TEST_PASSWORD,
    )
    opts.admin_user.add_permission("manage_chat")


@th.django_unit_test()
def test_create_group_room(opts):
    """Create a group room via REST and verify owner membership is auto-created."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room', {
        'name': 'test-chat-group-room',
        'kind': 'group',
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")
    assert_true(resp.json.data.id, "expected room id in response")
    opts.group_room_id = resp.json.data.id

    # Verify owner membership was auto-created
    from mojo.apps.chat.models import ChatMembership
    ms = ChatMembership.objects.filter(room_id=opts.group_room_id, user=opts.user1).first()
    assert_true(ms, "expected owner membership to be auto-created")
    assert_eq(ms.role, "owner", "expected owner role")


@th.django_unit_test()
def test_create_channel_room(opts):
    """Create a channel room."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room', {
        'name': 'test-chat-channel-room',
        'kind': 'channel',
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")
    opts.channel_room_id = resp.json.data.id

    # Verify default rules were set
    from mojo.apps.chat.models import ChatRoom
    room = ChatRoom.objects.get(pk=opts.channel_room_id)
    assert_true(room.rules, "expected default rules to be set")
    assert_eq(room.rules.get("max_message_length"), 4000, "expected default max_message_length")


@th.django_unit_test()
def test_join_channel(opts):
    """User 2 joins the channel room."""
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/join', {
        'room_id': opts.channel_room_id,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")

    # Verify membership
    from mojo.apps.chat.models import ChatMembership
    ms = ChatMembership.objects.filter(
        room_id=opts.channel_room_id, user=opts.user2,
    ).first()
    assert_true(ms, "expected membership after joining channel")
    assert_eq(ms.status, "active", "expected active status")


@th.django_unit_test()
def test_join_group_room_fails(opts):
    """Cannot join a group room (invite-only)."""
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/join', {
        'room_id': opts.group_room_id,
    })
    assert_eq(resp.status_code, 403, f"expected 403 for join on group room, got {resp.status_code}")


@th.django_unit_test()
def test_add_member_requires_admin(opts):
    """Non-admin cannot add members to group room."""
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/member/add', {
        'room_id': opts.group_room_id,
        'user_id': opts.user2.pk,
    })
    assert_eq(resp.status_code, 403, f"expected 403, got {resp.status_code}")


@th.django_unit_test()
def test_add_member_as_owner(opts):
    """Room owner can add members."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/member/add', {
        'room_id': opts.group_room_id,
        'user_id': opts.user2.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")


@th.django_unit_test()
def test_list_rooms(opts):
    """List rooms user is a member of."""
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/rooms')
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    room_ids = [r["id"] for r in resp.json.data]
    assert_true(opts.channel_room_id in room_ids, "expected channel room in list")
    assert_true(opts.group_room_id in room_ids, "expected group room in list")


@th.django_unit_test()
def test_room_members(opts):
    """List members of a room."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.get('/api/chat/room/members', params={
        'room_id': opts.group_room_id,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    assert_true(len(resp.json.data) >= 2, "expected at least 2 members")


@th.django_unit_test()
def test_create_dm_room(opts):
    """Create a DM room between user1 and user2."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/dm', {
        'user_id': opts.user2.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.data.kind, "direct", "expected direct kind")
    opts.dm_room_id = resp.json.data.id


@th.django_unit_test()
def test_dm_room_reuse(opts):
    """Second DM request to same user returns existing room, not a duplicate."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/dm', {
        'user_id': opts.user2.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    assert_eq(resp.json.data.id, opts.dm_room_id, "expected same DM room id")


@th.django_unit_test()
def test_dm_cannot_self(opts):
    """Cannot create a DM with yourself."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/dm', {
        'user_id': opts.user1.pk,
    })
    assert_eq(resp.status_code, 400, f"expected 400, got {resp.status_code}")


@th.django_unit_test()
def test_leave_channel(opts):
    """Leave a channel room."""
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/leave', {
        'room_id': opts.channel_room_id,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")

    # Verify membership removed
    from mojo.apps.chat.models import ChatMembership
    ms = ChatMembership.objects.filter(
        room_id=opts.channel_room_id, user=opts.user2,
    ).first()
    assert_true(ms is None, "expected membership to be removed after leaving")


@th.django_unit_test()
def test_cannot_leave_dm(opts):
    """Cannot leave a DM room."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/leave', {
        'room_id': opts.dm_room_id,
    })
    assert_eq(resp.status_code, 400, f"expected 400, got {resp.status_code}")


@th.django_unit_test()
def test_mute_member(opts):
    """Admin can mute a member."""
    opts.client.login(TEST_EMAIL_3, TEST_PASSWORD)

    # Admin adds user2 to group room first
    resp = opts.client.post('/api/chat/room/member/add', {
        'room_id': opts.group_room_id,
        'user_id': opts.user2.pk,
    })

    resp = opts.client.post('/api/chat/room/member/mute', {
        'room_id': opts.group_room_id,
        'user_id': opts.user2.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    assert_eq(resp.json.data.status, "muted", "expected muted status")


@th.django_unit_test()
def test_ban_member(opts):
    """Admin can ban a member."""
    opts.client.login(TEST_EMAIL_3, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/member/ban', {
        'room_id': opts.group_room_id,
        'user_id': opts.user2.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")
    assert_eq(resp.json.data.status, "banned", "expected banned status")


@th.django_unit_test()
def test_update_room_rules(opts):
    """Room owner can update rules."""
    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/rules', {
        'room_id': opts.group_room_id,
        'allow_urls': False,
        'max_message_length': 1000,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}")

    from mojo.apps.chat.models import ChatRoom
    room = ChatRoom.objects.get(pk=opts.group_room_id)
    assert_eq(room.rules.get("allow_urls"), False, "expected allow_urls=False")
    assert_eq(room.rules.get("max_message_length"), 1000, "expected max_message_length=1000")


@th.django_unit_test()
def test_update_rules_requires_admin(opts):
    """Non-admin cannot update room rules."""
    # user2 is banned, use a fresh login
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/rules', {
        'room_id': opts.group_room_id,
        'allow_urls': True,
    })
    assert_eq(resp.status_code, 403, f"expected 403, got {resp.status_code}")
