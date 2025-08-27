from django.db import models
from mojo.models import MojoModel


class EmailAttachment(models.Model, MojoModel):
    """
    EmailAttachment

    Represents a single attachment extracted from an incoming email. The binary
    content is stored in the same inbound S3 bucket as the raw message, and
    referenced by `stored_as` (e.g., s3://bucket/key).

    Relationships:
    - incoming_email: FK to IncomingEmail
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    incoming_email = models.ForeignKey(
        "aws.IncomingEmail",
        related_name="attachments",
        on_delete=models.CASCADE,
        help_text="The inbound email this attachment belongs to"
    )

    filename = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Original filename (if provided by the sender)"
    )

    content_type = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="MIME content type (e.g., application/pdf)"
    )

    size_bytes = models.IntegerField(
        default=0,
        help_text="Size of the stored attachment in bytes (approximate)"
    )

    stored_as = models.CharField(
        max_length=512,
        help_text="Storage reference (e.g., s3://bucket/key)"
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary metadata (e.g., content-id, part headers)"
    )

    class Meta:
        db_table = "aws_email_attachment"
        indexes = [
            models.Index(fields=["modified"]),
            models.Index(fields=["filename"]),
        ]
        ordering = ["-created", "id"]

    class RestMeta:
        VIEW_PERMS = ["manage_aws"]
        SAVE_PERMS = ["manage_aws"]
        DELETE_PERMS = ["manage_aws"]
        SEARCH_FIELDS = ["filename", "content_type", "stored_as"]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id",
                    "incoming_email",
                    "filename",
                    "content_type",
                    "size_bytes",
                    "created",
                ],
                "graphs": {"incoming_email": "basic"},
            },
            "default": {
                "fields": [
                    "id",
                    "incoming_email",
                    "filename",
                    "content_type",
                    "size_bytes",
                    "stored_as",
                    "metadata",
                    "created",
                    "modified",
                ],
                "graphs": {"incoming_email": "basic"},
            },
        }

    def __str__(self) -> str:
        return self.filename or self.stored_as or f"Attachment {self.pk}"
