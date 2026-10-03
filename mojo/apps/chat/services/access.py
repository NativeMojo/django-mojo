"""Frames that announce a change to who may read a room.

Each realtime socket remembers its chat access decision instead of
re-checking the database for every frame (see mojo/apps/realtime/access.py).
It re-checks when one of these frames reaches it on the room topic, so every
write that can take a user's access to a room away publishes one:

- ``chat_member_left``    ``{room_id, user_id}`` -- the member left
- ``chat_member_removed`` ``{room_id, user_id}`` -- an admin removed them
- ``chat_member_banned``  ``{room_id, user_id}`` -- a moderator banned them
- ``chat_room_deleted``   ``{room_id}``          -- the room was deleted

The frame is published after the write commits. Published earlier, the
socket's re-check could read the old rows and re-arm the stale decision.
A publish failure is logged, never raised: the write has already happened,
and the socket's periodic re-check still bounds the missed frame.
"""
from django.db import transaction

from mojo.helpers import logit

logger = logit.get_logger("chat", "chat.log")


def publish_access_change(room_id, kind, user_id=None, *, publisher=None):
    """Publish an access-change frame on the room topic once the write commits.

    `publisher` is a test seam; production callers leave it unset and the
    realtime publisher is used.
    """
    payload = {"type": kind, "room_id": room_id}
    if user_id is not None:
        payload["user_id"] = user_id

    def send():
        try:
            publish = publisher
            if publish is None:
                from mojo.apps.realtime import publish_topic as publish
            publish(f"chat:{room_id}", payload)
        except Exception:
            logger.exception(f"chat: could not publish {kind} for room {room_id}")

    transaction.on_commit(send)
