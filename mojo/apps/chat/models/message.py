from django.db import models
from mojo.models import MojoModel


MESSAGE_KIND_CHOICES = [
    ("text", "Text"),
    ("image", "Image"),
    ("system", "System"),
]

MODERATION_CHOICES = [
    ("allow", "Allow"),
    ("warn", "Warn"),
    ("block", "Block"),
]


class ChatMessage(models.Model, MojoModel):
    class RestMeta:
        VIEW_PERMS = ["comms", "manage_chat"]
        # user pinned so comms admins can't spoof message authorship.
        NO_SAVE_FIELDS = [
            "user", "is_flagged", "flagged_by", "flagged_at",
            "moderation_decision", "client_key",
        ]
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "room", "user", "body", "kind",
                    "edited_at", "created",
                ],
            },
            "default": {
                "fields": [
                    "id", "room", "user", "body", "kind",
                    "moderation_decision", "edited_at",
                    "is_flagged", "flagged_by", "flagged_at",
                    "metadata", "client_key", "created",
                ],
            },
        }

    room = models.ForeignKey(
        "chat.ChatRoom", on_delete=models.CASCADE, related_name="messages",
    )
    user = models.ForeignKey(
        "account.User", null=True, on_delete=models.SET_NULL,
        related_name="chat_messages",
    )
    body = models.TextField()
    kind = models.CharField(max_length=20, choices=MESSAGE_KIND_CHOICES, default="text")
    moderation_decision = models.CharField(
        max_length=10, choices=MODERATION_CHOICES, default="allow",
    )
    edited_at = models.DateTimeField(null=True, blank=True)
    is_flagged = models.BooleanField(default=False, db_index=True)
    flagged_by = models.ForeignKey(
        "account.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    flagged_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # Optional client-supplied idempotency key for a send. Scoped to
    # (room, user) by the partial unique constraint below; no separate
    # index -- that constraint leads with room, user and serves the lookup.
    client_key = models.CharField(max_length=64, null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["room", "created"]),
            models.Index(fields=["room", "is_flagged", "created"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["room", "user", "client_key"],
                condition=models.Q(client_key__isnull=False),
                name="chat_message_client_key_uniq"),
        ]

    def __str__(self):
        return f"Message {self.pk} in {self.room_id}"
