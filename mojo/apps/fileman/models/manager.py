import os
from django.db import models
from mojo.models import MojoModel, MojoSecrets
from urllib.parse import urlparse


class FileManager(MojoSecrets, MojoModel):
    """
    File manager configuration for different storage backends and upload strategies
    """

    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_fileman", "manage_files"]
        SEARCH_FIELDS = ["name", "backend_type", "description"]
        SEARCH_TERMS = [
            "name", "backend_type", "description",
            ("group", "group__name")]

        GRAPHS = {
            "default": {
                "fields": [
                    "created", "id", "name", "backend_type", "backend_url",
                    "settings", "is_active", "is_default"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            },
            "list": {
                "fields": ["created", "id", "name", "backend_type",  "backend_url",
                    "settings", "is_active", "is_default"],
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

    @classmethod
    def get_from_request(cls, request):
        """Get the file manager from the request"""
        if request.DATA.get(["fileman", "filemanager"]):
            return cls.objects.get(pk=request.DATA.get(["fileman", "filemanager"]))
        if request.DATA.use_groups_fileman and request.group:
            return cls.get_for_user_group(group=request.group)
        return cls.get_for_user_group(user=request.user, group=request.group)

    @classmethod
    def get_for_user(cls, user, group=None):
        file_manager = cls.objects.filter(
            user=user, group=group, is_default=True, is_active=True
        ).first()
        if file_manager is None:
            if group:
                sys_manager = cls.get_for_group(group=group)
            else:
                sys_manager = cls.objects.filter(user=None, group=None, is_default=True, is_active=True).first()
            if sys_manager is not None:
                file_manager = cls(user=user, is_default=True, group=group, parent=sys_manager)
                file_manager.set_backend_url(sys_manager.backend_url, user.uuid.hex)
                file_manager.save()
        return file_manager

    @classmethod
    def get_for_group(cls, group=None):
        file_manager = cls.objects.filter(
            user=None, group=group, is_default=True, is_active=True
        ).first()
        if file_manager is None:
            sys_manager = cls.objects.filter(
                user=None, group=None, is_default=True, is_active=True
            ).first()
            if sys_manager is not None:
                file_manager = cls(group=group, is_default=True, user=None, parent=sys_manager)
                file_manager.set_backend_url(sys_manager.backend_url, group.uuid.hex)
                file_manager.save()
        return file_manager

    @classmethod
    def get_for_user_group(cls, user=None, group=None):
        """Get the file manager from the user and/or group"""
        file_manager = None
        if user and group is None:
            file_manager = cls.get_for_user(user=user)
        if not file_manager and group and user is None:
            file_manager = cls.get_for_user_group(group=group)
        if not file_manager and group and user:
            file_manager = cls.get_for_user(user=user, group=group)
        return file_manager
