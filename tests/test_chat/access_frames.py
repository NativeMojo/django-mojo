"""Every REST write that takes room access away publishes a frame on the room
topic, after the write commits. Open sockets remember chat access instead of
querying per frame, and these frames are what make them re-check at once."""
import json
import time

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "framework"

ADMIN_EMAIL = "chat-access-frames-admin@example.com"
MEMBER_EMAIL = "chat-access-frames-member@example.com"
PASSWORD = "TestPass1!"
ROOM_PREFIX = "test-chat-access-frames-"


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_access_frames(opts):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    User.objects.filter(email__in=[ADMIN_EMAIL, MEMBER_EMAIL]).delete()
    ChatRoom.objects.filter(name__startswith=ROOM_PREFIX).delete()
    for attr, email in (("admin", ADMIN_EMAIL), ("member", MEMBER_EMAIL)):
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_email_verified = True
        user.save()
        setattr(opts, attr, user)
    opts.admin.add_permission("manage_chat")


def _room(opts, label, kind="group"):
    from mojo.apps.chat.models import ChatMembership, ChatRoom

    room = ChatRoom.objects.create(name=ROOM_PREFIX + label, kind=kind, user=opts.admin)
    ChatMembership.objects.create(room=room, user=opts.admin, role="owner")
    ChatMembership.objects.create(room=room, user=opts.member, role="member")
    return room


def _listen(room_id):
    from mojo.apps.realtime.channels import topic_channel
    from mojo.helpers.redis import get_connection

    pubsub = get_connection().pubsub()
    pubsub.subscribe(topic_channel(f"chat:{room_id}"))
    end = time.time() + 2
    while time.time() < end:
        msg = pubsub.get_message(timeout=0.1)
        if msg and msg.get("type") == "subscribe":
            break
    return pubsub


def _frames(pubsub, seconds=3.0):
    """Collect the published payloads until one arrives, then briefly more."""
    out = []
    end = time.time() + seconds
    while time.time() < end:
        msg = pubsub.get_message(timeout=0.1)
        if msg and msg.get("type") == "message":
            out.append(json.loads(msg["data"])["data"])
            end = min(end, time.time() + 0.3)
    return out


def _access_frames(frames):
    return [f for f in frames if f.get("type") in (
        "chat_member_left", "chat_member_removed", "chat_member_banned", "chat_room_deleted")]


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_remove_member_publishes_frame(opts):
    from mojo.apps.chat.models import ChatMembership

    room = _room(opts, "remove")
    pubsub = _listen(room.pk)
    try:
        opts.client.login(ADMIN_EMAIL, PASSWORD)
        resp = opts.client.post("/api/chat/room/member/remove", {
            "room_id": room.pk, "user_id": opts.member.pk})
        assert_eq(resp.status_code, 200, f"admin removal must succeed: {resp.json}")
        assert_true(not ChatMembership.objects.filter(room=room, user=opts.member).exists(),
                    "the membership must be gone")
        frames = _access_frames(_frames(pubsub))
        assert_eq(frames, [{"type": "chat_member_removed", "room_id": room.pk, "user_id": opts.member.pk}],
                  f"removal must publish exactly one chat_member_removed frame, got {frames}")
    finally:
        pubsub.close()
        room.delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_ban_member_publishes_frame(opts):
    room = _room(opts, "ban")
    pubsub = _listen(room.pk)
    try:
        opts.client.login(ADMIN_EMAIL, PASSWORD)
        resp = opts.client.post("/api/chat/room/member/ban", {
            "room_id": room.pk, "user_id": opts.member.pk})
        assert_eq(resp.status_code, 200, f"admin ban must succeed: {resp.json}")
        frames = _access_frames(_frames(pubsub))
        assert_eq(frames, [{"type": "chat_member_banned", "room_id": room.pk, "user_id": opts.member.pk}],
                  f"a ban must publish exactly one chat_member_banned frame, got {frames}")
    finally:
        pubsub.close()
        room.delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_leave_publishes_frame(opts):
    room = _room(opts, "leave", kind="channel")
    pubsub = _listen(room.pk)
    try:
        opts.client.login(MEMBER_EMAIL, PASSWORD)
        resp = opts.client.post("/api/chat/room/leave", {"room_id": room.pk})
        assert_eq(resp.status_code, 200, f"leaving must succeed: {resp.json}")
        frames = _access_frames(_frames(pubsub))
        assert_eq(frames, [{"type": "chat_member_left", "room_id": room.pk, "user_id": opts.member.pk}],
                  f"leaving must publish exactly one chat_member_left frame, got {frames}")
    finally:
        pubsub.close()
        room.delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_room_delete_publishes_frame(opts):
    from mojo.apps.chat.models import ChatRoom

    room = _room(opts, "delete")
    room_id = room.pk
    pubsub = _listen(room_id)
    try:
        opts.client.login(ADMIN_EMAIL, PASSWORD)
        resp = opts.client.delete(f"/api/chat/room/{room_id}")
        assert_eq(resp.status_code, 200, f"a manage_chat holder must delete the room: {resp.json}")
        assert_true(not ChatRoom.objects.filter(pk=room_id).exists(), "the room must be gone")
        frames = _access_frames(_frames(pubsub))
        assert_eq(frames, [{"type": "chat_room_deleted", "room_id": room_id}],
                  f"room deletion must publish exactly one chat_room_deleted frame, got {frames}")
    finally:
        pubsub.close()
        ChatRoom.objects.filter(pk=room_id).delete()


@th.django_unit_test()
@th.requires_app("mojo.apps.chat")
def test_access_frame_waits_for_commit(opts):
    from django.db import transaction
    from mojo.apps.chat.services.access import publish_access_change

    published = []

    def publisher(topic, payload):
        published.append((topic, payload))

    with transaction.atomic():
        publish_access_change(41, "chat_member_removed", 7, publisher=publisher)
        assert_eq(published, [], "the frame must not be published before the write commits")
    assert_eq(published, [("chat:41", {"type": "chat_member_removed", "room_id": 41, "user_id": 7})],
              f"the frame must be published once the write commits, got {published}")

    try:
        with transaction.atomic():
            publish_access_change(41, "chat_member_removed", 7, publisher=publisher)
            raise RuntimeError("roll back")
    except RuntimeError:
        pass
    assert_eq(len(published), 1, "a rolled-back write must publish nothing")

    def failing(topic, payload):
        raise RuntimeError("redis down")

    publish_access_change(41, "chat_room_deleted", publisher=failing)  # must not raise
