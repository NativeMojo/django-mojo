from django.db import models
from mojo.models import MojoModel, KSMSecrets


STATUS_PENDING = "pending"
STATUS_ISSUING = "issuing"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"
STATUS_REVOKED = "revoked"


class Certificate(KSMSecrets, MojoModel):
    """
    A TLS certificate this system issued and holds custody of.

    The certificate and chain are public material and safe to hand an
    authorized caller. The private key is KMS-envelope-encrypted and leaves the
    database through exactly one gated, access-logged endpoint — never through
    a graph and never in the cert-updated broadcast payload.

    Serving hosts are dumb consumers: they hear "a cert changed" on the job
    channel and pull the material themselves. Re-publishing to a fresh box is
    therefore a sync, not a re-issuance, which keeps us clear of CA rate limits.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    domain = models.ForeignKey(
        "dnsman.Domain",
        related_name="certificates",
        on_delete=models.CASCADE)

    common_name = models.CharField(max_length=253, db_index=True)

    sans = models.JSONField(
        default=list, blank=True,
        help_text="Every name on the certificate, including wildcards.")

    status = models.CharField(
        max_length=32,
        default=STATUS_PENDING,
        db_index=True,
        help_text="pending | issuing | active | failed | revoked")

    issuer = models.CharField(max_length=255, null=True, blank=True, default=None)
    serial = models.CharField(max_length=128, null=True, blank=True, default=None)

    not_before = models.DateTimeField(null=True, blank=True, default=None)
    not_after = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    renew_after = models.DateTimeField(
        null=True, blank=True, default=None, db_index=True,
        help_text="When the renewal scan should pick this up.")

    cert_pem = models.TextField(blank=True, null=True, default=None)
    chain_pem = models.TextField(blank=True, null=True, default=None)

    acme_order_url = models.CharField(max_length=512, null=True, blank=True, default=None)

    last_error = models.TextField(blank=True, null=True, default=None)
    attempts = models.IntegerField(default=0)

    class Meta:
        db_table = "dnsman_certificate"
        indexes = [
            models.Index(fields=["status", "renew_after"]),
            models.Index(fields=["domain", "status"]),
        ]
        ordering = ["-created"]

    class RestMeta:
        # Issued by the cert service only; revoke rather than delete.
        CAN_CREATE = False
        CAN_DELETE = False
        # No direct group FK — scope through the owning Domain.
        GROUP_FIELD = "domain__group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        SEARCH_FIELDS = ["common_name", "status"]
        NO_SHOW_FIELDS = ["mojo_secrets", "cert_pem", "chain_pem"]
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "domain", "common_name", "sans", "status",
            "issuer", "serial", "not_before", "not_after", "renew_after",
            "cert_pem", "chain_pem", "acme_order_url", "last_error", "attempts",
            # on_rest_save_field prefers a set_<key> method, and KSMSecrets
            # exposes set_secrets — so without these a manage_dns holder could
            # POST {"mojo_secrets": "junk"} and destroy the KMS blob. Decrypt
            # failures are swallowed into an empty mapping, so the certificate
            # would then report "material unavailable" forever.
            "secrets", "mojo_secrets",
        ]
        GRAPHS = {
            "basic": {
                "fields": ["id", "common_name", "status", "not_after"]
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "common_name", "sans", "status",
                    "issuer", "serial", "not_before", "not_after", "renew_after",
                    "last_error", "attempts",
                ],
                "extra": ["days_remaining"],
                "graphs": {"domain": "basic"},
            },
        }

    def __str__(self):
        return f"{self.common_name} [{self.status}]"

    @property
    def private_key_pem(self):
        return self.get_secret("private_key_pem")

    def set_private_key_pem(self, pem):
        self.set_secret("private_key_pem", pem)

    @property
    def days_remaining(self):
        if not self.not_after:
            return None
        from mojo.helpers import dates
        return (self.not_after - dates.utcnow()).days

    @property
    def has_material(self):
        """
        KSMSecrets returns an empty mapping when KMS decryption fails, so an
        empty key on an otherwise-active certificate means 'temporarily
        unavailable', not 'no key'. Callers must distinguish the two.
        """
        return bool(self.cert_pem and self.private_key_pem)
