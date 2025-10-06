from django.db import models
from django.core.exceptions import ValidationError
from mojo.models import MojoModel
from typing import Optional, Union, Sequence, Dict, Any


class Mailbox(models.Model, MojoModel):
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

    is_system_default = models.BooleanField(
        default=False,
        db_index=True,
        help_text="System-wide default mailbox (only one allowed)"
    )

    is_domain_default = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Default mailbox for this domain (one per domain)"
    )

    class Meta:
        db_table = "aws_mailbox"
        indexes = [
            models.Index(fields=["modified"]),
            models.Index(fields=["email"]),
            models.Index(fields=["is_system_default"]),
            models.Index(fields=["is_domain_default", "domain"]),
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
                    "is_system_default",
                    "is_domain_default",
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
                    "is_system_default",
                    "is_domain_default",
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

    def on_rest_saved(self, changed_fields, created):
        """Handle default field uniqueness after REST save"""

        # Clear other system defaults if this was just set as system default
        if 'is_system_default' in changed_fields and self.is_system_default:
            Mailbox.objects.exclude(pk=self.pk).update(is_system_default=False)

        # Clear other domain defaults if this was just set as domain default
        if 'is_domain_default' in changed_fields and self.is_domain_default:
            Mailbox.objects.filter(domain=self.domain).exclude(pk=self.pk).update(is_domain_default=False)

        super().on_rest_saved(changed_fields, created)

    @classmethod
    def get_system_default(cls) -> Optional['Mailbox']:
        """Get the system-wide default mailbox"""
        return cls.objects.filter(is_system_default=True).first()

    @classmethod
    def get_domain_default(cls, domain: Union[str, 'EmailDomain']) -> Optional['Mailbox']:
        """Get the default mailbox for a specific domain

        Args:
            domain: Either a domain name string or EmailDomain instance
        """
        if isinstance(domain, str):
            return cls.objects.filter(domain__name__iexact=domain, is_domain_default=True).first()
        else:
            return cls.objects.filter(domain=domain, is_domain_default=True).first()

    @classmethod
    def get_default(cls, domain: Optional[Union[str, 'EmailDomain']] = None, prefer_domain: bool = True) -> Optional['Mailbox']:
        """Smart default: try domain default first (if domain provided), then fall back to system default

        Args:
            domain: Optional domain to look for domain-specific default
            prefer_domain: If True (default), prefer domain default over system default
        """
        if domain and prefer_domain:
            domain_default = cls.get_domain_default(domain)
            if domain_default:
                return domain_default

        return cls.get_system_default()

    def send_email(
        self,
        to: Union[str, Sequence[str]],
        subject: Optional[str] = None,
        body_text: Optional[str] = None,
        body_html: Optional[str] = None,
        cc: Optional[Union[str, Sequence[str]]] = None,
        bcc: Optional[Union[str, Sequence[str]]] = None,
        reply_to: Optional[Union[str, Sequence[str]]] = None,
        **kwargs
    ) -> 'SentMessage':
        """Send plain email from this mailbox

        Args:
            to: One or more recipient addresses
            subject: Email subject
            body_text: Optional plain text body
            body_html: Optional HTML body
            cc, bcc, reply_to: Optional addressing
            **kwargs: Additional arguments passed to email service (allow_unverified, aws_access_key, etc.)

        Returns:
            SentMessage instance

        Raises:
            OutboundNotAllowed: If this mailbox has allow_outbound=False
        """
        from mojo.apps.aws.services import email as email_service

        if not self.allow_outbound:
            raise email_service.OutboundNotAllowed(f"Outbound sending is disabled for mailbox {self.email}")

        aws_access_key = self.domain.aws_key
        aws_secret_key = self.domain.aws_secret
        aws_region = self.domain.aws_region

        return email_service.send_email(
            from_email=self.email,
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            region=aws_region,
            **kwargs
        )

    def send_template_email(
        self,
        to: Union[str, Sequence[str]],
        template_name: str,
        context: Optional[Dict[str, Any]] = None,
        cc: Optional[Union[str, Sequence[str]]] = None,
        bcc: Optional[Union[str, Sequence[str]]] = None,
        reply_to: Optional[Union[str, Sequence[str]]] = None,
        **kwargs
    ) -> 'SentMessage':
        """Send email using DB EmailTemplate

        Args:
            to: One or more recipient addresses
            template_name: Name of the EmailTemplate in database
            context: Template context variables
            cc, bcc, reply_to: Optional addressing
            **kwargs: Additional arguments passed to email service (allow_unverified, aws_access_key, etc.)

        Returns:
            SentMessage instance

        Raises:
            OutboundNotAllowed: If this mailbox has allow_outbound=False
            ValueError: If template not found

        Note:
            Automatically checks for domain-specific template overrides.
            If "{domain.name}.{template_name}" exists, it will be used instead of the base template.
        """
        from mojo.apps.aws.services import email as email_service
        from mojo.apps.aws.models import EmailTemplate

        if not self.allow_outbound:
            raise email_service.OutboundNotAllowed(f"Outbound sending is disabled for mailbox {self.email}")

        # Check for domain-specific template override
        final_template_name = template_name
        if self.domain and self.domain.name:
            domain_template_name = f"{self.domain.name}.{template_name}"
            # Check if domain-specific template exists
            if EmailTemplate.objects.filter(name=domain_template_name).exists():
                final_template_name = domain_template_name

        aws_access_key = self.domain.aws_key
        aws_secret_key = self.domain.aws_secret
        aws_region = self.domain.aws_region

        return email_service.send_with_template(
            from_email=self.email,
            to=to,
            template_name=final_template_name,
            context=context,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            region=aws_region,
            **kwargs
        )
