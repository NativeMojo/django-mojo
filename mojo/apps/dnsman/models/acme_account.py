from django.db import models
from mojo.models import MojoModel, KSMSecrets


class AcmeAccount(KSMSecrets, MojoModel):
    """
    An ACME account registered with a certificate authority.

    One row per directory URL, created lazily on first issuance. The account
    private key lives in KMS-envelope-encrypted secrets.

    Deliberately has NO REST handler anywhere in this app. It is pure internal
    state, and never routing it is the cheapest possible guarantee that the
    account key cannot appear in a response.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    directory_url = models.CharField(
        max_length=512,
        unique=True,
        db_index=True,
        help_text="ACME directory URL. Distinguishes staging from production.")

    contact_email = models.CharField(max_length=255, null=True, blank=True, default=None)

    account_url = models.CharField(
        max_length=512, null=True, blank=True, default=None,
        help_text="The CA's account URL, used as the JWS 'kid' for every later request.")

    status = models.CharField(max_length=32, default="pending", db_index=True)

    last_error = models.TextField(blank=True, null=True, default=None)

    class Meta:
        db_table = "dnsman_acme_account"
        ordering = ["directory_url"]

    def __str__(self):
        return self.directory_url

    @property
    def private_key_pem(self):
        return self.get_secret("private_key_pem")

    def set_private_key_pem(self, pem):
        self.set_secret("private_key_pem", pem)
