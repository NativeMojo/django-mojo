from copy import copy

from django.db import models
from mojo import errors as me
from mojo.helpers.request import is_request_user, is_key_backed_session
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
        VIEW_PERMS = ["view_admin", "owner"]
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

    @classmethod
    def _can_read_history(cls, request):
        return (request.user.is_authenticated and is_request_user(request)
                and not is_key_backed_session(request))

    def check_view_permission(self, perms, request):
        return self._can_read_history(request) and (
            self.user_id == request.user.pk or request.user.has_permission("view_admin"))

    @classmethod
    def _audit_history_read(cls, request, operation, instance=None):
        # Oversight is global: never stamp an audit with a caller-selected tenant.
        audit_request = copy(request)
        audit_request.group = None
        details = {"operation": operation}
        if instance is not None:
            details.update(conversation_id=instance.pk, owner_id=instance.user_id)
        cls.class_logit(audit_request, details, kind="assistant:conversation_read",
                        model_id=instance.pk if instance is not None else 0)

    @classmethod
    def on_rest_list_filter(cls, request, queryset):
        if not cls._can_read_history(request):
            raise me.PermissionDeniedException("Conversation history requires a user session")
        oversight = request.user.has_permission("view_admin")
        if not oversight:
            queryset = queryset.filter(user=request.user)
        queryset = super().on_rest_list_filter(request, queryset)
        # This boundary also covers REST exports and aggregate queries, whose
        # response paths do not invoke an instance's on_rest_get hook.
        if oversight and queryset.exclude(user=request.user).exists():
            cls._audit_history_read(request, "list")
        return queryset

    def on_rest_get(self, request, graph="default"):
        response = super().on_rest_get(request, graph=graph)
        if self.user_id != request.user.pk:
            self._audit_history_read(request, "detail", instance=self)
        return response

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
