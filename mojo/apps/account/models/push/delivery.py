from django.db import models
from mojo.models import MojoModel


class NotificationDelivery(models.Model, MojoModel):
    """
    Track all push notification delivery attempts and results.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", on_delete=models.CASCADE,
                           related_name="notification_deliveries")
    device = models.ForeignKey("account.RegisteredDevice", on_delete=models.CASCADE,
                             related_name="notification_deliveries")
    template = models.ForeignKey("account.NotificationTemplate", on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="deliveries")

    title = models.CharField(max_length=200, blank=True, null=True)
    body = models.TextField(blank=True, null=True)
    category = models.CharField(max_length=50, db_index=True)
    action_url = models.URLField(blank=True, null=True)
    data_payload = models.JSONField(default=dict, blank=True,
                                   help_text="Custom data payload sent with notification")

    # Delivery tracking
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed')
    ], default='pending', db_index=True)

    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, null=True)

    # Push service specific data
    platform_data = models.JSONField(default=dict, blank=True,
                                    help_text="Platform-specific response data")

    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ["view_notifications", "manage_notifications", "owner", "manage_users"]
        SAVE_PERMS = ["manage_notifications"]
        SEARCH_FIELDS = ["title", "category"]
        LIST_DEFAULT_FILTERS = {"status": "sent"}
        GRAPHS = {
            "basic": {
                "fields": ["id", "title", "category", "status", "sent_at", "created"]
            },
            "default": {
                "fields": ["id", "title", "body", "category", "action_url", "data_payload", "status",
                          "sent_at", "delivered_at", "error_message", "created"],
                "graphs": {
                    "user": "basic",
                    "device": "basic"
                }
            },
            "full": {
                "graphs": {
                    "user": "default",
                    "device": "default",
                    "template": "basic"
                }
            }
        }

    def __str__(self):
        display_title = self.title or f"[{self.category} data]"
        return f"{display_title} -> {self.device} ({self.status})"

    def mark_sent(self):
        """Mark notification as sent with timestamp."""
        from mojo.helpers import dates
        self.status = 'sent'
        self.sent_at = dates.utcnow()
        self.save(update_fields=['status', 'sent_at'])

    def mark_delivered(self):
        """Mark notification as delivered with timestamp."""
        from mojo.helpers import dates
        self.status = 'delivered'
        self.delivered_at = dates.utcnow()
        self.save(update_fields=['status', 'delivered_at'])

    def mark_failed(self, error_message):
        """Mark notification as failed with error message."""
        self.status = 'failed'
        self.error_message = error_message
        self.save(update_fields=['status', 'error_message'])
