from django.db import models
from mojo.models import MojoModel


class ShortLinkClick(models.Model, MojoModel):
    """
    Individual click record for a short link.
    Only created when the parent ShortLink has track_clicks=True.
    """

    class RestMeta:
        CAN_SAVE = False
        CAN_CREATE = False
        CAN_DELETE = False
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["manage_shortlinks"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "ip", "user_agent", "referer",
                    "is_bot", "created"],
                "graphs": {
                    "shortlink": "basic",
                }
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    shortlink = models.ForeignKey(
        "shortlink.ShortLink",
        related_name="clicks",
        on_delete=models.CASCADE,
    )

    ip = models.GenericIPAddressField(null=True, blank=True)

    user_agent = models.TextField(blank=True, default="")

    referer = models.TextField(blank=True, default="")

    is_bot = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["shortlink", "created"]),
        ]

    def __str__(self):
        return f"Click on {self.shortlink.code} from {self.ip or 'unknown'}"
