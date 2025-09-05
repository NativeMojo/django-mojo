from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers.settings import settings
from mojo.helpers.aws.ses_domain import audit_domain_config, reconcile_domain_config
from mojo.helpers import aws


class EmailDomain(MojoSecrets, MojoModel):
    """
    EmailDomain

    Minimal model for managing an SES-backed email domain configuration in MOJO.

    Notes:
    - 'name' is the domain (e.g., example.com).
    - 'region' defaults to project AWS region (settings.AWS_REGION) or 'us-east-1'.
    - 'receiving_enabled' toggles domain-level catch-all receiving (SES receipt rules).
    - 's3_inbound_bucket' and 's3_inbound_prefix' identify where inbound emails are stored.
    - 'status' is a lightweight lifecycle indicator: pending | verified | error (free-form for now).
    - 'metadata' allows flexible per-domain extension without schema churn.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    name = models.CharField(max_length=255, unique=True, db_index=True)
    region = models.CharField(
        max_length=64,
        default=getattr(settings, 'AWS_REGION', 'us-east-1'),
        help_text="AWS region for SES operations"
    )

    status = models.CharField(
        max_length=32,
        default='pending',
        db_index=True,
        help_text='Domain status: "pending" (created), "ready" (audit passed), or "missing" (audit failed)'
    )

    receiving_enabled = models.BooleanField(
        default=False,
        help_text="When true, domain-level catch-all receiving is enabled via SES receipt rules"
    )

    s3_inbound_bucket = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="S3 bucket for inbound emails (required if receiving_enabled)"
    )
    s3_inbound_prefix = models.CharField(
        max_length=255,
        default='',
        blank=True,
        help_text="S3 prefix for inbound emails (e.g., inbound/example.com/)"
    )

    dns_mode = models.CharField(
        max_length=32,
        default='manual',
        help_text="DNS automation mode: manual | route53 | godaddy"
    )

    sns_topic_bounce_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="SNS topic ARN for SES bounce notifications"
    )
    sns_topic_complaint_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="SNS topic ARN for SES complaint notifications"
    )
    sns_topic_delivery_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="SNS topic ARN for SES delivery notifications"
    )
    sns_topic_inbound_arn = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="SNS topic ARN for SES inbound notifications"
    )

    metadata = models.JSONField(default=dict, blank=True)

    # Computed readiness flags updated by audit runs
    can_send = models.BooleanField(
        default=False,
        help_text="True if outbound sending is ready per last audit"
    )
    can_recv = models.BooleanField(
        default=False,
        help_text="True if inbound receiving is ready per last audit"
    )

    class Meta:
        db_table = "aws_email_domain"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["modified"]),
        ]
        ordering = ["name"]

    class RestMeta:
        VIEW_PERMS = ["manage_aws"]
        SAVE_PERMS = ["manage_aws"]
        DELETE_PERMS = ["manage_aws"]
        SEARCH_FIELDS = ["name", "region", "status"]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id",
                    "name",
                    "region",
                    "status",
                    "receiving_enabled",
                ]
            },
            "default": {
                "fields": [
                    "id",
                    "name",
                    "region",
                    "status",
                    "receiving_enabled",
                    "s3_inbound_bucket",
                    "s3_inbound_prefix",
                    "dns_mode",
                    "sns_topic_bounce_arn",
                    "sns_topic_complaint_arn",
                    "sns_topic_delivery_arn",
                    "sns_topic_inbound_arn",
                    "metadata",
                    "created",
                    "modified",
                ],
                "extra": [
                    "aws_key",
                    "aws_secret_masked"
                ]
            },
        }

    @property
    def aws_key(self):
        return self.get_secret('aws_key')

    @property
    def aws_secret(self):
        return self.get_secret('aws_secret')

    @property
    def aws_secret_masked(self):
        secret = self.get_secret('aws_secret', '')
        if len(secret) > 4:
            return '*' * (len(secret) - 4) + secret[-4:]
        return secret


    @property
    def aws_region(self):
        return self.region or getattr(settings, 'AWS_REGION', 'us-east-1')

    @property
    def is_verified(self):
        return self.status in ["verified", "ready"]

    def set_aws_key(self, key):
        self.set_secret('aws_key', key)

    def set_aws_secret(self, secret):
        self.set_secret('aws_secret', secret)

    def on_rest_created(self):
        """
        Automatically audit and reconcile SES/SNS configuration after this domain is created.
        This keeps AWS-side resources aligned without requiring a separate call.
        """
        try:
            region = self.aws_region
            # Audit current state (best-effort; ignore failures)
            try:
                desired_receiving = None
                if self.receiving_enabled and self.s3_inbound_bucket:
                    desired_receiving = {
                        'bucket': self.s3_inbound_bucket,
                        'prefix': self.s3_inbound_prefix or '',
                        'rule_set': 'mojo-default-receiving',
                        'rule_name': f'mojo-{self.name}-catchall',
                    }
                audit_domain_config(
                    domain=self.name,
                    region=region,
                    desired_receiving=desired_receiving,
                )
            except Exception:
                # Non-fatal: continue with reconcile
                pass

            # Reconcile (idempotent): ensure topics/mappings and catch-all receipt rule if enabled
            reconcile_domain_config(
                domain=self.name,
                region=region,
                receiving_enabled=self.receiving_enabled,
                s3_bucket=self.s3_inbound_bucket,
                s3_prefix=self.s3_inbound_prefix or '',
            )
        except Exception:
            # Swallow exceptions to avoid failing the create call; details can be inspected via /audit
            pass

    def __str__(self) -> str:
        return self.name
