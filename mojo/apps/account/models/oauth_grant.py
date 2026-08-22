from django.db import models

from mojo.models import MojoModel


class OAuthGrant(models.Model, MojoModel):
    """
    One standing authorization: this user let this client act at this resource.

    The grant is the durable record behind a live credential pair.

      - ``access_jti`` is the ``jti`` of the CURRENT access token. The mcp
        branch of ``User.validate_jwt`` resolves the grant from the presented
        token through it, so a refresh (which replaces the value) kills the
        previous access token immediately, and revocation invalidates a live
        token before its ``exp`` without any denylist.
      - ``refresh_hash`` / ``prev_refresh_hash`` hold sha256 hex of the raw
        refresh secrets. The raw secret exists once, on the wire; nothing here
        can reproduce it.
      - ``refresh_expires`` is absolute — set once at consent and never slid,
        so a credential in a third party's custody has a hard upper bound.

    Both unique columns carry random placeholders from creation onward and are
    only ever overwritten with fresh random values, so no two rows collide and
    a revoked grant's columns match no live token.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey(
        "account.User", related_name="oauth_grants", on_delete=models.CASCADE)
    client = models.ForeignKey(
        "account.OAuthClient", related_name="grants", on_delete=models.CASCADE)

    access_jti = models.CharField(max_length=64, unique=True, db_index=True)
    access_expires = models.DateTimeField(null=True, default=None)
    refresh_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prev_refresh_hash = models.CharField(
        max_length=64, null=True, default=None, db_index=True)
    refresh_expires = models.DateTimeField(db_index=True)
    last_refreshed = models.DateTimeField(null=True, default=None)
    last_used = models.DateTimeField(null=True, default=None)

    scopes = models.JSONField(default=list, blank=True)
    resource = models.CharField(max_length=512)
    auth_time = models.BigIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    revoked_reason = models.CharField(max_length=32, default="", blank=True)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_users", "users"]
        SAVE_PERMS = ["manage_users", "users"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        OWNER_FIELD = "user"
        SENSITIVE_FIELDS = ["access_jti", "refresh_hash", "prev_refresh_hash"]
        NO_SHOW_FIELDS = ["access_jti", "refresh_hash", "prev_refresh_hash"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "resource", "scopes", "is_active", "revoked_reason",
                    "created", "last_used", "refresh_expires",
                ],
                "graphs": {
                    "client": "default",
                    "user": "basic",
                },
            },
        }

    def __str__(self):
        return f"grant {self.pk} -> {self.resource}"
