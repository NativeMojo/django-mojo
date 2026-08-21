from django.db import models
from mojo.models import MojoModel


class Conversation(models.Model, MojoModel):
    """A multi-turn assistant conversation owned by a single user."""

    user = models.ForeignKey("account.User", on_delete=models.CASCADE,
                             related_name="assistant_conversations")
    group = models.ForeignKey("account.Group", on_delete=models.SET_NULL,
                              null=True, blank=True, related_name="assistant_conversations")
    title = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-modified"]

    class RestMeta:
        NO_REST_SAVE = True
        VIEW_PERMS = ["view_admin", "assistant", "owner"]
        OWNER_FIELD = "user"
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "fields": ["id", "title", "created", "modified"],
                "graphs": {"user": "basic"},
            },
            "detail": {
                "fields": ["id", "title", "created", "modified", "messages"],
                "graphs": {"messages": "default", "user": "basic"},
                "extra": [("get_pending_actions", "pending_actions")],
            },
        }

    def get_pending_actions(self):
        """Current state of this conversation's approval cards, in one query.

        Message blocks carry the card AS PROPOSED. Re-loading a history without
        this would offer Approve on an action that expired an hour ago; with it,
        resolved and expired cards render inert.
        """
        from mojo.apps.assistant.services import approvals

        return approvals.states_for_conversation(self)

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
    blocks = models.JSONField(default=None, null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True, default=None)
    usage = models.JSONField(default=None, null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        ordering = ["created"]

    class RestMeta:
        NO_REST = True
        VIEW_PERMS = ["view_admin"]
        GRAPHS = {
            "default": {
                "fields": ["id", "role", "content", "tool_calls", "duration_ms", "usage", "created"],
                "extra": [("get_rest_blocks", "blocks")],
            },
        }

    def get_rest_blocks(self):
        from mojo.apps.assistant.services.attachments import rest_message_blocks

        return rest_message_blocks(self.role, self.blocks)

    def __str__(self):
        return f"Message {self.pk} ({self.role})"
