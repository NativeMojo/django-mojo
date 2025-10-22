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
        VIEW_PERMS = ["manage_push_config", "manage_groups"]
        SAVE_PERMS = ["manage_push_config", "manage_groups"]
        SEARCH_FIELDS = ["name"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "test_mode", "default_sound", "is_active", "fcm_project_id"]
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
                return json.loads(data) if isinstance(data, str) else data
            except json.JSONDecodeError:
                return None
        return None

    @property
    def fcm_project_id(self):
        """Get FCM project ID from service account JSON."""
        service_account = self.get_fcm_service_account()
        if service_account:
            return service_account.get('project_id')
        return None

    def test_fcm_connection(self, test_token=None):
        """
        Test FCM configuration by attempting to send a test notification.

        Args:
            test_token: Optional FCM device token to test with.
                       If not provided, uses a dummy token (will fail but validates credentials).

        Returns:
            dict with 'success' (bool), 'message' (str), and optional 'error' details
        """
        from mojo.helpers import logit

        if self.test_mode:
            return {
                'success': True,
                'message': 'Config is in test mode - FCM not tested',
                'test_mode': True
            }

        service_account = self.get_fcm_service_account()
        if not service_account:
            return {
                'success': False,
                'message': 'No FCM service account configured',
                'error': 'missing_credentials',
                'note': 'Use set_fcm_service_account() to configure FCM v1'
            }

        try:
            from mojo.helpers.fcm import FCMv1Client
            # Initialize FCM v1 client
            fcm_client = FCMv1Client(service_account)

            # Use provided token or a dummy one
            token = test_token or "dummy_test_token_for_credential_validation"

            # Attempt to send test notification
            result = fcm_client.send(
                token=token,
                title="FCM Test",
                body="Testing FCM v1 configuration"
            )

            # Check result
            if result.get('success'):
                return {
                    'success': True,
                    'message': 'FCM v1 configuration valid - notification sent successfully',
                    'message_id': result.get('message_id'),
                    'fcm_version': 'v1'
                }
            else:
                # Check error type
                error = result.get('error', {})
                error_code = error.get('code') if isinstance(error, dict) else None
                error_message = error.get('message') if isinstance(error, dict) else str(error)
                status_code = result.get('status_code')

                # Invalid token is actually success (credentials are valid)
                if (status_code == 400 and
                    ('INVALID_ARGUMENT' in str(error_code) or
                     'not a valid FCM registration token' in error_message or
                     'invalid' in error_message.lower())):
                    return {
                        'success': True,
                        'message': 'FCM v1 credentials valid (dummy token rejected by FCM)',
                        'note': 'Your FCM credentials work! Provide a real device token to test actual delivery',
                        'fcm_version': 'v1',
                        'fcm_error': 'INVALID_ARGUMENT (expected - dummy token used)'
                    }
                elif status_code == 401 or status_code == 403:
                    return {
                        'success': False,
                        'message': 'FCM v1 credentials invalid or unauthorized',
                        'error': 'invalid_credentials',
                        'details': error_message,
                        'status_code': status_code
                    }
                else:
                    return {
                        'success': False,
                        'message': f'FCM v1 test failed: {error_message}',
                        'error': 'request_failed',
                        'status_code': status_code,
                        'details': error
                    }

        except ValueError as e:
            # Service account JSON is malformed
            return {
                'success': False,
                'message': 'Invalid service account JSON',
                'error': 'invalid_json',
                'details': str(e)
            }
        except Exception as e:
            error_str = str(e)
            return {
                'success': False,
                'message': f'FCM v1 test failed: {error_str}',
                'error': 'request_failed',
                'details': error_str
            }
