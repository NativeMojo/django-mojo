from django.db import models

from mojo.models import MojoModel


class PublicMessage(models.Model, MojoModel):
    """
    Unauthenticated public message submitted through the bouncer-gated contact page.

    `kind` drives which form fields the page renders and which fields the
    submit endpoint validates. Kind-specific values live in `metadata`.
    """

    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ["view_support", "security", "support"]
        SAVE_PERMS = ["manage_support", "security", "support"]
        DELETE_PERMS = ["manage_support"]
        CAN_DELETE = True
        SEARCH_FIELDS = ["name", "email", "subject", "message"]
        GROUP_FIELD = "group"
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "created", "modified", "kind", "name", "email",
                    "subject", "status",
                ],
                "graphs": {"group": "basic"},
            },
            "default": {
                "graphs": {"group": "basic"},
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group", blank=True, null=True, default=None,
        related_name="public_messages", on_delete=models.SET_NULL,
    )

    kind = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=120)
    email = models.EmailField(max_length=254, db_index=True)
    subject = models.CharField(max_length=255, blank=True, default="")
    message = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=32, default='open', db_index=True)

    ip_address = models.GenericIPAddressField(blank=True, null=True, default=None)
    user_agent = models.CharField(max_length=512, blank=True, default="")

    def __str__(self):
        return f"PublicMessage({self.kind}, {self.email}, {self.status})"
