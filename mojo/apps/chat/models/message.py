from django.db import models
from mojo.models import MojoModel


MESSAGE_KIND_CHOICES = [
    ("text", "Text"),
    ("image", "Image"),
    ("system", "System"),
    # Opaque, consumer-defined typed payload in `metadata`. Client-authored
    # only when the host registers a validator for it.
    ("card", "Card"),
    # Server-authored only -- the send service refuses a client-authored
    # `file` frame. Listed because production rows already carry this kind.
    ("file", "File"),
]

MODERATION_CHOICES = [
    ("allow", "Allow"),
    ("warn", "Warn"),
    ("masked", "Masked"),
    ("block", "Block"),
]


class MessageQuerySet(models.QuerySet):
    def create(self, **kwargs):
        backend = kwargs.pop("read_backend", None)
        from ..services.read_state import _UNSET
        handler = kwargs.pop("read_handler", _UNSET)
        publisher = kwargs.pop("read_publisher", None)
        obj = self.model(**kwargs)
        save_args = {"force_insert": True, "using": self.db, "read_backend": backend}
        save_args["read_handler"] = handler
        save_args["read_publisher"] = publisher
        obj.save(**save_args)
        return obj

    def bulk_create(self, objs, **kwargs):
        from ..services.backend import read_backend
        if read_backend() == "cursor":
            raise ValueError("Cursor messages require individual save; use chat maintenance for imports")
        return super().bulk_create(objs, **kwargs)

    def update(self, **kwargs):
        from ..services.backend import read_backend
        if read_backend() == "cursor" and {"room", "room_id", "room_seq"}.intersection(kwargs):
            raise ValueError("Message identity changes require chat maintenance")
        return super().update(**kwargs)


class ChatMessage(models.Model, MojoModel):
    objects = MessageQuerySet.as_manager()
    class RestMeta:
        VIEW_PERMS = ["comms", "manage_chat"]
        # user pinned so comms admins can't spoof message authorship.
        NO_SAVE_FIELDS = [
            "user", "is_flagged", "flagged_by", "flagged_at",
            "moderation_decision", "moderation_reasons", "moderation_score",
            "client_key", "metadata", "room_seq", "room",
        ]
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "room", "room_seq", "user", "body", "kind", "metadata",
                    "moderation_decision", "moderation_reasons", "moderation_score",
                    "edited_at", "created",
                ],
            },
            "default": {
                "fields": [
                    "id", "room", "room_seq", "user", "body", "kind",
                    "moderation_decision", "moderation_reasons", "moderation_score",
                    "edited_at",
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
    # Null is unscored/legacy; classified clean is 0. Database defaults keep
    # old writers working after migration or an application-only rollback.
    moderation_score = models.PositiveSmallIntegerField(
        null=True, blank=True, default=None, db_default=None,
    )
    moderation_reasons = models.JSONField(default=list, db_default=[], blank=True)
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
    room_seq = models.PositiveBigIntegerField(
        null=True, default=None, db_default=None, editable=False)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["room", "created"]),
            models.Index(fields=["room", "is_flagged", "created"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["room", "room_seq"], condition=models.Q(room_seq__isnull=False),
                name="chat_message_room_seq_uniq"),
            models.UniqueConstraint(
                fields=["room", "user", "client_key"],
                condition=models.Q(client_key__isnull=False),
                name="chat_message_client_key_uniq"),
        ]

    def __str__(self):
        return f"Message {self.pk} in {self.room_id}"

    def save(self, *args, read_backend=None, **kwargs):
        from ..services.backend import read_backend as resolve_backend
        backend = resolve_backend(read_backend)
        if backend == "cursor":
            from ..services.sequences import save_message
            return save_message(self, args, kwargs)
        kwargs.pop("read_handler", None)
        kwargs.pop("read_publisher", None)
        return super().save(*args, **kwargs)
