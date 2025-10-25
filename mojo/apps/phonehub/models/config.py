from django.db import models
from mojo.models import MojoModel, MojoSecrets


class PhoneConfig(MojoSecrets, MojoModel):
    """
    Phone service configuration for SMS and phone lookup.
    Can be system-wide (group=None) or org-specific.
    Supports Twilio and AWS SNS. Sensitive credentials stored via MojoSecrets.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.OneToOneField("account.Group", on_delete=models.CASCADE,
                                related_name="phone_config", null=True, blank=True,
                                help_text="Organization for this config. Null = system default")

    name = models.CharField(max_length=100, help_text="Configuration name")
    is_active = models.BooleanField(default=True, db_index=True)

    # Provider Selection
    PROVIDER_CHOICES = [
        ('twilio', 'Twilio'),
        ('aws', 'AWS SNS'),
    ]
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES,
                              default='twilio', db_index=True)

    # Twilio-specific fields (credentials stored in mojo_secrets)
    # twilio_account_sid - stored in secrets
    # twilio_auth_token - stored in secrets
    twilio_from_number = models.CharField(max_length=20, blank=True, null=True,
                                        help_text="Twilio phone number for sending SMS")

    # AWS-specific fields (credentials stored in mojo_secrets)
    # aws_access_key_id - stored in secrets
    # aws_secret_access_key - stored in secrets
    aws_region = models.CharField(max_length=20, default='us-east-1',
                                help_text="AWS region for SNS")
    aws_sender_id = models.CharField(max_length=11, blank=True, null=True,
                                   help_text="AWS SNS sender ID (optional)")

    # Lookup Settings
    lookup_enabled = models.BooleanField(default=True, db_index=True,
                                       help_text="Enable phone number lookups")
    lookup_cache_days = models.IntegerField(default=90,
                                          help_text="Days to cache lookup results before re-lookup")

    # Test Mode
    test_mode = models.BooleanField(default=False, db_index=True,
                                  help_text="Enable test mode - don't send real SMS")

    class Meta:
        ordering = ['group__name', 'name']

    class RestMeta:
        VIEW_PERMS = ["manage_phone_config", "manage_groups"]
        SAVE_PERMS = ["manage_phone_config", "manage_groups"]
        DELETE_PERMS = ["manage_phone_config", "manage_groups"]
        SEARCH_FIELDS = ["name"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "provider", "test_mode", "is_active"]
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
        return f"{self.name} ({org}) - {self.get_provider_display()}"

    @classmethod
    def get_for_group(cls, group=None):
        """
        Get phone config for group. Priority: group config -> system default

        Args:
            group: Group object or None for system default

        Returns:
            PhoneConfig instance or None
        """
        if group:
            config = cls.objects.filter(group=group, is_active=True).first()
            if config:
                return config

        # Fallback to system default
        return cls.objects.filter(group__isnull=True, is_active=True).first()

    # Twilio credentials management
    def set_twilio_credentials(self, account_sid, auth_token):
        """Set Twilio credentials (will be encrypted)."""
        self.set_secret('twilio_account_sid', account_sid)
        self.set_secret('twilio_auth_token', auth_token)

    def get_twilio_account_sid(self):
        """Get decrypted Twilio account SID."""
        return self.get_secret('twilio_account_sid', '')

    def get_twilio_auth_token(self):
        """Get decrypted Twilio auth token."""
        return self.get_secret('twilio_auth_token', '')

    # AWS credentials management
    def set_aws_credentials(self, access_key_id, secret_access_key):
        """Set AWS credentials (will be encrypted)."""
        self.set_secret('aws_access_key_id', access_key_id)
        self.set_secret('aws_secret_access_key', secret_access_key)

    def get_aws_access_key_id(self):
        """Get decrypted AWS access key ID."""
        return self.get_secret('aws_access_key_id', '')

    def get_aws_secret_access_key(self):
        """Get decrypted AWS secret access key."""
        return self.get_secret('aws_secret_access_key', '')

    def test_connection(self):
        """
        Test provider configuration.

        Returns:
            dict with 'success' (bool), 'message' (str), and optional error details
        """
        if self.test_mode:
            return {
                'success': True,
                'message': 'Config is in test mode - provider not tested',
                'test_mode': True
            }

        if self.provider == 'twilio':
            return self._test_twilio()
        elif self.provider == 'aws':
            return self._test_aws()
        else:
            return {
                'success': False,
                'message': f'Unknown provider: {self.provider}',
                'error': 'invalid_provider'
            }

    def _test_twilio(self):
        """Test Twilio configuration."""
        account_sid = self.get_twilio_account_sid()
        auth_token = self.get_twilio_auth_token()

        if not account_sid or not auth_token:
            return {
                'success': False,
                'message': 'Twilio credentials not configured',
                'error': 'missing_credentials'
            }

        try:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)

            # Test by fetching account info
            account = client.api.accounts(account_sid).fetch()

            return {
                'success': True,
                'message': 'Twilio credentials valid',
                'account_status': account.status,
                'account_friendly_name': account.friendly_name
            }
        except ImportError:
            return {
                'success': False,
                'message': 'Twilio library not installed (pip install twilio)',
                'error': 'missing_library'
            }
        except Exception as e:
            return {
                'success': False,
                'message': f'Twilio test failed: {str(e)}',
                'error': 'connection_failed',
                'details': str(e)
            }

    def _test_aws(self):
        """Test AWS SNS configuration."""
        access_key = self.get_aws_access_key_id()
        secret_key = self.get_aws_secret_access_key()

        if not access_key or not secret_key:
            return {
                'success': False,
                'message': 'AWS credentials not configured',
                'error': 'missing_credentials'
            }

        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError

            # Create SNS client
            sns = boto3.client(
                'sns',
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=self.aws_region
            )

            # Test by listing topics (lightweight operation)
            response = sns.list_topics(MaxItems=1)

            return {
                'success': True,
                'message': 'AWS SNS credentials valid',
                'region': self.aws_region
            }
        except ImportError:
            return {
                'success': False,
                'message': 'AWS boto3 library not installed (pip install boto3)',
                'error': 'missing_library'
            }
        except (ClientError, NoCredentialsError) as e:
            return {
                'success': False,
                'message': f'AWS test failed: {str(e)}',
                'error': 'connection_failed',
                'details': str(e)
            }
        except Exception as e:
            return {
                'success': False,
                'message': f'AWS test failed: {str(e)}',
                'error': 'connection_failed',
                'details': str(e)
            }
