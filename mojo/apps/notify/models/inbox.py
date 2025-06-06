from django.db import models
from mojo.models import MojoModel


class Inbox(models.Model, MojoModel):
    """
    Inbox for receiving messages from various notification services
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
            ("group", "account__group__name")]

        GRAPHS = {
            "default": {
                "graphs": {
                    "account": "basic"
                }
            },
            "list": {
                "graphs": {
                    "account": "basic"
                }
            }
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    
    account = models.ForeignKey(
        "notify.Account",
        related_name="inboxes",
        on_delete=models.CASCADE,
        help_text="Notification account this inbox belongs to"
    )
    
    address = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Inbox address (e.g., support@example.com, +1234567890)"
    )
    
    async_handler = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        default=None,
        help_text="Python path to async handler function (e.g., app.inbox_handler.on_message_received)"
    )
    
    sync_handler = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        default=None,
        help_text="Python path to sync handler function (e.g., app.inbox_handler.on_message_received)"
    )
    
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this inbox is active and can receive messages"
    )
    
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Inbox-specific configuration settings"
    )

    class Meta:
        unique_together = ['account', 'address']
        indexes = [
            models.Index(fields=['account', 'address']),
            models.Index(fields=['address', 'is_active']),
        ]

    def __str__(self):
        return f"{self.account.get_kind_display()} inbox: {self.address}"

    def get_setting(self, key, default=None):
        """Get a specific setting value"""
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        """Set a specific setting value"""
        self.settings[key] = value

    @property
    def group(self):
        """Get the group this inbox belongs to through its account"""
        return self.account.group if self.account else None

    def can_receive_messages(self):
        """Check if this inbox can receive messages"""
        return self.is_active and self.account.is_active

    def get_handler(self, async_preferred=True):
        """Get the appropriate handler for message processing"""
        if async_preferred and self.async_handler:
            return self.async_handler
        elif self.sync_handler:
            return self.sync_handler
        elif self.async_handler:
            return self.async_handler
        return None