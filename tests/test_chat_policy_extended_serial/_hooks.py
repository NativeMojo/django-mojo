"""Test-only REALTIME_SUBSCRIBE_HANDLER for the live server (maestro #8874).

The WMWX shape: a restricted player may take part only in support rooms.
Restriction is read fresh from the database on every call, as the docs tell a
project to do, so a test flips it with a plain row update.
"""
RESTRICTED_KEY = "test_chat_policy_restricted"
SUPPORT_PREFIX = "test-chat-policy-support-"


def support_only(user, topic, default):
    from mojo.apps.account.models import User
    from mojo.apps.chat.models import ChatRoom

    if not topic.startswith("chat:"):
        return True
    restricted = User.objects.filter(
        pk=getattr(user, "pk", None), **{f"metadata__{RESTRICTED_KEY}": True}).exists()
    if not restricted:
        return True
    room = ChatRoom.objects.filter(pk=int(topic.split(":", 1)[1])).only("name").first()
    return room is not None and room.name.startswith(SUPPORT_PREFIX)
