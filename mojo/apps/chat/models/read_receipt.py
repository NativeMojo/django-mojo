from django.db import models
from mojo.models import MojoModel


class ChatReadReceipt(models.Model, MojoModel):
    class RestMeta:
        VIEW_PERMS = ["comms", "manage_chat"]
        GRAPHS = {
            "default": {
                "fields": ["id", "message", "user", "read_at"],
            },
        }

    message = models.ForeignKey(
        "chat.ChatMessage", on_delete=models.CASCADE, related_name="read_receipts",
    )
    user = models.ForeignKey(
        "account.User", on_delete=models.CASCADE, related_name="chat_read_receipts",
    )
    read_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        unique_together = ("message", "user")

    def __str__(self):
        return f"{self.user_id} read {self.message_id}"
