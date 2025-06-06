from django.db import models
from mojo.models import MojoModel


class Account(models.Model, MojoModel):
    """
    Notification service account for sending/receiving messages across different channels
    """
    
    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_notify"]
        SEARCH_FIELDS = ["domain", "kind"]
        SEARCH_TERMS = [
            "kind", "domain",
            ("group", "group__name")]

        GRAPHS = {
            "default": {
                "graphs": {
                    "group": "basic"
                }
            },
            "list": {
                "graphs": {
                    "group": "basic"
                }
            }
        }

    # Message service types
    EMAIL = 'email'
    SMS = 'sms'
    WHATSAPP = 'whatsapp'
    SIGNAL = 'signal'
    WEBSOCKET = 'ws'
    PUSH = 'push'
    
    KIND_CHOICES = [
        (EMAIL, 'Email'),
        (SMS, 'SMS'),
        (WHATSAPP, 'WhatsApp'),
        (SIGNAL, 'Signal'),
        (WEBSOCKET, 'WebSocket'),
        (PUSH, 'Push Notification'),
    ]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)
    
    group = models.ForeignKey(
        "account.Group", 
        related_name="notify_accounts",
        null=True, 
        blank=True, 
        default=None, 
        on_delete=models.CASCADE,
        help_text="Group that owns this notification account"
    )
    
    kind = models.CharField(
        max_length=32, 
        choices=KIND_CHOICES, 
        db_index=True,
        help_text="Type of notification service (email, sms, whatsapp, etc.)"
    )
    
    domain = models.CharField(
        max_length=255, 
        db_index=True,
        help_text="Domain for email (example.com) or phone number for SMS (9493211234)"
    )
    
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Service-specific configuration settings"
    )
    
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this account is active and can send/receive messages"
    )

    class Meta:
        unique_together = ['group', 'kind', 'domain']
        indexes = [
            models.Index(fields=['kind', 'domain']),
            models.Index(fields=['group', 'kind']),
        ]

    def __str__(self):
        group_name = self.group.name if self.group else "No Group"
        return f"{self.get_kind_display()} account: {self.domain} ({group_name})"

    def get_setting(self, key, default=None):
        """Get a specific setting value"""
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        """Set a specific setting value"""
        self.settings[key] = value

    @property
    def is_email(self):
        return self.kind == self.EMAIL

    @property
    def is_sms(self):
        return self.kind == self.SMS

    @property
    def is_whatsapp(self):
        return self.kind == self.WHATSAPP

    @property
    def is_signal(self):
        return self.kind == self.SIGNAL

    @property
    def is_websocket(self):
        return self.kind == self.WEBSOCKET

    @property
    def is_push(self):
        return self.kind == self.PUSH