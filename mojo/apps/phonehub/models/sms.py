from django.db import models
from mojo.models import MojoModel


class SMS(models.Model, MojoModel):
    """
    Stores sent and received SMS messages.
    Tracks delivery status and provides audit trail.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", on_delete=models.CASCADE,
                           related_name="sms_messages", null=True, blank=True,
                           help_text="User associated with this SMS")
    group = models.ForeignKey("account.Group", on_delete=models.CASCADE,
                            related_name="sms_messages", null=True, blank=True,
                            help_text="Organization for this SMS")

    # Direction
    DIRECTION_CHOICES = [
        ('outbound', 'Outbound'),
        ('inbound', 'Inbound'),
    ]
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES,
                                db_index=True, help_text="Message direction")

    # Phone Numbers
    from_number = models.CharField(max_length=20, db_index=True,
                                  help_text="Sender phone number (E.164 format)")
    to_number = models.CharField(max_length=20, db_index=True,
                                help_text="Recipient phone number (E.164 format)")

    # Message Content
    body = models.TextField(help_text="SMS message body")

    # Status
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
        ('undelivered', 'Undelivered'),
        ('received', 'Received'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                            default='queued', db_index=True)

    # Provider Info
    provider = models.CharField(max_length=20, blank=True, null=True,
                              help_text="twilio or aws")
    provider_message_id = models.CharField(max_length=100, blank=True, null=True,
                                         db_index=True,
                                         help_text="Provider's message ID")

    # Error Tracking
    error_code = models.CharField(max_length=50, blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    # Metadata
    metadata = models.JSONField(default=dict, blank=True,
                               help_text="Additional metadata (webhooks, callbacks, etc.)")

    # Test Mode
    is_test = models.BooleanField(default=False, db_index=True,
                                 help_text="Whether this was sent in test mode")

    # Timestamps
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                  help_text="When message was sent to provider")
    delivered_at = models.DateTimeField(null=True, blank=True,
                                       help_text="When message was delivered to recipient")

    class Meta:
        ordering = ['-created']
        verbose_name = "SMS"
        verbose_name_plural = "SMS Messages"
        indexes = [
            models.Index(fields=['-created', 'direction']),
            models.Index(fields=['to_number', '-created']),
            models.Index(fields=['from_number', '-created']),
            models.Index(fields=['status', '-created']),
            models.Index(fields=['provider_message_id']),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_sms", "manage_sms", "owner"]
        SAVE_PERMS = ["manage_sms"]  # Creating SMS via API requires manage permission
        DELETE_PERMS = ["manage_sms"]
        SEARCH_FIELDS = ["to_number", "from_number", "body"]
        LIST_DEFAULT_FILTERS = {}
        GRAPHS = {
            "basic": {
                "fields": ["id", "direction", "from_number", "to_number",
                          "body", "status", "created"]
            },
            "default": {
                "fields": ["id", "direction", "from_number", "to_number",
                          "body", "status", "provider", "error_message",
                          "sent_at", "delivered_at", "created"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            },
            "full": {
                "exclude": [],
                "graphs": {
                    "user": "default",
                    "group": "default"
                }
            }
        }

    def __str__(self):
        return f"{self.get_direction_display()}: {self.from_number} -> {self.to_number} ({self.status})"

    @property
    def is_outbound(self):
        """Check if this is an outbound message."""
        return self.direction == 'outbound'

    @property
    def is_inbound(self):
        """Check if this is an inbound message."""
        return self.direction == 'inbound'

    @property
    def is_delivered(self):
        """Check if message was successfully delivered."""
        return self.status in ['delivered', 'received']

    @property
    def is_failed(self):
        """Check if message failed to send."""
        return self.status in ['failed', 'undelivered']

    def mark_sent(self, provider_message_id=None):
        """Mark message as sent."""
        from django.utils import timezone
        self.status = 'sent'
        self.sent_at = timezone.now()
        if provider_message_id:
            self.provider_message_id = provider_message_id
        self.save()

    def mark_delivered(self):
        """Mark message as delivered."""
        from django.utils import timezone
        self.status = 'delivered'
        self.delivered_at = timezone.now()
        self.save()

    def mark_failed(self, error_code=None, error_message=None):
        """Mark message as failed."""
        self.status = 'failed'
        if error_code:
            self.error_code = error_code
        if error_message:
            self.error_message = error_message
        self.save()

    @classmethod
    def send(cls, body, to_number, metadata=None, group=None, user=None):
        """Mark message as received."""
        from mojo.apps.phonehub.services.twilio import send_sms, FROM_NUMBER, PROVIDER
        from .phone import PhoneNumber
        to_number = PhoneNumber.normalize(to_number)
        sms = cls.objects.create(
            user=user,
            group=group,
            direction='outbound',
            from_number=FROM_NUMBER,
            to_number=to_number,
            body=body,
            status='queued',
            provider=PROVIDER,
            metadata=metadata or {},
        )
        resp = send_sms(body, to_number)
        if resp.sent:
            sms.mark_sent(resp.id)
        else:
            sms.mark_failed(error_code=resp.code, error_message=resp.error)
        sms.save()
