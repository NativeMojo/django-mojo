from django.db import models

from mojo.models import MojoModel


class Passkey(MojoModel, models.Model):
    """
    FIDO2 / WebAuthn credential bound to a user.

    Simple model:
    - User can have multiple passkeys for different portals (different rp_id)
    - rp_id is derived from the origin hostname during registration
    - Challenges are stored in Redis (not in database)
    """

    user = models.ForeignKey(
        "account.User",
        related_name="passkeys",
        on_delete=models.CASCADE,
    )
    token = models.TextField(
        help_text="Base64url encoded AttestedCredentialData payload"
    )
    credential_id = models.CharField(max_length=255, unique=True)
    rp_id = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Relying Party ID (hostname of the portal)",
    )
    is_enabled = models.BooleanField(default=True, db_index=True)
    sign_count = models.BigIntegerField(default=0)
    transports = models.JSONField(default=list, blank=True)
    friendly_name = models.CharField(max_length=255, null=True, blank=True)
    aaguid = models.CharField(max_length=36, null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)
    last_used = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        indexes = [
            models.Index(fields=["user", "rp_id", "is_enabled"]),
        ]

    class RestMeta:
        VIEW_PERMS = ["owner", "manage_users"]
        SAVE_PERMS = ["owner", "manage_users"]
        DELETE_PERMS = ["owner", "manage_users"]
        CAN_DELETE = True
        OWNER_FIELD = "user"
        UNIQUE_LOOKUP = ["credential_id"]
        NO_SHOW_FIELDS = ["token"]
        NO_SAVE_FIELDS = [
            "token",
            "credential_id",
            "rp_id",
            "sign_count",
            "transports",
            "aaguid",
            "created",
            "modified",
            "last_used",
            "user",
        ]
        LIST_DEFAULT_FILTERS = {"is_enabled": True}
        SEARCH_FIELDS = ["friendly_name", "credential_id"]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id",
                    "friendly_name",
                    "credential_id",
                    "rp_id",
                    "is_enabled",
                    "last_used",
                    "created",
                ]
            },
            "default": {
                "fields": [
                    "id",
                    "friendly_name",
                    "credential_id",
                    "rp_id",
                    "is_enabled",
                    "sign_count",
                    "transports",
                    "aaguid",
                    "last_used",
                    "created",
                    "modified",
                ],
                "graphs": {"user": "basic"},
            },
        }

    def __str__(self):
        name = self.friendly_name or self.credential_id[:8]
        return f"{self.user.username} - {name} ({self.rp_id})"
