from django.db import models

from mojo.models import MojoModel


class UploadInitiation(models.Model, MojoModel):
    """Internal retry record for the initiated-upload endpoint.

    This model intentionally has no RestMeta: idempotency material is an
    implementation detail and must never be exposed through generic REST.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    actor = models.ForeignKey(
        "account.User",
        related_name="file_upload_initiations",
        on_delete=models.CASCADE,
    )
    file = models.OneToOneField(
        "fileman.File",
        related_name="upload_initiation",
        on_delete=models.CASCADE,
    )
    key_digest = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["actor", "key_digest"],
                name="fileman_upload_actor_key_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["actor", "created"]),
        ]
