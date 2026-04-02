from django.db import models
from mojo.models import MojoModel


class Conversation(models.Model, MojoModel):
    """A multi-turn assistant conversation owned by a single user."""

    user = models.ForeignKey("account.User", on_delete=models.CASCADE,
                             related_name="assistant_conversations")
    title = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-modified"]

    class RestMeta:
        # All mutations go through the service layer (run_assistant).
        # RestMeta endpoint is read-only, owner-scoped.
        NO_REST_SAVE = True
        VIEW_PERMS = ["view_admin"]
        OWNER_FIELD = "user"
        GRAPHS = {
            "default": {
                "fields": ["id", "title", "created", "modified"],
            },
        }

    def __str__(self):
        return f"Conversation {self.pk} ({self.user})"


class Message(models.Model, MojoModel):
    """A single message in an assistant conversation (user, assistant, or tool)."""

    ROLE_CHOICES = [
        ("user", "User"),
        ("assistant", "Assistant"),
        ("tool_use", "Tool Use"),
        ("tool_result", "Tool Result"),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     related_name="messages")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, db_index=True)
    content = models.TextField(blank=True, default="")
    tool_calls = models.JSONField(default=None, null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        ordering = ["created"]

    class RestMeta:
        # Messages are only accessible via the conversation detail endpoint.
        # No direct RestMeta endpoint exposed.
        NO_REST = True
        VIEW_PERMS = ["view_admin"]
        GRAPHS = {
            "default": {
                "fields": ["id", "role", "content", "created"],
            },
        }

    def __str__(self):
        return f"Message {self.pk} ({self.role})"
