import uuid
import mimetypes
from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit


class VaultFile(models.Model, MojoModel):
    """Encrypted file stored in S3 via FileManager."""

    class RestMeta:
        CAN_SAVE = True
        CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["view_vault", "manage_vault", "owner"]
        SAVE_PERMS = ["manage_vault", "owner"]
        DELETE_PERMS = ["manage_vault", "owner"]
        SEARCH_FIELDS = ["name", "content_type", "description"]
        SEARCH_TERMS = [
            "name", "content_type",
            ("group", "group__name")]
        NO_SAVE_FIELDS = [
            "id", "pk", "ekey", "uuid", "chunk_count",
            "hashed_password", "is_encrypted"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified", "name", "content_type",
                    "description", "size", "is_encrypted", "metadata"
                ],
                "extra": ["requires_password"],
                "graphs": {
                    "user": "basic",
                    "unlocked_by": "basic",
                    "group": "basic"
                }
            },
            "basic": {
                "fields": [
                    "id", "name", "content_type", "size", "is_encrypted"
                ],
                "extra": ["requires_password"]
            },
            "list": {
                "fields": [
                    "id", "created", "name", "content_type", "size",
                    "is_encrypted"
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
        related_name="vault_files",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL)

    group = models.ForeignKey(
        "account.Group",
        related_name="vault_files",
        on_delete=models.CASCADE)

    uuid = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=128)
    description = models.TextField(blank=True, null=True, default=None)
    size = models.BigIntegerField(default=0)
    chunk_count = models.IntegerField(default=0)
    is_encrypted = models.IntegerField(default=2)  # 0=plaintext, 2=AES-256-GCM
    ekey = models.TextField()
    hashed_password = models.TextField(blank=True, null=True, default=None)
    metadata = models.JSONField(default=dict, blank=True)

    unlocked_by = models.ForeignKey(
        "account.User",
        related_name="vault_unlocked_files",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL)

    class Meta:
        indexes = [
            models.Index(fields=["group", "created"]),
            models.Index(fields=["user", "created"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.uuid[:8]})"

    @property
    def requires_password(self):
        return self.hashed_password is not None

    def generate_uuid(self):
        self.uuid = uuid.uuid4().hex

    def on_rest_pre_save(self, changed_fields, created):
        if created:
            if not self.uuid:
                self.generate_uuid()
            if not self.content_type and self.name:
                self.content_type = mimetypes.guess_type(self.name)[0] or "application/octet-stream"

    def on_rest_pre_delete(self):
        """Delete the S3 object when the DB record is deleted."""
        try:
            from mojo.apps.filevault.services.vault import delete_s3_object
            delete_s3_object(self)
        except Exception:
            logit.error(f"filevault: failed to delete S3 object for VaultFile {self.pk}")
