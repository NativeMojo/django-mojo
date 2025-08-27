from django.db import models
from mojo.models import MojoModel


class SentMessage(models.Model, MojoModel):
    """
    SentMessage

    Represents an outbound email sent via AWS SES using a specific Mailbox (email address).
    Tracks SES MessageId and delivery lifecycle (delivery, bounce, complaint) updated via SNS webhooks.

    Notes:
    - `mailbox` identifies the sending address; sending is only allowed when mailbox.allow_outbound is True.
    - `ses_message_id` is populated after a successful SES send API call.
    - `to_addresses`, `cc_addresses`, `bcc_addresses` are stored as JSON arrays.
    - `template_name` and `template_context` support simple templated sending (EmailTemplate model can be added later).
    - `status` reflects the current delivery state; `status_reason` stores detailed info (bounce/complaint payloads, errors).
    """

    STATUS_QUEUED = "queued"
    STATUS_SENDING = "sending"
    STATUS_DELIVERED = "delivered"
    STATUS_BOUNCED = "bounced"
    STATUS_COMPLAINED = "complained"
    STATUS_FAILED = "failed"
    STATUS_UNKNOWN = "unknown"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_SENDING, "Sending"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_BOUNCED, "Bounced"),
        (STATUS_COMPLAINED, "Complained"),
        (STATUS_FAILED, "Failed"),
        (STATUS_UNKNOWN, "Unknown"),
    ]

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    mailbox = models.ForeignKey(
        "aws.Mailbox",
        related_name="sent_messages",
        on_delete=models.CASCADE,
        help_text="Mailbox used as the sender (envelope MAIL FROM = mailbox.email)"
    )

    ses_message_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        help_text="AWS SES MessageId returned after a successful send"
    )

    # Recipients
    to_addresses = models.JSONField(
        default=list,
        blank=True,
        help_text="List of recipient addresses (To)"
    )
    cc_addresses = models.JSONField(
        default=list,
        blank=True,
        help_text="List of recipient addresses (Cc)"
    )
    bcc_addresses = models.JSONField(
        default=list,
        blank=True,
        help_text="List of recipient addresses (Bcc)"
    )

    # Content
    subject = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Email subject"
    )
    body_text = models.TextField(
        null=True,
        blank=True,
        help_text="Plain text body"
    )
    body_html = models.TextField(
        null=True,
        blank=True,
        help_text="HTML body"
    )

    # Template support (simple; FK can be added later)
    template_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Optional EmailTemplate name used to render this message"
    )
    template_context = models.JSONField(
        default=dict,
        blank=True,
        help_text="Context used when rendering a template"
    )

    # Delivery status
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_QUEUED,
        db_index=True,
        help_text="Current delivery status"
    )
    status_reason = models.TextField(
        null=True,
        blank=True,
        help_text="Details or raw payload for bounces/complaints/errors"
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary metadata for downstream processing/auditing"
    )

    class Meta:
        db_table = "aws_sent_message"
        indexes = [
            models.Index(fields=["modified"]),
            models.Index(fields=["status"]),
            models.Index(fields=["ses_message_id"]),
        ]
        ordering = ["-created", "id"]

    class RestMeta:
        VIEW_PERMS = ["manage_aws"]
        SAVE_PERMS = ["manage_aws"]
        DELETE_PERMS = ["manage_aws"]
        SEARCH_FIELDS = ["subject", "ses_message_id"]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id",
                    "mailbox",
                    "ses_message_id",
                    "subject",
                    "to_addresses",
                    "status",
                    "created",
                ],
                "graphs": {"mailbox": "basic"}
            },
            "default": {
                "fields": [
                    "id",
                    "mailbox",
                    "ses_message_id",
                    "to_addresses",
                    "cc_addresses",
                    "bcc_addresses",
                    "subject",
                    "body_text",
                    "body_html",
                    "template_name",
                    "template_context",
                    "status",
                    "status_reason",
                    "metadata",
                    "created",
                    "modified",
                ],
                "graphs": {"mailbox": "basic"}
            },
        }

    def __str__(self) -> str:
        return self.subject or self.ses_message_id or f"SentMessage {self.pk}"
