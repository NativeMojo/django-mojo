from django.db import models
from mojo.models import MojoModel, MojoSecrets


PROVIDER_GODADDY = "godaddy"
PROVIDER_ROUTE53 = "route53"


class DnsCredential(MojoSecrets, MojoModel):
    """
    A provider API credential used to manage DNS for one or more Domains.

    Credentials live here rather than on the Domain row because a single
    provider account (a GoDaddy customer account, for example) holds many
    domains, and rotation has to happen in exactly one place.

    The stored key/secret is the proof of control for BYO onboarding: a
    credential is only marked `verified` after the provider API has confirmed
    it works. A Domain whose provider requires a credential is refused all DNS
    operations until it points at an active, verified credential.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="dns_credentials",
        null=True, blank=True, default=None,
        on_delete=models.CASCADE,
        help_text="Owning group. Null means a house/platform credential.")

    name = models.CharField(
        max_length=200,
        help_text="Human label for this credential, e.g. 'Acme Corp GoDaddy'.")

    provider = models.CharField(
        max_length=32,
        default=PROVIDER_GODADDY,
        db_index=True,
        help_text="DNS provider this credential authenticates against: godaddy.")

    is_active = models.BooleanField(
        default=True,
        help_text="Set False to retire a credential without deleting its history.")

    verified = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True once the provider API confirmed the key/secret works.")

    verified_at = models.DateTimeField(null=True, blank=True, default=None)

    domain_count = models.IntegerField(
        default=0,
        help_text="How many domains the provider account held at verification time. "
                  "A health signal only — the domain list itself is never stored or exposed.")

    last_error = models.TextField(blank=True, null=True, default=None)

    class Meta:
        db_table = "dnsman_credential"
        indexes = [
            models.Index(fields=["group", "provider"]),
        ]
        ordering = ["name"]

    class RestMeta:
        CAN_CREATE = False
        CAN_DELETE = True
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DELETE_PERMS = ["manage_dns", "security"]
        SEARCH_FIELDS = ["name", "provider"]
        NO_SHOW_FIELDS = ["mojo_secrets"]
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "provider", "group",
            "verified", "verified_at", "domain_count", "last_error",
            # on_rest_save_field prefers a set_<key> method and MojoSecrets
            # exposes set_secrets, so without these a POST carrying
            # {"secrets": {...}} would store an unverified key/secret pair
            # while `verified` (blocked above) stayed True — bypassing the
            # whole verify-before-persist invariant of credential linking.
            "secrets", "mojo_secrets",
        ]
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "provider", "is_active", "verified"]
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "name", "provider",
                    "is_active", "verified", "verified_at", "domain_count",
                    "last_error",
                ],
                "extra": ["api_key_masked", "api_secret_masked"],
                "graphs": {"group": "basic"},
            },
        }

    def __str__(self):
        return f"{self.name} ({self.provider})"

    @property
    def api_key(self):
        return self.get_secret("api_key")

    @property
    def api_secret(self):
        return self.get_secret("api_secret")

    @property
    def api_key_masked(self):
        return self._mask(self.get_secret("api_key", ""))

    @property
    def api_secret_masked(self):
        return self._mask(self.get_secret("api_secret", ""))

    @staticmethod
    def _mask(value):
        if not value:
            return ""
        if len(value) > 4:
            return ("*" * (len(value) - 4)) + value[-4:]
        return "*" * len(value)

    @property
    def is_usable(self):
        """A credential may only be used for live DNS operations when all three hold."""
        return bool(self.is_active and self.verified and self.api_key and self.api_secret)

    def set_credentials(self, api_key, api_secret):
        self.set_secret("api_key", api_key)
        self.set_secret("api_secret", api_secret)
