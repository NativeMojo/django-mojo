from django.db import models
from mojo.models import MojoModel


class IncomingEmail(models.Model, MojoModel):
    """
    IncomingEmail

    Represents a single inbound email received via SES and stored in S3.
    Raw MIME is stored in S3 (s3_object_url). Parsed metadata and content are
    stored in this model. Attachments are stored in the same inbound S3 bucket
    and represented in a separate model (EmailAttachment, added later).

    Routing:
    - This model may be associated to a Mailbox if any recipient matches.
    - An async handler (on the Mailbox) can process the message after creation.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    mailbox = models.ForeignKey(
        "aws.Mailbox",
        null=True,
        blank=True,
        related_name="incoming_emails",
        on_delete=models.CASCADE,
        help_text="Associated mailbox if any recipient matches"
    )

    # Storage and identity
    s3_object_url = models.CharField(
        max_length=512,
        help_text="S3 URL for the raw MIME message (e.g., s3://bucket/key)"
    )
    message_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        help_text="SMTP Message-ID header (if present)"
    )

    # Headers and addressing
    from_address = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Raw From header address (may include name)"
    )
    to_addresses = models.JSONField(
        default=list,
        blank=True,
        help_text="List of recipient addresses from To header"
    )
    cc_addresses = models.JSONField(
        default=list,
        blank=True,
        help_text="List of recipient addresses from Cc header"
    )
    subject = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Email subject"
    )
    date_header = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Parsed Date header from the message"
    )
    headers = models.JSONField(
        default=dict,
        blank=True,
        help_text="All headers as a JSON object (flattened)"
    )

    # Content
    text_body = models.TextField(
        null=True,
        blank=True,
        help_text="Extracted plain text body (if available)"
    )
    html_body = models.TextField(
        null=True,
        blank=True,
        help_text="Extracted HTML body (if available)"
    )

    # Misc
    size_bytes = models.IntegerField(
        default=0,
        help_text="Approximate size of the raw message in bytes"
    )
    received_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Time message was received (from SNS/S3 event or set by parser)"
    )

    # Processing status
    processed = models.BooleanField(
        default=False,
        help_text="True if post-receive processing completed"
    )
    process_status = models.CharField(
        max_length=32,
        default="pending",
        db_index=True,
        help_text="Processing status: pending | success | error"
    )
    process_error = models.TextField(
        null=True,
        blank=True,
        help_text="Error details if processing failed"
    )

    class Meta:
        db_table = "aws_incoming_email"
        indexes = [
            models.Index(fields=["modified"]),
            models.Index(fields=["received_at"]),
            models.Index(fields=["message_id"]),
        ]
        ordering = ["-received_at", "-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_aws"]
        SAVE_PERMS = ["manage_aws"]
        DELETE_PERMS = ["manage_aws"]
        SEARCH_FIELDS = ["subject", "from_address", "message_id"]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id",
                    "mailbox",
                    "subject",
                    "from_address",
                    "to_addresses",
                    "received_at",
                    "processed",
                    "process_status",
                    "created",
                ],
                "graphs": {"mailbox": "basic"}
            },
            "default": {
                "fields": [
                    "id",
                    "mailbox",
                    "s3_object_url",
                    "message_id",
                    "from_address",
                    "to_addresses",
                    "cc_addresses",
                    "subject",
                    "date_header",
                    "headers",
                    "size_bytes",
                    "received_at",
                    "processed",
                    "process_status",
                    "process_error",
                    "created",
                    "modified",
                ],
                "graphs": {"mailbox": "basic"}
            },
            "full": {
                "fields": [
                    "id",
                    "mailbox",
                    "s3_object_url",
                    "message_id",
                    "from_address",
                    "to_addresses",
                    "cc_addresses",
                    "subject",
                    "date_header",
                    "headers",
                    "text_body",
                    "html_body",
                    "size_bytes",
                    "received_at",
                    "processed",
                    "process_status",
                    "process_error",
                    "created",
                    "modified",
                ],
                "graphs": {"mailbox": "basic"}
            },
        }

    def __str__(self) -> str:
        return self.subject or self.message_id or f"IncomingEmail {self.pk}"
