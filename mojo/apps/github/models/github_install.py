from django.db import models

from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets


class GitHubInstall(MojoSecrets, MojoModel):
    """
    Tracks a GitHub App installation.

    Each record represents one GitHub App installation on a GitHub org or user
    account. The encrypted secret stored is ``token`` (the GitHub installation
    access token). Consuming apps can store additional data in ``metadata``.

    When ``group`` is NULL the installation is global (not scoped to a group).
    """

    group = models.ForeignKey(
        "account.Group",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="github_installs",
    )
    installation_id = models.BigIntegerField(db_index=True, unique=True)
    account_name = models.CharField(max_length=255)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ["github", "view_github", "manage_github"]
        SAVE_PERMS = ["github", "manage_github"]
        DELETE_PERMS = ["github", "manage_github"]
        CAN_DELETE = True
        NO_SHOW_FIELDS = ["mojo_secrets"]
        GRAPHS = {
            "list": {
                "fields": [
                    "id", "installation_id", "account_name",
                    "token_expires_at", "created",
                ],
            },
            "default": {
                "fields": [
                    "id", "installation_id", "account_name",
                    "token_expires_at", "permissions", "metadata",
                    "group", "created", "modified",
                ],
            },
        }

    def __str__(self):
        return f"GitHubInstall {self.installation_id} ({self.account_name})"
