"""
Tests for chat room CRUD, membership, join/leave, and permissions.
"""

TESTIT_TIER = "extended"
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_1 = 'chat-test-user1@example.com'
TEST_EMAIL_2 = 'chat-test-user2@example.com'
TEST_EMAIL_3 = 'chat-test-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_chat_rooms(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom, ChatMembership, ChatMessage

    # Clean up prior test data
    User.objects.filter(email__in=[TEST_EMAIL_1, TEST_EMAIL_2, TEST_EMAIL_3]).delete()
    ChatRoom.objects.filter(name__startswith="test-chat-").delete()

    # Create test users (mark verified so login works with REQUIRE_VERIFIED_EMAIL)
    opts.user1 = User.objects.create_user(
        username=TEST_EMAIL_1, email=TEST_EMAIL_1, password=TEST_PASSWORD,
    )
    opts.user1.is_email_verified = True
    opts.user1.save()
    opts.user2 = User.objects.create_user(
        username=TEST_EMAIL_2, email=TEST_EMAIL_2, password=TEST_PASSWORD,
    )
    opts.user2.is_email_verified = True
    opts.user2.save()
    opts.admin_user = User.objects.create_user(
        username=TEST_EMAIL_3, email=TEST_EMAIL_3, password=TEST_PASSWORD,
    )
    opts.admin_user.is_email_verified = True
    opts.admin_user.save()
    opts.admin_user.add_permission("manage_chat")


@th.tier("core")  # prerequisite: sets opts.group_room_id for the core tests below (#2792)
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


@th.tier("core")
@th.django_unit_test()
def test_join_group_room_fails(opts):
    """Cannot join a group room (invite-only)."""
    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/room/join', {
        'room_id': opts.group_room_id,
    })
    assert_eq(resp.status_code, 403, f"expected 403 for join on group room, got {resp.status_code}")


@th.tier("core")
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


def _shared_direct_room_ids(user_a, user_b):
    """Every direct room `user_a` and `user_b` are both members of."""
    from mojo.apps.chat.models import ChatMembership

    mine = ChatMembership.objects.filter(
        user=user_a, room__kind="direct").values_list("room_id", flat=True)
    return set(ChatMembership.objects.filter(
        user=user_b, room_id__in=mine).values_list("room_id", flat=True))


@th.tier("core")
@th.django_unit_test()
def test_dm_returns_group_scoped_room_with_group_field(opts):
    """When the pair's only shared direct room is group-scoped, /dm returns it.

    The reuse lookup no longer hands a tenant-managed room out of the groupless
    match -- but it must not fork the conversation either. Creating a second,
    groupless room would leave the history in the old one, list both under
    /api/chat/rooms, and tell neither party why the thread went blank. The
    `group` field on the response is how a client tells the two apart.
    """
    from mojo.apps.account.models import Group
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    # Delete before creating -- this runs against a long-lived database.
    ChatRoom.objects.filter(name="test-chat-dm-scoped").delete()
    Group.objects.filter(name="test-chat-dm-group").delete()
    group = Group.objects.create(name="test-chat-dm-group")
    scoped = ChatRoom.objects.create(
        name="test-chat-dm-scoped", kind="direct", group=group, user=opts.user1)
    ChatMembership.objects.create(room=scoped, user=opts.user1, role="owner")
    ChatMembership.objects.create(room=scoped, user=opts.admin_user, role="member")

    before = _shared_direct_room_ids(opts.user1, opts.admin_user)
    assert_eq(
        before, {scoped.pk},
        f"expected the group-scoped room to be the pair's only direct room, got {before}")

    opts.client.login(TEST_EMAIL_1, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/dm', {
        'user_id': opts.admin_user.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")
    assert_eq(
        resp.json.data.id, scoped.pk,
        f"expected the existing group-scoped room {scoped.pk}, got {resp.json.data.id}")
    assert_eq(
        resp.json.data.group, group.pk,
        f"expected the response to carry group={group.pk}, got {resp.json.data.group}")

    after = _shared_direct_room_ids(opts.user1, opts.admin_user)
    assert_eq(
        after, before,
        f"expected no second room to be created for the pair, got {after - before} new")


@th.tier("core")
@th.django_unit_test()
def test_dm_prefers_groupless_room_over_scoped(opts):
    """With both kinds present, /dm hands back the personal (groupless) room.

    This is the actual boundary: the reuse lookup used to filter on
    `kind="direct"` and shared membership only, so a tenant-managed room could
    be returned from the global endpoint as though it were a personal DM. The
    groupless match is now explicit.
    """
    from mojo.apps.account.models import Group
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    # The group-scoped room is created FIRST, so an unordered `.first()` over
    # "any shared direct room" -- what the endpoint used to run -- reaches it
    # ahead of the personal one.
    for name in ("test-chat-dm-both-scoped", "test-chat-dm-both-personal"):
        ChatRoom.objects.filter(name=name).delete()
    Group.objects.filter(name="test-chat-dm-both-group").delete()
    group = Group.objects.create(name="test-chat-dm-both-group")
    scoped = ChatRoom.objects.create(
        name="test-chat-dm-both-scoped", kind="direct", group=group, user=opts.user2)
    ChatMembership.objects.create(room=scoped, user=opts.user2, role="owner")
    ChatMembership.objects.create(room=scoped, user=opts.admin_user, role="member")

    personal = ChatRoom.objects.create(
        name="test-chat-dm-both-personal", kind="direct", user=opts.user2)
    ChatMembership.objects.create(room=personal, user=opts.user2, role="owner")
    ChatMembership.objects.create(room=personal, user=opts.admin_user, role="member")

    shared = _shared_direct_room_ids(opts.user2, opts.admin_user)
    assert_eq(
        shared, {scoped.pk, personal.pk},
        f"expected the pair to share exactly both rooms before the call, got {shared}")

    opts.client.login(TEST_EMAIL_2, TEST_PASSWORD)
    resp = opts.client.post('/api/chat/dm', {
        'user_id': opts.admin_user.pk,
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.json}")
    assert_eq(
        resp.json.data.id, personal.pk,
        f"expected the groupless personal DM {personal.pk}, got {resp.json.data.id} "
        f"(the group-scoped room is {scoped.pk})")
    assert_true(
        resp.json.data.group is None,
        f"expected a groupless room from /dm here, got group={resp.json.data.group}")


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


@th.tier("core")
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
