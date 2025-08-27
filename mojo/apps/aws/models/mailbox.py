from django.db import models
from django.core.exceptions import ValidationError
from mojo.models import MojoModel


class Mailbox(MojoModel):
    """
    Mailbox

    Minimal model representing a single email address (mailbox) within a verified EmailDomain.
    Sending and receiving policies are configured per mailbox. When inbound messages arrive
    (domain-level catch-all), they are routed to the matching mailbox by recipient address and
    optionally dispatched to an async handler.

    Notes:
    - `email` is the full email address (e.g., support@example.com) and is unique.
    - `domain` references the owning EmailDomain (e.g., example.com).
    - `allow_inbound` and `allow_outbound` control behavior for this mailbox.
    - `async_handler` is a dotted path "package.module:function" used by the Tasks system.
    - `metadata` allows flexible extension without schema churn.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    domain = models.ForeignKey(
        "EmailDomain",
        related_name="mailboxes",
        on_delete=models.CASCADE,
        help_text="Owning email domain (SES identity)"
    )

    email = models.EmailField(
        unique=True,
        db_index=True,
        help_text="Full email address for this mailbox (e.g., support@example.com)"
    )

    allow_inbound = models.BooleanField(
        default=True,
        help_text="If true, inbound messages addressed to this mailbox will be processed"
    )
    allow_outbound = models.BooleanField(
        default=True,
        help_text="If true, outbound messages can be sent from this mailbox"
    )

    async_handler = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Dotted path to async handler: 'package.module:function'"
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "aws_mailbox"
        indexes = [
            models.Index(fields=["modified"]),
            models.Index(fields=["email"]),
        ]
        ordering = ["email"]

    class RestMeta:
        VIEW_PERMS = ["manage_aws"]
        SAVE_PERMS = ["manage_aws"]
        DELETE_PERMS = ["manage_aws"]
        SEARCH_FIELDS = ["email"]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id",
                    "email",
                    "domain",
                    "allow_inbound",
                    "allow_outbound",
                ]
            },
            "default": {
                "fields": [
                    "id",
                    "email",
                    "domain",
                    "allow_inbound",
                    "allow_outbound",
                    "async_handler",
                    "metadata",
                    "created",
                    "modified",
                ],
                "graphs": {
                    "domain": "basic"
                }
            },
        }

    def __str__(self) -> str:
        return self.email

    def clean(self):
        """
        Ensure the mailbox email belongs to the associated domain (simple sanity check).
        """
        super().clean()
        if self.domain and self.email:
            domain_name = f"@{self.domain.name.lower()}"
            if not self.email.lower().endswith(domain_name):
                raise ValidationError(
                    {"email": f"Email must belong to domain '{self.domain.name}'"}
                )
