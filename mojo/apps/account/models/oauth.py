from django.db import models

from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets


class OAuthConnection(MojoSecrets, MojoModel):
    """
    Links a MOJO user account to an external OAuth provider identity.

    Tokens stored in mojo_secrets. provider_uid is the stable ID from
    the provider (e.g. Google's 'sub' claim).

    One connection per (user, provider) pair.
    """

    PROVIDER_GOOGLE = "google"

    user = models.ForeignKey(
        "account.User",
        related_name="oauth_connections",
        on_delete=models.CASCADE,
    )
    provider = models.CharField(max_length=32, db_index=True)
    provider_uid = models.CharField(max_length=255, db_index=True)
    email = models.EmailField(blank=True, null=True, default=None)
    is_active = models.BooleanField(default=True, db_index=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        unique_together = [("provider", "provider_uid")]

    class RestMeta:
        VIEW_PERMS = ["owner", "manage_users", "users"]
        SAVE_PERMS = ["manage_users", "users"]
        CAN_DELETE = True
        OWNER_FIELD = "user"
        NO_SHOW_FIELDS = ["mojo_secrets"]
        GRAPHS = {
            "default": {
                "fields": ["id", "provider", "email", "is_active", "created"],
            }
        }

    def __str__(self):
        return f"{self.user.username} via {self.provider}"
