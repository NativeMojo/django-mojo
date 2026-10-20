from django.db import models
from mojo.models import MojoModel


class ChatUnreadException(models.Model, MojoModel):
    """Sparse maintenance-only unread identities. Ordinary traffic creates none.

    message_id is deliberately scalar: deleting a message must not erase an
    arrival count slot or invalidate the membership's cached count.
    """
    membership = models.ForeignKey(
        "chat.ChatMembership", on_delete=models.CASCADE, related_name="unread_exceptions")
    room_seq = models.PositiveBigIntegerField(editable=False)
    message_id = models.PositiveBigIntegerField(editable=False)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["membership", "room_seq"], name="chat_unread_exception_seq_uniq")]
        indexes = [models.Index(fields=["membership", "message_id"])]

    class RestMeta:
        VIEW_PERMS = ["comms", "manage_chat"]
        SAVE_PERMS = ["comms", "manage_chat"]
        NO_SAVE_FIELDS = ["membership", "room_seq", "message_id"]
        GRAPHS = {"default": {"fields": ["id"]}}
