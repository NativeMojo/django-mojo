from django.db import models

from mojo.models import MojoModel, KSMSecrets


class CdnSigningKey(KSMSecrets, MojoModel):
    """
    One CloudFront signing key pair (maestro #8250).

    The public half lives in CloudFront (`key_id` is the public key id that
    goes into a signed URL's `Key-Pair-Id`); the private half is the PEM in
    this row's KMS envelope, never in a graph, a log or a command's output.
    `FileManager`'s own secrets are deliberately NOT used: `MojoSecrets`
    derives its password from the row itself (`mojo/models/secrets.py`), so a
    database copy alone would decrypt it, and a CloudFront private key can
    sign a link to every object behind the distribution.

    `public_key_name` is deterministic (`mojo-fm-key-<pk>`) and is the
    CloudFront `CallerReference`, so provisioning that crashes between saving
    this row and creating the public key finds or recreates the same key on
    the next run instead of leaving a stray.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    key_id = models.CharField(
        max_length=64, null=True, blank=True, default=None, unique=True,
        help_text="CloudFront public key id (Key-Pair-Id). Null until the "
                  "public key exists in CloudFront.")
    public_key_name = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text="Deterministic CloudFront public key name and CallerReference.")
    is_active = models.BooleanField(default=True, db_index=True)
    retire_after = models.DateTimeField(
        null=True, blank=True, default=None, db_index=True,
        help_text="Set by rotation: when the last link signed with this key "
                  "has expired, the cron removes it from the key group.")

    class Meta:
        db_table = "fileman_cdn_signing_key"

    class RestMeta:
        # No URL is registered for this model: provisioning, rotation and
        # revocation drive it through the FileManager actions and the
        # fileman_cdn command. The graph exists for those reports only, and
        # it never carries the PEM.
        CAN_CREATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["manage_files", "files"]
        SAVE_PERMS = []  # written by provisioning and the fileman_cdn command only
        GRAPHS = {
            "default": {
                "fields": ["id", "created", "modified", "key_id", "public_key_name",
                           "is_active", "retire_after"],
                "extra": ["has_private_key"],
            },
            "basic": {
                "fields": ["id", "key_id", "is_active"],
                "extra": ["has_private_key"],
            },
        }

    def __str__(self):
        return f"CdnSigningKey {self.key_id or self.public_key_name or self.pk}"

    @property
    def has_private_key(self):
        """Presence only. The PEM itself never leaves `load_private_key`."""
        return bool(self.mojo_secrets)

    def set_private_key_pem(self, pem):
        """Store a validated RSA-2048 private key. Raises ValueError otherwise."""
        from mojo.apps.fileman.services import cdn
        cdn.load_private_key(pem)
        self.set_secret("private_key_pem", pem)

    def load_private_key(self):
        """The parsed private key, decrypted through KMS. Callers cache."""
        from mojo.apps.fileman.services import cdn
        pem = self.get_secret("private_key_pem")
        if not pem:
            raise ValueError("this signing key holds no private key")
        return cdn.load_private_key(pem)
