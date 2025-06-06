from django.db import models
from mojo.models import MojoModel


class Attachment(models.Model, MojoModel):
    class RestMeta:
        CAN_SAVE = CAN_CREATE = False
        DEFAULT_SORT = "-id"
        GRAPHS = {
            "default": {
                "graphs": {
                    "media": "basic"
                },
            }
        }
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    name = models.CharField(max_length=255, null=True, default=None)
    content_type = models.CharField(max_length=128, null=True, default=None)
    message = models.ForeignKey(Message, related_name="attachments",
        on_delete=models.CASCADE)
    file = models.ForeignKey("fileman.File", related_name="attachments", on_delete=models.CASCADE)

    def __str__(self):
        return f"attachment: to:{self.message.to_email} from:{self.message.from_email} filename: {self.name}"
