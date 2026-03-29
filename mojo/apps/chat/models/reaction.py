from django.db import models
from mojo.models import MojoModel


class ChatReaction(models.Model, MojoModel):
    class RestMeta:
        VIEW_PERMS = ["comms", "manage_chat"]
        GRAPHS = {
            "default": {
                "fields": ["id", "message", "user", "emoji"],
            },
        }

    message = models.ForeignKey(
        "chat.ChatMessage", on_delete=models.CASCADE, related_name="reactions",
    )
    user = models.ForeignKey(
        "account.User", on_delete=models.CASCADE, related_name="chat_reactions",
    )
    emoji = models.CharField(max_length=8)

    class Meta:
        unique_together = ("message", "user", "emoji")

    def __str__(self):
        return f"{self.user_id} reacted {self.emoji} on {self.message_id}"
