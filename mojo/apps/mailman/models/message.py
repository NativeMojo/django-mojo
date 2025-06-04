from django.db import models
from mojo.models import MojoModel


POSIX_BODY_BREAK = "\n\n\n"
WIN_BODY_BREAK = "\r\n\r\n\r\n"


class Message(models.Model, MojoModel):
    class RestMeta:
        CAN_SAVE = CAN_CREATE = False
        CAN_DELETE = True
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_email"]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField()

    state = models.IntegerField(default=0, db_index=True)

    to_email = models.CharField(max_length=255, db_index=True)
    to_name = models.CharField(max_length=255, null=True, default=None)
    to = models.TextField()
    cc = models.TextField(null=True, default=None)

    from_email = models.CharField(max_length=255, db_index=True)
    from_name = models.CharField(max_length=255, null=True, default=None)

    subject = models.CharField(max_length=255, null=True, default=None)
    message = models.TextField(null=True, default=None)

    body = models.TextField(null=True, default=None)
    html = models.TextField(null=True, default=None)

    def get_most_recent_body(self):
        body = self.body
        if POSIX_BODY_BREAK in body:
            body = body[:body.find(POSIX_BODY_BREAK)]
        elif WIN_BODY_BREAK in body:
            body = body[:body.find(WIN_BODY_BREAK)]
        return body

    def __str__(self):
        return f"message: to:{self.to_email} from:{self.from_email} subject: {self.subject}"


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
    message = models.ForeignKey(Message, related_name="attachments", on_delete=models.CASCADE)
    file = models.ForeignKey("fileman.File", related_name="attachments", on_delete=models.CASCADE)

    def __str__(self):
        return f"attachment: to:{self.message.to_email} from:{self.message.from_email} filename: {self.name}"
