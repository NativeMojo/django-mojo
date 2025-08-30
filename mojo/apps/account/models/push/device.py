from django.db import models
from mojo.models import MojoModel


class RegisteredDevice(models.Model, MojoModel):
    """
    Represents a device explicitly registered for push notifications via REST API.
    Separate from UserDevice which tracks browser sessions via duid/user-agent.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='registered_devices')

    # Device identification
    device_token = models.TextField(db_index=True, help_text="Push token from platform")
    device_id = models.CharField(max_length=255, db_index=True, help_text="App-provided device ID")
    platform = models.CharField(max_length=20, choices=[
        ('ios', 'iOS'),
        ('android', 'Android'),
        ('web', 'Web')
    ], db_index=True)

    # Device info
    app_version = models.CharField(max_length=50, blank=True)
    os_version = models.CharField(max_length=50, blank=True)
    device_name = models.CharField(max_length=100, blank=True)

    # Push preferences
    push_enabled = models.BooleanField(default=True, db_index=True)
    push_preferences = models.JSONField(default=dict, blank=True,
                                      help_text="Category-based notification preferences")

    # Status tracking
    is_active = models.BooleanField(default=True, db_index=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'device_id'), ('device_token', 'platform')]
        ordering = ['-last_seen']

    class RestMeta:
        VIEW_PERMS = ["view_devices", "manage_devices", "owner", "manage_users"]
        SAVE_PERMS = ["manage_devices", "owner"]
        SEARCH_FIELDS = ["device_name", "device_id"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "basic": {
                "fields": ["id", "device_id", "platform", "device_name", "push_enabled", "last_seen"]
            },
            "default": {
                "fields": ["id", "device_id", "platform", "device_name", "app_version",
                          "os_version", "push_enabled", "push_preferences", "last_seen"],
                "graphs": {
                    "user": "basic"
                }
            },
            "full": {
                "graphs": {
                    "user": "default"
                }
            }
        }

    def __str__(self):
        return f"{self.device_name or self.device_id} ({self.platform}) - {self.user.username}"
