from django.db import models
from mojo.models import MojoModel, MojoSecrets


POSIX_BODY_BREAK = "\n\n\n"
WIN_BODY_BREAK = "\r\n\r\n\r\n"


class EmailAccount(models.Model, MojoModel, MojoSecrets):
    """
    Handles how email is sent
    """
    class RestMeta:
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_email"]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    label = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, db_index=True)
    default_from = models.CharField(max_length=255, db_index=True)
    state = models.IntegerField(default=1, db_index=True)
    # kind is ses, smtp, imap, gmail, etc.
    kind = models.CharField(max_length=255, null=True, default=None)

    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"inbox:{self.email}.{self.tq_app}.{self.tq_handler}.{self.tq_channel}"


class EmailRouter(models.Model, MojoModel):
    class RestMeta:
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_email"]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    email = models.CharField(max_length=255, db_index=True)
    state = models.IntegerField(default=1, db_index=True)
    # define how this email address should be handeled
    app = models.CharField(max_length=255, null=True, default=None)
    handler = models.CharField(max_length=255, null=True, default=None)
    channel = models.CharField(max_length=255, null=True, default=None)

    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"email_router:{self.email}.{self.tq_app}.{self.tq_handler}.{self.tq_channel}"
