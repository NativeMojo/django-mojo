from django.db import models
from mojo.models import MojoModel


class Outbox(models.Model, MojoModel):
    """
    Outbox for sending messages through various notification services
    """
    
    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_notify"]
        SEARCH_FIELDS = ["address"]
        SEARCH_TERMS = [
            "address", 
            ("account", "account__domain"),
            ("account_kind", "account__kind"),
            ("group", "group__name")]

        GRAPHS = {
            "default": {
                "graphs": {
                    "account": "basic",
                    "group": "basic"
                }
            },
            "list": {
                "graphs": {
                    "account": "basic",
                    "group": "basic"
                }
            }
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    
    account = models.ForeignKey(
        "notify.Account",
        related_name="outboxes",
        on_delete=models.CASCADE,
        help_text="Notification account this outbox belongs to"
    )
    
    group = models.ForeignKey(
        "account.Group",
        related_name="outboxes",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group that owns this outbox"
    )
    
    address = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Outbox address (e.g., outbox@example.com, +1234567890)"
    )
    
    handler = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        default=None,
        help_text="Python path to handler function (e.g., app.outbox_handler.on_message_sent)"
    )
    
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this outbox is active and can send messages"
    )
    
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Outbox-specific configuration settings"
    )
    
    rate_limit = models.IntegerField(
        null=True,
        blank=True,
        default=None,
        help_text="Max messages per hour (null for no limit)"
    )

    class Meta:
        unique_together = ['account', 'address']
        indexes = [
            models.Index(fields=['account', 'address']),
            models.Index(fields=['group', 'is_active']),
            models.Index(fields=['address', 'is_active']),
        ]

    def __str__(self):
        group_name = self.group.name if self.group else "No Group"
        return f"{self.account.get_kind_display()} outbox: {self.address} ({group_name})"

    def get_setting(self, key, default=None):
        """Get a specific setting value"""
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        """Set a specific setting value"""
        self.settings[key] = value

    def can_send_messages(self):
        """Check if this outbox can send messages"""
        return self.is_active and self.account.is_active

    def check_rate_limit(self):
        """Check if outbox is within rate limits"""
        if not self.rate_limit:
            return True
            
        from django.utils import timezone
        from datetime import timedelta
        
        one_hour_ago = timezone.now() - timedelta(hours=1)
        recent_messages = self.messages.filter(created__gte=one_hour_ago).count()
        
        return recent_messages < self.rate_limit

    @property
    def message_kind(self):
        """Get the kind of messages this outbox sends"""
        return self.account.kind if self.account else None