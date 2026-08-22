from django.db import models

from mojo.models import MojoModel


class OAuthCode(models.Model, MojoModel):
    """
    One single-use authorization code, stored as a hash.

    Minted when the user approves on the consent page and burned at the token
    endpoint. ``consumed`` is flipped by a conditional UPDATE, so a concurrent
    exchange is settled by rowcount rather than by a read-then-write race, and
    a presented code that is already consumed is treated as replay: the linked
    grant family is revoked and a security incident is reported.

    Only the sha256 hex of the raw code is stored; the raw code exists once, on
    the wire. ``code_challenge`` is the client's PKCE S256 challenge (itself a
    hash of a verifier this server never sees).
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    client = models.ForeignKey(
        "account.OAuthClient", related_name="codes", on_delete=models.CASCADE)
    user = models.ForeignKey(
        "account.User", related_name="oauth_codes", on_delete=models.CASCADE)

    code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    redirect_uri = models.CharField(max_length=2048)
    code_challenge = models.CharField(max_length=128)
    scope = models.CharField(max_length=200, default="", blank=True)
    resource = models.CharField(max_length=512)
    auth_time = models.BigIntegerField(default=0)
    expires = models.DateTimeField(db_index=True)
    consumed = models.BooleanField(default=False)
    grant = models.ForeignKey(
        "account.OAuthGrant", related_name="codes", null=True, default=None,
        on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_users", "users"]
        SAVE_PERMS = ["manage_users", "users"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["code_hash", "code_challenge"]
        NO_SHOW_FIELDS = ["code_hash", "code_challenge"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "redirect_uri", "scope", "resource", "expires",
                    "consumed", "created",
                ],
            },
        }

    def __str__(self):
        return f"code {self.pk} -> {self.resource}"
