from django.db import models
from mojo.models import MojoModel


ROLE_CHOICES = [
    ("member", "Member"),
    ("admin", "Admin"),
    ("owner", "Owner"),
]

STATUS_CHOICES = [
    ("active", "Active"),
    ("muted", "Muted"),
    ("banned", "Banned"),
]


class ChatMembership(models.Model, MojoModel):
    class RestMeta:
        VIEW_PERMS = ["chat", "manage_chat"]
        SAVE_PERMS = ["manage_chat"]
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "room", "user", "role", "status", "joined_at",
                ],
            },
            "default": {
                "fields": [
                    "id", "room", "user", "role", "status",
                    "last_read_at", "joined_at", "metadata",
                ],
            },
        }

    room = models.ForeignKey(
        "chat.ChatRoom", on_delete=models.CASCADE, related_name="memberships",
    )
    user = models.ForeignKey(
        "account.User", on_delete=models.CASCADE, related_name="chat_memberships",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="member")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    last_read_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True, editable=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("room", "user")

    def __str__(self):
        return f"{self.user_id} in {self.room_id} ({self.role})"

    @property
    def is_active(self):
        return self.status == "active"

    @property
    def can_send(self):
        return self.status == "active"

    @property
    def is_admin(self):
        return self.role in ("admin", "owner")
