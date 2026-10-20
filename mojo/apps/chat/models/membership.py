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


class MembershipQuerySet(models.QuerySet):
    def create(self, **kwargs):
        backend = kwargs.pop("read_backend", None)
        obj = self.model(**kwargs)
        obj.save(force_insert=True, using=self.db, read_backend=backend)
        return obj

    def bulk_create(self, objs, **kwargs):
        from ..services.memberships import create_memberships
        backend = kwargs.pop("read_backend", None)
        return create_memberships(self, list(objs), kwargs, backend=backend)

    def update(self, **kwargs):
        from ..services.backend import read_backend
        if read_backend() == "cursor" and {"room", "room_id", "user", "user_id"}.intersection(kwargs):
            raise ValueError("Membership identity changes require chat maintenance")
        return super().update(**kwargs)


class ChatMembership(models.Model, MojoModel):
    objects = MembershipQuerySet.as_manager()
    class RestMeta:
        VIEW_PERMS = ["comms", "manage_chat"]
        SAVE_PERMS = ["manage_chat", "comms"]
        NO_SAVE_FIELDS = ["last_read_at", "last_read_seq", "last_read_message_id", "unread_exception_count"]
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "room", "user", "role", "status", "joined_at",
                ],
            },
            "default": {
                "fields": [
                    "id", "room", "user", "role", "status",
                    "last_read_at", "last_read_seq", "last_read_message_id",
                    "unread_exception_count", "joined_at", "metadata",
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
    last_read_seq = models.PositiveBigIntegerField(default=0, db_default=0, editable=False)
    last_read_message_id = models.PositiveBigIntegerField(
        null=True, default=None, db_default=None, editable=False)
    unread_exception_count = models.PositiveIntegerField(default=0, db_default=0, editable=False)
    joined_at = models.DateTimeField(auto_now_add=True, editable=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("room", "user")

    def __str__(self):
        return f"{self.user_id} in {self.room_id} ({self.role})"

    def save(self, *args, read_backend=None, **kwargs):
        from ..services.memberships import save_membership
        return save_membership(self, args, kwargs, backend=read_backend)

    @property
    def is_active(self):
        return self.status == "active"

    @property
    def can_send(self):
        return self.status == "active"

    @property
    def is_admin(self):
        return self.role in ("admin", "owner")
