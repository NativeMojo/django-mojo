import os
import boto3
from botocore.exceptions import ClientError
from django.db import models
from mojo.models import MojoModel, MojoSecrets
from urllib.parse import urlparse
from mojo.helpers.settings import settings

class FileManager(MojoSecrets, MojoModel):
    """
    File manager configuration for different storage backends and upload strategies
    """

    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-id"
        POST_SAVE_ACTIONS = ["test_connection", "fix_cors", "clone", "check_cors"]
        VIEW_PERMS = ["view_fileman", "manage_files"]
        SEARCH_FIELDS = ["name", "backend_type", "description"]
        SEARCH_TERMS = [
            "name", "backend_type", "description",
            ("group", "group__name")]

        GRAPHS = {
            "default": {
                "extra": ["aws_region", "aws_key", "aws_secret_masked", "allowed_origins"],
                "fields": [
                    "created", "id", "name", "backend_type", "backend_url",
                    "is_active", "is_default"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            },
            "list": {
                "extra": ["aws_region", "aws_key", "aws_secret_masked", "allowed_origins"],
                "fields": ["created", "id", "name", "backend_type",  "backend_url",
                    "is_active", "is_default"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            }
        }

    # Storage backend types
    FILE_SYSTEM = 'file'
    AWS_S3 = 's3'
    AZURE_BLOB = 'azure'
    GOOGLE_CLOUD = 'gcs'
    CUSTOM = 'custom'

    BACKEND_CHOICES = [
        (FILE_SYSTEM, 'File System'),
        (AWS_S3, 'AWS S3'),
        (AZURE_BLOB, 'Azure Blob Storage'),
        (GOOGLE_CLOUD, 'Google Cloud Storage'),
        (CUSTOM, 'Custom Backend'),
    ]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="file_managers",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group that owns this file manager configuration"
    )

    user = models.ForeignKey(
        "account.User",
        related_name="file_managers",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="User that owns this file manager configuration"
    )

    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Descriptive name for this file manager configuration"
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text="Optional description of this file manager's purpose"
    )

    backend_type = models.CharField(
        max_length=32,
        choices=BACKEND_CHOICES,
        db_index=True,
        help_text="Type of storage backend (file, s3, azure, gcs, custom)"
    )

    backend_url = models.CharField(
        max_length=500,
        help_text="Base URL or connection string for the storage backend"
    )

    supports_direct_upload = models.BooleanField(
        default=False,
        help_text="Whether this backend supports direct upload (pre-signed URLs)"
    )

    max_file_size = models.BigIntegerField(
        default=1000 * 1024 * 1024,  # 100MB default
        help_text="Maximum file size in bytes (0 for unlimited)"
    )

    allowed_extensions = models.JSONField(
        default=list,
        blank=True,
        help_text="List of allowed file extensions (empty for all)"
    )

    allowed_mime_types = models.JSONField(
        default=list,
        blank=True,
        help_text="List of allowed MIME types (empty for all)"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether this file manager is active and can be used"
    )

    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is the default file manager for the group or user"
    )

    is_public = models.BooleanField(
        default=True,
        help_text="Whether this allows public access to the files"
    )

    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Used if this file manager is a child of another file manager, and inherits settings from its parent"
    )

    class Meta:
        unique_together = [
            ['group', 'name'],
        ]
        indexes = [
            models.Index(fields=['backend_type', 'is_active']),
            models.Index(fields=['group', 'is_default']),
            models.Index(fields=['user', 'is_default']),
            models.Index(fields=['group', 'backend_type']),
        ]

    def __str__(self):
        group_name = self.group.name if self.group else "Global"
        return f"{self.name} ({self.get_backend_type_display()}) - {group_name}"

    def save(self, *args, **kwargs):
        """Override save to ensure name is set before saving"""
        if not self.name:
            self.name = self.generate_name()
        super().save(*args, **kwargs)

    def get_setting(self, key, default=None):
        """Get a specific setting value"""
        value = self.get_secret(key, default)
        if value is None:
            value = self.primary_parent.get_secret(key, default)
        return value

    def set_setting(self, key, value):
        """Set a specific setting value"""
        self.set_secret(key, value)

    def set_settings(self, value):
        """Set a specific setting value"""
        self.set_secrets(value)

    def set_backend_url(self, url, *args):
        """Set the backend URL"""
        self.backend_url = os.path.join(url, *args)
        self.backend_type = self.backend_url.split(':')[0]

    def _update_default(self):
        if self.is_default:
            if self.pk is None:
                FileManager.objects.filter(
                    group=self.group,
                    user=self.user,
                    is_default=True
                ).update(is_default=False)
            else:
                FileManager.objects.filter(
                    group=self.group,
                    user=self.user,
                    is_default=True
                ).exclude(pk=self.pk).update(is_default=False)

    _backend = None

    @property
    def aws_key(self):
        return self.get_secret('aws_key')

    @property
    def aws_secret(self):
        return self.get_secret('aws_secret')

    @property
    def aws_secret_masked(self):
        secret = self.get_secret('aws_secret', '')
        if len(secret) > 4:
            return '*' * (len(secret) - 4) + secret[-4:]
        return secret

    @property
    def aws_region(self):
        return self.get_secret('aws_region')

    @property
    def is_verified(self):
        return self.status in ["verified", "ready"]

    def set_aws_key(self, key):
        self.set_secret('aws_key', key)

    def set_aws_secret(self, secret):
        self.set_secret('aws_secret', secret)

    def set_aws_region(self, secret):
        self.set_secret('aws_region', secret)

    def set_allowed_origins(self, origins):
        if isinstance(origins, str) and "," in origins:
            origins = [origin.strip() for origin in origins.split(',')]
        self.set_secret('allowed_origins', origins)

    @property
    def allowed_origins(self):
        return self.get_secret('allowed_origins')

    @property
    def backend(self):
        """Get the backend instance"""
        from mojo.apps.fileman import backends
        if not self._backend:
            self._backend = backends.get_backend(self)
        return self._backend

    @property
    def settings(self):
        return self.secrets

    @property
    def primary_settings(self):
        return self.primary_parent.secrets

    @property
    def primary_parent(self):
        parent = self
        while parent.parent:
            parent = parent.parent
        return parent

    @property
    def root_path(self):
        purl = urlparse(self.backend_url)
        return purl.path.lstrip('/')

    @property
    def root_location(self):
        purl = urlparse(self.backend_url)
        return purl.netloc

    @property
    def is_file_system(self):
        return self.backend_type == self.FILE_SYSTEM

    @property
    def is_s3(self):
        return self.backend_type == self.AWS_S3

    @property
    def is_azure(self):
        return self.backend_type == self.AZURE_BLOB

    @property
    def is_gcs(self):
        return self.backend_type == self.GOOGLE_CLOUD

    @property
    def is_custom(self):
        return self.backend_type == self.CUSTOM

    def can_upload_file(self, filename, file_size=None):
        """Check if a file can be uploaded based on restrictions"""
        if not self.is_active:
            return False, "File manager is not active"

        # Check file size
        if file_size and self.max_file_size > 0 and file_size > self.max_file_size:
            return False, f"File size exceeds maximum of {self.max_file_size} bytes"

        # Check file extension
        if self.allowed_extensions:
            import os
            _, ext = os.path.splitext(filename.lower())
            if ext and ext[1:] not in [e.lower() for e in self.allowed_extensions]:
                return False, f"File extension {ext} is not allowed"

        return True, "File can be uploaded"

    def can_upload_mime_type(self, mime_type):
        """Check if a MIME type is allowed"""
        if not self.allowed_mime_types:
            return True
        return mime_type.lower() in [mt.lower() for mt in self.allowed_mime_types]

    def on_rest_created(self):
        self._update_default()

    def on_rest_pre_save(self, changed_fields, created):
        self._update_default()
        if not self.name:
            self.name = self.generate_name()
        if created:
            if not self.aws_region:
                self.set_aws_region(settings.get("AWS_REGION", "us-east-1"))
            if not self.aws_key:
                self.set_aws_key(settings.get("AWS_KEY", None))
            if not self.aws_secret:
                self.set_aws_secret(settings.get("AWS_SECRET", None))
        if created or "is_default" in changed_fields:
            self._update_default()

    def on_rest_saved(self, changed_fields, created):
        self._update_default()
        if not self.name:
            self.name = self.generate_name()
        if "is_public" in changed_fields or created:
            if self.is_public:
                self.backend.make_path_public()
            else:
                self.backend.make_path_private()

    def generate_name(self):
        if self.user and self.group:
            return f"{self.user.username}@{self.group.name}'s {self.backend_type} FileManager"
        elif self.user:
            return f"{self.user.username}'s {self.backend_type} FileManager"
        elif self.group:
            return f"{self.group.name}'s {self.backend_type} FileManager"
        return f"{self.backend_type} FileManager"

    def on_action_test_connection(self, value):
        try:
            self.backend.test_connection()
            return dict(status=True)
        except Exception as e:
            return dict(status=False, error=str(e))

    def on_action_fix_cors(self, value):
        try:
            if not self.is_s3:
                return dict(status=False, error="CORS management is only supported for S3 backends.")
            # Validate connectivity first
            self.backend.test_connection()
            allowed_origins = self._resolve_allowed_origins_from_value_or_settings(value or {})
            result = self.update_cors(allowed_origins)
            return dict(status=True, result=result)
        except Exception as e:
            return dict(status=False, error=str(e))

    def on_action_check_cors(self, value):
        try:
            if not self.is_s3:
                return dict(status=False, error="CORS management is only supported for S3 backends.")
            self.backend.test_connection()
            # allowed_origins = self._resolve_allowed_origins_from_value_or_settings(value or {})
            result = self.check_cors_config(allowed_origins=self.allowed_origins)
            return dict(status=True, result=result)
        except Exception as e:
            return dict(status=False, error=str(e))

    def on_action_clone(self, value):
        secrets = self.secrets
        new_manager = FileManager(user=self.user, group=self.group)
        new_manager.name = f"Clone of {self.name}"
        new_manager.backend_url = self.backend_url
        new_manager.backend_type = self.backend_type
        new_manager.set_secrets(secrets)
        new_manager.save()
        return dict(status=True, id=new_manager.id)

    def fix_cors(self):
        """
        Ensure bucket CORS allows direct uploads from configured origins.
        This uses manager settings and does not require manual AWS console changes.
        """
        if not self.is_s3:
            return
        allowed_origins = self._resolve_allowed_origins_from_value_or_settings({})
        self.update_cors(allowed_origins)

    # --- CORS helpers for S3 direct upload ---
    def _s3_client(self):
        if not self.is_s3:
            raise ValueError("CORS management is only supported for S3 backends.")
        session = boto3.Session(
            aws_access_key_id=self.aws_key,
            aws_secret_access_key=self.aws_secret,
            region_name=self.aws_region or "us-east-1",
        )
        endpoint_url = self.get_setting("endpoint_url", None)
        return session.client("s3", endpoint_url=endpoint_url)

    def _resolve_allowed_origins_from_value_or_settings(self, value):
        """
        Resolve a list of allowed origins from action value or global settings.
        Accepts 'origins', 'allowed_origins', 'domains', or 'list_of_domains' keys.
        Falls back to settings such as CORS_ALLOWED_ORIGINS, ALLOWED_ORIGINS, FRONTEND_ORIGIN/URL.
        """
        origins = []

        if isinstance(value, dict):
            for key in ("origins", "allowed_origins", "domains", "list_of_domains"):
                v = value.get(key)
                if v:
                    if isinstance(v, str):
                        origins.extend([s.strip() for s in v.split(",") if s.strip()])
                    elif isinstance(v, (list, tuple)):
                        origins.extend([str(s).strip() for s in v if str(s).strip()])
                    break

        for key in ("CORS_ALLOWED_ORIGINS", "ALLOWED_ORIGINS"):
            v = settings.get(key)
            if v:
                if isinstance(v, str):
                    origins.extend([s.strip() for s in v.split(",") if s.strip()])
                elif isinstance(v, (list, tuple)):
                    origins.extend([str(s).strip() for s in v if str(s).strip()])

        for key in ("FRONTEND_ORIGIN", "FRONTEND_URL", "SITE_URL", "BASE_URL"):
            v = settings.get(key)
            if v:
                origins.append(str(v).strip())

        # Normalize: dedupe, drop trailing slash
        cleaned = []
        seen = set()
        for o in origins:
            if not o:
                continue
            if o.endswith("/"):
                o = o[:-1]
            if o not in seen:
                seen.add(o)
                cleaned.append(o)

        if not cleaned:
            raise ValueError("No allowed origins provided. Please pass at least one origin.")
        return cleaned

    def check_cors_config(self, allowed_origins=None, required_methods=None, required_headers=None):
        """
        Check the current CORS configuration to ensure it supports direct uploads.
        Note: S3 CORS is bucket-wide. Prefix-level restriction must be enforced by IAM/policy and presigned URLs.
        """
        if not self.is_s3:
            raise ValueError("CORS management is only supported for S3 backends.")

        s3 = self._s3_client()
        bucket = self.root_location

        try:
            resp = s3.get_bucket_cors(Bucket=bucket)
            config = resp
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchCORSConfiguration":
                return {"ok": False, "issues": ["No CORS configuration set on this bucket."], "config": None}
            return {"ok": False, "issues": [str(e)], "config": None}

        if allowed_origins is None:
            allowed_origins = self._resolve_allowed_origins_from_value_or_settings({})
        if not allowed_origins:
            raise ValueError("No allowed origins provided. Please pass at least one origin.")

        required_methods = [m.upper() for m in (required_methods or ["GET", "PUT", "POST", "HEAD"])]
        required_headers = [h.lower() for h in (required_headers or ["content-type"])]

        rules = config.get("CORSRules", [])
        issues = []

        def origin_covered(origin: str) -> bool:
            for r in rules:
                origins = r.get("AllowedOrigins", [])
                if "*" in origins or origin in origins:
                    methods = [m.upper() for m in r.get("AllowedMethods", [])]
                    if not all(m in methods for m in required_methods):
                        continue
                    headers = [h.lower() for h in r.get("AllowedHeaders", [])]
                    if "*" in headers or all(h in headers for h in required_headers):
                        return True
            return False

        for origin in allowed_origins:
            if not origin_covered(origin):
                issues.append(f"Origin not covered for direct upload: {origin}")

        return {"ok": len(issues) == 0, "issues": issues, "config": config}

    def update_cors(self, allowed_origins, merge=True, allowed_methods=None, allowed_headers=None, expose_headers=None, max_age_seconds=3000):
        """
        Update bucket CORS to support direct uploads from allowed_origins.
        If merge=True, append our rule to any existing rules; otherwise replace entirely.
        """
        if not self.is_s3:
            raise ValueError("CORS management is only supported for S3 backends.")

        s3 = self._s3_client()
        bucket = self.root_location
        if not allowed_origins:
            raise ValueError("No allowed origins provided. Please pass at least one origin.")

        if allowed_methods is None:
            allowed_methods = ["POST", "HEAD"] if getattr(self.backend, "server_side_encryption", None) else ["PUT", "HEAD"]
        allowed_methods = [m.upper() for m in allowed_methods]
        allowed_headers = [h for h in (allowed_headers or ["*"])]
        expose_headers = expose_headers or ["ETag", "x-amz-request-id", "x-amz-id-2", "x-amz-version-id"]

        desired = {
            "CORSRules": [
                {
                    "AllowedOrigins": allowed_origins,
                    "AllowedMethods": allowed_methods,
                    "AllowedHeaders": allowed_headers,
                    "ExposeHeaders": expose_headers,
                    "MaxAgeSeconds": max_age_seconds,
                }
            ]
        }

        # If already compliant, no change
        verify_required_headers = [] if getattr(self.backend, "server_side_encryption", None) else ["content-type"]
        check = self.check_cors_config(allowed_origins, required_methods=allowed_methods, required_headers=verify_required_headers)
        if check["ok"]:
            return {"changed": False, "message": "Existing CORS already supports direct uploads.", "current": check["config"]}

        current = None
        try:
            current = s3.get_bucket_cors(Bucket=bucket)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchCORSConfiguration":
                raise

        if merge and current:
            merged = {"CORSRules": current.get("CORSRules", []) + desired["CORSRules"]}
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=merged)
            applied = merged
        else:
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=desired)
            applied = desired

        verify_required_headers = [] if getattr(self.backend, "server_side_encryption", None) else ["content-type"]
        verify = self.check_cors_config(allowed_origins, required_methods=allowed_methods, required_headers=verify_required_headers)
        return {"changed": True, "applied": applied, "verified": verify["ok"], "post_update_issues": verify["issues"]}

    @classmethod
    def get_from_request(cls, request):
        """Get the file manager from the request"""
        if request.DATA.get(["fileman", "filemanager"]):
            return cls.objects.get(pk=request.DATA.get(["fileman", "filemanager"]))
        if request.DATA.use_groups_fileman and request.group:
            return cls.get_for_user_group(group=request.group)
        return cls.get_for_user_group(user=request.user, group=request.group)

    @classmethod
    def get_for_user(cls, user):
        """Get the file manager for a specific user (without group)"""
        from django.db import transaction, IntegrityError

        file_manager = cls.objects.filter(
            user=user, group=None, is_default=True, is_active=True
        ).first()
        if file_manager is None:
            # Get the system default manager
            sys_manager = cls.objects.filter(user=None, group=None, is_default=True, is_active=True).first()
            if sys_manager is not None:
                # Generate name before creating to avoid race conditions
                temp_manager = cls(user=user, is_default=True, group=None, parent=sys_manager)
                temp_manager.set_backend_url(sys_manager.backend_url, user.uuid.hex)
                name = temp_manager.generate_name()

                try:
                    with transaction.atomic():
                        # Use get_or_create to handle race conditions
                        file_manager, created = cls.objects.get_or_create(
                            user=user,
                            group=None,
                            name=name,
                            defaults={
                                'is_default': True,
                                'parent': sys_manager,
                                'backend_type': sys_manager.backend_type,
                                'backend_url': temp_manager.backend_url,
                                'is_active': True,
                            }
                        )
                        if created:
                            # Copy secrets from parent
                            file_manager.set_secrets(sys_manager.secrets)
                            file_manager.save()
                except IntegrityError:
                    # If still a race condition, fetch the existing one
                    file_manager = cls.objects.filter(
                        user=user, group=None, is_default=True, is_active=True
                    ).first()
        return file_manager

    @classmethod
    def get_for_group(cls, group=None):
        from django.db import transaction, IntegrityError

        file_manager = cls.objects.filter(
            user=None, group=group, is_default=True, is_active=True
        ).first()
        if file_manager is None:
            sys_manager = cls.objects.filter(
                user=None, group=None, is_default=True, is_active=True
            ).first()
            if sys_manager is not None:
                # Generate name before creating to avoid race conditions
                temp_manager = cls(group=group, is_default=True, user=None, parent=sys_manager)
                temp_manager.set_backend_url(sys_manager.backend_url, group.get_uuid())
                name = temp_manager.generate_name()

                try:
                    with transaction.atomic():
                        # Use get_or_create to handle race conditions
                        file_manager, created = cls.objects.get_or_create(
                            user=None,
                            group=group,
                            name=name,
                            defaults={
                                'is_default': True,
                                'parent': sys_manager,
                                'backend_type': sys_manager.backend_type,
                                'backend_url': temp_manager.backend_url,
                                'is_active': True,
                            }
                        )
                        if created:
                            # Copy secrets from parent
                            file_manager.set_secrets(sys_manager.secrets)
                            file_manager.save()
                except IntegrityError:
                    # If still a race condition, fetch the existing one
                    file_manager = cls.objects.filter(
                        user=None, group=group, is_default=True, is_active=True
                    ).first()
        return file_manager

    @classmethod
    def get_for_user_group(cls, user=None, group=None):
        """Get the file manager from the user and/or group

        If group is provided, returns the group's FileManager (user-agnostic).
        If only user is provided, returns the user's FileManager.
        """
        if group:
            # Always use group manager when group is present, ignore user
            return cls.get_for_group(group=group)
        elif user:
            # Only use user manager if no group
            return cls.get_for_user(user=user)
        return None
