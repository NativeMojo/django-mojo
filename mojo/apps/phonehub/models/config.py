import requests
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
        ('mojo', 'Mojo Remote Instance'),
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

    # Mojo-remote provider fields (api key stored in mojo_secrets)
    # mojo_api_key - stored in secrets
    mojo_remote_url = models.CharField(max_length=255, blank=True, null=True,
                                       help_text="Base URL of the remote django-mojo instance, e.g. https://sms.example.com")

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
        VIEW_PERMS = ["manage_phone_config", "manage_groups", "comms"]
        SAVE_PERMS = ["manage_phone_config", "manage_groups", "comms"]
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

    def save(self, *args, **kwargs):
        # Defensive: strip trailing slash from mojo_remote_url so the service
        # layer can always concat paths without double slashes.
        if self.mojo_remote_url:
            self.mojo_remote_url = self.mojo_remote_url.rstrip('/')
        return super().save(*args, **kwargs)

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

    # Mojo remote provider credentials management
    def set_mojo_api_key(self, api_key):
        """Set encrypted API key for the remote mojo SMS provider."""
        self.set_secret('mojo_api_key', api_key)

    def get_mojo_api_key(self):
        """Get decrypted API key for the remote mojo SMS provider."""
        return self.get_secret('mojo_api_key', '')

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
        elif self.provider == 'mojo':
            return self._test_mojo()
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

    def _test_mojo(self):
        """Test the remote mojo SMS provider by validating the API key."""
        from mojo.helpers.settings import settings

        base_url = (self.mojo_remote_url or '').rstrip('/')
        api_key = self.get_mojo_api_key()

        if not base_url or not api_key:
            return {
                'success': False,
                'message': 'Mojo provider requires mojo_remote_url and mojo_api_key',
                'error': 'missing_credentials'
            }

        timeout = settings.get_static('SMS_REMOTE_TIMEOUT', 10)
        url = f"{base_url}/api/account/me"
        headers = {"Authorization": f"apikey {api_key}"}

        try:
            response = requests.get(
                url, headers=headers, timeout=timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            return {
                'success': False,
                'message': f'Mojo provider test timed out after {timeout}s',
                'error': 'timeout'
            }
        except Exception as e:
            # Log the raw exception for operators but do NOT echo it back to
            # the caller — `str(e)` from `requests` can carry the full URL,
            # internal hostnames, or TLS error details that should not surface
            # in REST responses.
            from mojo.helpers import logit
            logit.warning("[phonehub] mojo provider test_connection error: %s", e)
            return {
                'success': False,
                'message': 'Mojo provider connection failed (see logs)',
                'error': 'connection_failed'
            }

        if response.status_code in (401, 403):
            return {
                'success': False,
                'message': 'Mojo provider rejected the API key',
                'error': 'invalid_credentials',
                'status_code': response.status_code
            }

        if response.status_code >= 400:
            return {
                'success': False,
                'message': f'Mojo provider returned HTTP {response.status_code}',
                'error': 'connection_failed',
                'status_code': response.status_code
            }

        return {
            'success': True,
            'message': 'Mojo provider reachable and API key valid',
            'remote_url': base_url
        }
