from django.db import models
from mojo.models import MojoModel


class VaultData(models.Model, MojoModel):
    """Encrypted structured JSON stored in the database."""

    class RestMeta:
        CAN_SAVE = True
        CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["view_vault", "manage_vault", "files", "owner"]
        SAVE_PERMS = ["manage_vault", "files", "owner"]
        DELETE_PERMS = ["manage_vault", "owner"]
        SEARCH_FIELDS = ["name", "description"]
        SEARCH_TERMS = [
            "name",
            ("group", "group__name")]
        NO_SAVE_FIELDS = ["id", "pk", "ekey", "edata", "hashed_password"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified", "name", "description", "metadata"
                ],
                "extra": ["requires_password"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            },
            "basic": {
                "fields": ["id", "name", "description"],
                "extra": ["requires_password"]
            },
            "list": {
                "fields": [
                    "id", "created", "name", "description"
                ],
                "extra": ["requires_password"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            }
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey(
        "account.User",
        related_name="vault_data",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL)

    group = models.ForeignKey(
        "account.Group",
        related_name="vault_data",
        on_delete=models.CASCADE)

    name = models.CharField(max_length=64)
    description = models.TextField(blank=True, null=True, default=None)
    ekey = models.TextField()
    edata = models.TextField()
    hashed_password = models.TextField(blank=True, null=True, default=None)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["group", "created"]),
            models.Index(fields=["user", "created"]),
        ]

    def __str__(self):
        return self.name

    @property
    def requires_password(self):
        return self.hashed_password is not None
