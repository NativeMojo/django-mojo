from django.db import models
from mojo.models import MojoModel


KIND_CHOICES = [
    ("direct", "Direct Message"),
    ("group", "Group Chat"),
    ("channel", "Channel"),
]

# Sentinel for the on_rest_pre_delete test seam. Local on purpose: importing
# the services package at model-import time would be load-order coupling for
# nothing.
_UNSET = object()

DEFAULT_RULES = {
    "allow_urls": True,
    "allow_media": True,
    "allow_phone_numbers": True,
    "max_message_length": 4000,
    "disappearing_ttl": 0,
    "rate_limit": 10,
}


class ChatRoom(models.Model, MojoModel):
    class RestMeta:
        VIEW_PERMS = ["comms", "manage_chat", "owner"]
        CREATE_PERMS = ["authenticated"]
        SAVE_PERMS = ["manage_chat", "comms", "owner"]
        CAN_DELETE = True
        DELETE_PERMS = ["manage_chat"]
        OWNER_FIELD = "user"
        GROUP_FIELD = "group"
        SEARCH_FIELDS = ["name"]
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "name", "kind", "group", "user",
                    "created", "modified",
                ],
            },
            "default": {
                "fields": [
                    "id", "name", "kind", "group", "user",
                    "rules", "metadata", "created", "modified",
                ],
            },
        }

    name = models.CharField(max_length=255, blank=True, default="")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="group", db_index=True)
    user = models.ForeignKey(
        "account.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="owned_chat_rooms",
    )
    group = models.ForeignKey(
        "account.Group", null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_rooms",
    )
    rules = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-modified"]

    def __str__(self):
        return self.name or f"ChatRoom {self.pk}"

    @property
    def topic(self):
        return f"chat:{self.pk}"

    def get_rule(self, key, default=None):
        if default is None:
            default = DEFAULT_RULES.get(key)
        return self.rules.get(key, default)

    def on_rest_pre_save(self, changed_fields, created):
        if created and not self.rules:
            self.rules = dict(DEFAULT_RULES)

    def on_rest_pre_delete(self, *, handler=_UNSET):
        """Notify the deletion handler before the message cascade runs.

        `ChatMessage.room` is `on_delete=CASCADE` and `RestMeta.CAN_DELETE` is
        True, so one room delete destroys every message in it at once -- the
        worse orphan case for a consumer holding message references. The ids
        are only readable before the delete, hence the pre hook.

        `handler` is a keyword-only test seam with a sentinel default; the REST
        layer calls this with no arguments.
        """
        from ..services.deletion import notify_room_deleted
        if handler is _UNSET:
            notify_room_deleted(self)
        else:
            notify_room_deleted(self, handler=handler)

    def on_rest_created(self):
        if self.user:
            from .membership import ChatMembership
            ChatMembership.objects.get_or_create(
                room=self, user=self.user,
                defaults={"role": "owner"},
            )
