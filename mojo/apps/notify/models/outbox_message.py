from django.db import models
from mojo.models import MojoModel


class OutboxMessage(models.Model, MojoModel):
    """
    Message to be sent or already sent through an outbox
    """
    
    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_notify"]
        SEARCH_FIELDS = ["to_address", "from_address", "subject", "message"]
        SEARCH_TERMS = [
            "to_address", "from_address", "subject", "status",
            ("outbox", "outbox__address"),
            ("account", "outbox__account__domain"),
            ("account_kind", "outbox__account__kind"),
            ("user", "user__username"),
            ("group", "group__name")]

        GRAPHS = {
            "default": {
                "graphs": {
                    "outbox": "basic",
                    "user": "basic",
                    "group": "basic"
                }
            },
            "list": {
                "graphs": {
                    "outbox": "basic",
                    "user": "basic",
                    "group": "basic"
                }
            }
        }

    # Message status choices
    PENDING = 'pending'
    SENDING = 'sending'
    SENT = 'sent'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    
    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (SENDING, 'Sending'),
        (SENT, 'Sent'),
        (FAILED, 'Failed'),
        (CANCELLED, 'Cancelled'),
    ]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    
    outbox = models.ForeignKey(
        "notify.Outbox",
        related_name="messages",
        on_delete=models.CASCADE,
        help_text="Outbox that will send this message"
    )
    
    user = models.ForeignKey(
        "account.User",
        related_name="outbox_messages",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="User associated with this message (if applicable)"
    )
    
    group = models.ForeignKey(
        "account.Group",
        related_name="outbox_messages",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group associated with this message (if applicable)"
    )
    
    to_address = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Recipient address (e.g., joe@example.com, +1234567890)"
    )
    
    from_address = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Sender address (e.g., outbox@example.com, +0987654321)"
    )
    
    subject = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        default=None,
        help_text="Message subject (for email, etc.)"
    )
    
    message = models.TextField(
        help_text="Message content/body"
    )
    
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional message metadata (delivery options, attachments, etc.)"
    )
    
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
        help_text="Current status of the message"
    )
    
    scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="When this message should be sent (null for immediate)"
    )
    
    sent_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text="When this message was actually sent"
    )
    
    failed_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text="When this message failed to send"
    )
    
    error_message = models.TextField(
        null=True,
        blank=True,
        default=None,
        help_text="Error message if sending failed"
    )
    
    message_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="External message ID from the service provider"
    )
    
    retry_count = models.IntegerField(
        default=0,
        help_text="Number of times sending has been attempted"
    )
    
    max_retries = models.IntegerField(
        default=3,
        help_text="Maximum number of retry attempts"
    )

    class Meta:
        indexes = [
            models.Index(fields=['outbox', 'status', 'created']),
            models.Index(fields=['status', 'scheduled_at']),
            models.Index(fields=['to_address', 'created']),
            models.Index(fields=['from_address', 'created']),
            models.Index(fields=['user', 'created']),
            models.Index(fields=['group', 'created']),
            models.Index(fields=['sent_at']),
        ]

    def __str__(self):
        subject_preview = f" - {self.subject[:50]}..." if self.subject else ""
        return f"{self.outbox.account.get_kind_display()} message: {self.from_address} → {self.to_address}{subject_preview} ({self.get_status_display()})"

    def get_metadata_value(self, key, default=None):
        """Get a specific metadata value"""
        return self.metadata.get(key, default)

    def set_metadata_value(self, key, value):
        """Set a specific metadata value"""
        self.metadata[key] = value

    @property
    def account(self):
        """Get the account this message will be sent through"""
        return self.outbox.account if self.outbox else None

    @property
    def message_kind(self):
        """Get the kind of message (email, sms, etc.)"""
        return self.account.kind if self.account else None

    @property
    def message_preview(self):
        """Get a preview of the message content"""
        if not self.message:
            return ""
        return self.message[:200] + "..." if len(self.message) > 200 else self.message

    @property
    def is_pending(self):
        return self.status == self.PENDING

    @property
    def is_sending(self):
        return self.status == self.SENDING

    @property
    def is_sent(self):
        return self.status == self.SENT

    @property
    def is_failed(self):
        return self.status == self.FAILED

    @property
    def is_cancelled(self):
        return self.status == self.CANCELLED

    @property
    def can_retry(self):
        """Check if this message can be retried"""
        return self.is_failed and self.retry_count < self.max_retries

    @property
    def is_ready_to_send(self):
        """Check if this message is ready to be sent"""
        if not self.is_pending:
            return False
        
        if self.scheduled_at:
            from django.utils import timezone
            return timezone.now() >= self.scheduled_at
        
        return True

    def mark_sending(self):
        """Mark this message as currently being sent"""
        self.status = self.SENDING
        self.save(update_fields=['status'])

    def mark_sent(self, message_id=None):
        """Mark this message as successfully sent"""
        from django.utils import timezone
        self.status = self.SENT
        self.sent_at = timezone.now()
        if message_id:
            self.message_id = message_id
        self.save(update_fields=['status', 'sent_at', 'message_id'])

    def mark_failed(self, error_message=None):
        """Mark this message as failed to send"""
        from django.utils import timezone
        self.status = self.FAILED
        self.failed_at = timezone.now()
        self.retry_count += 1
        if error_message:
            self.error_message = error_message
        self.save(update_fields=['status', 'failed_at', 'retry_count', 'error_message'])

    def mark_cancelled(self):
        """Mark this message as cancelled"""
        self.status = self.CANCELLED
        self.save(update_fields=['status'])

    def reset_for_retry(self):
        """Reset message status for retry"""
        if self.can_retry:
            self.status = self.PENDING
            self.save(update_fields=['status'])

    def is_to_user(self, user):
        """Check if this message is to a specific user"""
        if not user or not user.email:
            return False
        return self.to_address.lower() == user.email.lower()