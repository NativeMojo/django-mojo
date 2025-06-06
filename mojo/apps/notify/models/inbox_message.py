from django.db import models
from mojo.models import MojoModel


class InboxMessage(models.Model, MojoModel):
    """
    Message received in an inbox from various notification services
    """
    
    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_notify"]
        SEARCH_FIELDS = ["to_address", "from_address", "subject", "message"]
        SEARCH_TERMS = [
            "to_address", "from_address", "subject",
            ("inbox", "inbox__address"),
            ("account", "inbox__account__domain"),
            ("account_kind", "inbox__account__kind"),
            ("user", "user__username"),
            ("group", "group__name")]

        GRAPHS = {
            "default": {
                "graphs": {
                    "inbox": "basic",
                    "user": "basic",
                    "group": "basic"
                }
            },
            "list": {
                "graphs": {
                    "inbox": "basic",
                    "user": "basic",
                    "group": "basic"
                }
            }
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    
    inbox = models.ForeignKey(
        "notify.Inbox",
        related_name="messages",
        on_delete=models.CASCADE,
        help_text="Inbox that received this message"
    )
    
    user = models.ForeignKey(
        "account.User",
        related_name="inbox_messages",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="User associated with this message (if applicable)"
    )
    
    group = models.ForeignKey(
        "account.Group",
        related_name="inbox_messages",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group associated with this message (if applicable)"
    )
    
    to_address = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Recipient address (e.g., inbox@example.com, +1234567890)"
    )
    
    from_address = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Sender address (e.g., sender@example.com, +0987654321)"
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
        help_text="Additional message metadata (headers, delivery info, etc.)"
    )
    
    processed = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether this message has been processed by handlers"
    )
    
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text="When this message was processed"
    )
    
    message_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="External message ID from the service provider"
    )

    class Meta:
        indexes = [
            models.Index(fields=['inbox', 'created']),
            models.Index(fields=['from_address', 'created']),
            models.Index(fields=['to_address', 'created']),
            models.Index(fields=['processed', 'created']),
            models.Index(fields=['user', 'created']),
            models.Index(fields=['group', 'created']),
        ]

    def __str__(self):
        subject_preview = f" - {self.subject[:50]}..." if self.subject else ""
        return f"{self.inbox.account.get_kind_display()} message: {self.from_address} → {self.to_address}{subject_preview}"

    def get_metadata_value(self, key, default=None):
        """Get a specific metadata value"""
        return self.metadata.get(key, default)

    def set_metadata_value(self, key, value):
        """Set a specific metadata value"""
        self.metadata[key] = value

    @property
    def account(self):
        """Get the account this message was received through"""
        return self.inbox.account if self.inbox else None

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

    def mark_processed(self):
        """Mark this message as processed"""
        from django.utils import timezone
        self.processed = True
        self.processed_at = timezone.now()
        self.save(update_fields=['processed', 'processed_at'])

    def is_from_user(self, user):
        """Check if this message is from a specific user"""
        if not user or not user.email:
            return False
        return self.from_address.lower() == user.email.lower()