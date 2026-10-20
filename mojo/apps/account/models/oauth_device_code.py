from django.db import models

from mojo.models import MojoModel


class OAuthDeviceCode(models.Model, MojoModel):
    """
    One RFC 8628 device authorization request, from start to single use.

    A headless client starts the request and receives two codes: the long
    ``device_code`` it polls the token endpoint with, and the short
    ``user_code`` a person types on the verification page to approve it.
    Neither is stored raw.

      - ``device_code_hash`` is the sha256 hex of a 256-bit secret.
      - ``user_code_hash`` is an HMAC keyed by the server's secret. The user
        code carries only ~34 bits, so a plain hash would fall to an offline
        search the moment this table leaked.

    ``status`` moves pending → approved | denied, then approved → consumed, and
    every move is a conditional UPDATE so two racing requests settle by
    rowcount. A device code presented after it was consumed is replay: the
    grant it produced is revoked and a security incident is reported.

    The ``requester_*`` columns are what the approving person is shown — facts
    about the machine that started the request, which a phishing link cannot
    choose.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    client = models.ForeignKey(
        "account.OAuthClient", related_name="device_codes", on_delete=models.CASCADE)
    user = models.ForeignKey(
        "account.User", related_name="oauth_device_codes", null=True, default=None,
        on_delete=models.CASCADE)
    grant = models.ForeignKey(
        "account.OAuthGrant", related_name="device_codes", null=True, default=None,
        on_delete=models.SET_NULL)

    device_code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    user_code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scope = models.CharField(max_length=200, default="", blank=True)
    resource = models.CharField(max_length=512)
    status = models.CharField(max_length=16, default="pending", db_index=True)
    interval = models.PositiveSmallIntegerField(default=5)
    last_polled = models.DateTimeField(null=True, default=None)
    expires = models.DateTimeField(db_index=True)

    requester_ip = models.CharField(max_length=45, null=True, blank=True)
    requester_ua = models.CharField(max_length=512, default="", blank=True)
    requester_geo = models.CharField(max_length=200, default="", blank=True)

    auth_time = models.BigIntegerField(default=0)
    approved_at = models.DateTimeField(null=True, default=None)
    grant_ttl_days = models.PositiveSmallIntegerField(null=True, default=None)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_users", "users"]
        SAVE_PERMS = ["manage_users", "users"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["device_code_hash", "user_code_hash"]
        NO_SHOW_FIELDS = ["device_code_hash", "user_code_hash"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "scope", "resource", "status", "expires",
                    "requester_ip", "requester_geo", "approved_at",
                    "grant_ttl_days", "created",
                ],
            },
        }

    def __str__(self):
        return f"device code {self.pk} ({self.status}) -> {self.resource}"
