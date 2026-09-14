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

    # FCM v1 uses service account JSON (stored encrypted in mojo_secrets)
    # No additional fields needed - project_id comes from service account JSON

    # General Settings
    default_sound = models.CharField(max_length=50, default="default")

    class Meta:
        ordering = ['group__name', 'name']

    class RestMeta:
        VIEW_PERMS = ["manage_push_config", "manage_groups", "comms"]
        SAVE_PERMS = ["manage_push_config", "manage_groups", "comms"]
        SEARCH_FIELDS = ["name"]
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "test_mode", "default_sound", "is_active", "fcm_project_id", "has_fcm_credentials", "fcm_client_email"]
            },
            "default": {
                "exclude": ["mojo_secrets"],  # Never expose encrypted secrets
                "fields": ["id", "created", "modified", "name", "is_active", "test_mode",
                           "default_sound", "fcm_project_id", "has_fcm_credentials", "fcm_client_email"],
                "graphs": {
                    "group": "basic"
                }
            },
            "full": {
                "exclude": ["mojo_secrets"],  # Never expose encrypted secrets
                "fields": ["id", "created", "modified", "name", "is_active", "test_mode",
                           "default_sound", "fcm_project_id", "has_fcm_credentials", "fcm_client_email"],
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

    def set_fcm_service_account(self, service_account_json):
        """
        Set FCM service account JSON (will be encrypted).

        Args:
            service_account_json: Dict or JSON string with service account credentials
        """
        import json
        if isinstance(service_account_json, dict):
            service_account_json = json.dumps(service_account_json)
        self.set_secret('fcm_service_account', service_account_json)

    def get_fcm_service_account(self):
        """Get decrypted FCM service account JSON."""
        import json
        data = self.get_secret('fcm_service_account', '')
        if data:
            try:
                parsed = json.loads(data) if isinstance(data, str) else data
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    @property
    def fcm_project_id(self):
        """Get FCM project ID from service account JSON."""
        service_account = self.get_fcm_service_account()
        if service_account:
            value = service_account.get('project_id')
            return value[:200] if isinstance(value, str) else None
        return None

    @property
    def has_fcm_credentials(self):
        """Presence is not a claim that the credential is valid."""
        return bool(self.get_secret('fcm_service_account', ''))

    @property
    def fcm_client_email(self):
        account = self.get_fcm_service_account() or {}
        value = account.get('client_email')
        return value[:254] if isinstance(value, str) else None

    def test_fcm_connection(self, test_token=None, client_factory=None):
        """Validate with FCM; an explicit legacy token requests a real send."""
        from mojo.helpers.fcm import FCMv1Client
        from mojo.apps.account.services.push import provider_test_result
        validation = not test_token
        account = self.get_fcm_service_account()
        if not account:
            result = {'success': False, 'outcome': 'blocked', 'error_code': 'missing_credentials'}
        elif test_token and self.test_mode:
            result = {'success': False, 'outcome': 'blocked', 'error_code': 'test_mode'}
        else:
            try:
                client = (client_factory or FCMv1Client)(account)
                result = client.validate() if validation else client.send(
                    token=test_token, title='FCM Test', body='Testing FCM configuration')
            except Exception:
                result = {'success': False, 'outcome': 'blocked', 'error_code': 'invalid_credentials'}
        result = provider_test_result(result, validation=validation)
        result.update(test_mode=self.test_mode, validation_only=validation, fcm_version='v1')
        return result
