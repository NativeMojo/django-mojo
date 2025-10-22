from django.db import models
from mojo.models import MojoModel, MojoSecrets


class PushConfig(MojoSecrets, MojoModel):
    """
    Push notification configuration. Can be system-wide (group=None) or org-specific.
    Uses FCM (Firebase Cloud Messaging) for all platforms (iOS, Android, Web).
    Sensitive credentials are encrypted via MojoSecrets.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.OneToOneField("account.Group", on_delete=models.CASCADE,
                                related_name="push_config", null=True, blank=True,
                                help_text="Organization for this config. Null = system default")

    name = models.CharField(max_length=100, help_text="Configuration name")
    is_active = models.BooleanField(default=True, db_index=True)

    # Test/Development Mode
    test_mode = models.BooleanField(default=False, db_index=True,
                                   help_text="Enable test mode - fake notifications for development")

    # FCM Configuration (supports iOS, Android, and Web)
    fcm_sender_id = models.CharField(max_length=100, blank=True)

    # General Settings
    default_sound = models.CharField(max_length=50, default="default")

    class Meta:
        ordering = ['group__name', 'name']

    class RestMeta:
        VIEW_PERMS = ["manage_push_config", "manage_groups"]
        SAVE_PERMS = ["manage_push_config", "manage_groups"]
        SEARCH_FIELDS = ["name"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "test_mode", "default_sound", "is_active"]
            },
            "default": {
                "exclude": ["mojo_secrets"],  # Never expose encrypted secrets
                "graphs": {
                    "group": "basic"
                }
            },
            "full": {
                "exclude": ["mojo_secrets"],  # Never expose encrypted secrets
                "graphs": {
                    "group": "default"
                }
            }
        }

    def __str__(self):
        org = self.group.name if self.group else "System Default"
        return f"{self.name} ({org})"

    @classmethod
    def get_for_user(cls, user):
        """
        Get push config for user. Priority: user's org config -> system default
        """
        if user.org:
            config = cls.objects.filter(group=user.org, is_active=True).first()
            if config:
                return config

        # Fallback to system default
        return cls.objects.filter(group__isnull=True, is_active=True).first()

    def set_fcm_server_key(self, server_key):
        """Set FCM server key (will be encrypted)."""
        self.set_secret('fcm_server_key', server_key)

    def get_fcm_server_key(self):
        """Get decrypted FCM server key."""
        return self.get_secret('fcm_server_key', '')
