import datetime as _dt
import secrets as _secrets
from django.db import models
from django.utils import timezone
from objict import objict
from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets
from mojo.apps.account.utils.jwtoken import JWToken


class UserAPIKey(MojoSecrets, MojoModel):
    """
    A long-lived JWT API token record for a user.

    Each token is signed with a per-key auth_key stored in mojo_secrets.
    This decouples API key lifetime from session key rotation — revoking a
    user's session does not affect their API keys.

    Revocation: set is_active=False. The validate_jwt path checks this before
    verifying the signature, so the token is immediately rejected.

    The JWT payload carries:
        token_type = "api_key"
        jti        = this record's jti (links token → record)
        uid        = user pk
    """
    user = models.ForeignKey(
        "account.User",
        related_name="api_tokens",
        on_delete=models.CASCADE,
    )
    jti = models.CharField(max_length=64, unique=True, db_index=True)
    label = models.CharField(max_length=200, default="", blank=True)
    allowed_ips = models.JSONField(default=list, blank=True)
    expires = models.DateTimeField(db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    last_used = models.DateTimeField(null=True, default=None)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["owner", "manage_users", "users"]
        SAVE_PERMS = ["owner", "manage_users", "users"]
        OWNER_FIELD = "user"
        NO_SHOW_FIELDS = ["mojo_secrets"]
        NO_SAVE_FIELDS = ["jti", "expires", "user", "last_used"]
        POST_SAVE_ACTIONS = ["revoke"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "label", "allowed_ips", "expires",
                    "is_active", "last_used", "created",
                ],
            }
        }

    def get_auth_key(self):
        """Return the per-key JWT signing secret."""
        return self.get_secret("auth_key")

    def on_action_revoke(self, value):
        self.set_secret("auth_key", _secrets.token_hex(32))
        self.is_active = False
        self.save(update_fields=["is_active", "mojo_secrets", "modified"])
        self.user.log(f"API Key Revoked {self.jti}", "api_key:revoked")
        return {"status": True}

    @classmethod
    def create_for_user(cls, user, expire_days=360, allowed_ips=None, label=""):
        if allowed_ips is None:
            allowed_ips = []
        expire_seconds = expire_days * 24 * 60 * 60
        expires = timezone.now() + _dt.timedelta(seconds=expire_seconds)
        key_secret = _secrets.token_hex(32)
        token = JWToken(key_secret, access_token_expiry=expire_seconds, refresh_token_expiry=expire_seconds)
        package = token.create(uid=user.pk, allowed_ips=allowed_ips, token_type="user_api_key")
        key_record = cls(user=user, jti=token.payload.jti, label=label, allowed_ips=allowed_ips, expires=expires)
        key_record.set_secret("auth_key", key_secret)
        key_record.save()
        return objict(id=key_record.pk, jti=token.payload.jti, expires=token.payload.exp, token=package.access_token)
