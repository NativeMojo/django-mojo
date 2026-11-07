"""
GET chat/unread under a confined credential (maestro #9544).

A group token authenticates as a real user, so the default (legacy) unread
read must apply the same tenant bound as GET rooms: only rooms inside the
token's own group are counted, never another tenant's room the user also
sits in.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "bug"

PREFIX = "unread_conf_"


def gt(token):
    return {"Authorization": f"grouptoken {token}"}


@th.django_unit_setup()
@th.requires_app("mojo.apps.chat")
def setup_unread_confinement(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.account.services import group_token
    from mojo.apps.chat.models import ChatMembership, ChatMessage, ChatRoom

    ChatRoom.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()

    group_a = Group.objects.create(name=f"{PREFIX}tenant_a", kind="organization")
    group_b = Group.objects.create(name=f"{PREFIX}tenant_b", kind="organization")
    visitor = User(username=f"{PREFIX}visitor", email=f"{PREFIX}visitor@example.com")
    visitor.save()
    sender = User(username=f"{PREFIX}sender", email=f"{PREFIX}sender@example.com")
    sender.save()
    group_a.add_member(visitor)
    group_b.add_member(visitor)

    rooms = {}
    for label, group in (("a", group_a), ("b", group_b)):
        room = ChatRoom.objects.create(name=f"{PREFIX}room_{label}", kind="group", group=group)
        ChatMembership.objects.create(room=room, user=visitor, role="member")
        ChatMembership.objects.create(room=room, user=sender, role="member")
        ChatMessage.objects.create(room=room, user=sender, body=f"unread {label}", kind="text")
        rooms[label] = room

    opts.unread_room_a_id = rooms["a"].pk
    opts.unread_room_b_id = rooms["b"].pk
    opts.unread_token_a = group_token.mint(visitor, group_a)


@th.django_unit_test()
def test_legacy_unread_confined_to_token_group(opts):
    """A tenant-A group token counts tenant A's room and never names tenant B's."""
    resp = opts.client.get("/api/chat/unread", headers=gt(opts.unread_token_a))
    assert_eq(resp.status_code, 200, f"unread answers 200, got {resp.status_code}: {resp.response}")
    ids = [row["room_id"] for row in resp.response.data]
    assert_true(opts.unread_room_a_id in ids,
                f"the token's own tenant room must be counted: {ids}")
    assert_true(opts.unread_room_b_id not in ids,
                f"an A-token must not see tenant B's room name or unread count: {ids}")
