from django.db import models

from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets


class UserTOTP(MojoSecrets, MojoModel):
    """
    TOTP (Time-based One-Time Password) credential for a user.

    Secret stored in mojo_secrets — never exposed via API.
    One record per user; re-setup overwrites the existing record.
    """

    user = models.OneToOneField(
        "account.User",
        related_name="totp",
        on_delete=models.CASCADE,
    )
    is_enabled = models.BooleanField(default=False, db_index=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ["owner", "manage_users"]
        SAVE_PERMS = ["owner", "manage_users"]
        OWNER_FIELD = "user"
        NO_SHOW_FIELDS = ["mojo_secrets"]
        GRAPHS = {
            "default": {
                "fields": ["id", "is_enabled", "created", "modified"],
            }
        }

    def __str__(self):
        return f"{self.user.username} TOTP ({'enabled' if self.is_enabled else 'disabled'})"
