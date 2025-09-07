from django.db import models
from mojo.models import MojoModel
from objict import objict
import io
import uuid
import hashlib
import base64
import magic
import mimetypes
from datetime import datetime
import os
from mojo.apps.fileman import utils
from mojo.apps.fileman.models import FileManager


class File(models.Model, MojoModel):
    """
    File model representing uploaded files with metadata and storage information
    """

    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["view_fileman", "manage_files"]
        SEARCH_FIELDS = ["filename", "content_type"]
        POST_SAVE_ACTIONS = ["action"]
        SEARCH_TERMS = [
            "filename",  "content_type",
            ("group", "group__name"),
            ("file_manager", "file_manager__name")]

        GRAPHS = {
            "upload": {
                "fields": ["id", "filename", "content_type", "file_size", "upload_url"],
            },
            "detailed": {
                "extra": ["url", "renditions"],
                "graphs": {
                    "group": "basic",
                    "file_manager": "basic",
                    "user": "basic"
                }
            },
            "basic": {
                "fields": ["id", "filename", "content_type", "category"],
                "extra": ["url", "thumbnail"],
            },
            "default": {
                "extra": ["url", "renditions"],
            },
            "list": {
                "extra": ["url", "renditions"],
                "graphs": {
                    "group": "basic",
                    "file_manager": "basic",
                    "user": "basic"
                }
            }
        }

    # Upload status choices
    PENDING = 'pending'
    UPLOADING = 'uploading'
    COMPLETED = 'completed'
    FAILED = 'failed'
    EXPIRED = 'expired'

    STATUS_CHOICES = [
        (PENDING, 'Pending Upload'),
        (UPLOADING, 'Uploading'),
        (COMPLETED, 'Upload Completed'),
        (FAILED, 'Upload Failed'),
        (EXPIRED, 'Upload Expired'),
    ]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="files",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group that owns this file"
    )

    user = models.ForeignKey(
        "account.User",
        related_name="files",
        null=True,
        blank=True,
        default=None,
        on_delete=models.SET_NULL,
        help_text="User who uploaded this file"
    )

    file_manager = models.ForeignKey(
        "fileman.FileManager",
        related_name="files",
        on_delete=models.CASCADE,
        help_text="File manager configuration used for this file"
    )

    filename = models.CharField(
        max_length=255,
        db_index=True,
        help_text="User-provided filename"
    )

    storage_filename = models.CharField(
        max_length=255,
        help_text="Storage filename",
        default=None,
        blank=True,
        null=True,
    )

    storage_file_path = models.TextField(
        help_text="Full path to file in storage backend"
    )

    download_url = models.TextField(
        blank=True,
        null=True,
        default=None,
        help_text="Persistent URL for downloading the file, (if allowed)"
    )

    file_size = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="File size in bytes"
    )

    content_type = models.CharField(
        max_length=255,
        db_index=True,
        help_text="MIME type of the file"
    )

    category = models.CharField(
        max_length=255,
        db_index=True,
        default=None,
        blank=True,
        null=True,
        help_text="A category for the file, like 'image', 'document', 'video', etc."
    )

    checksum = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="File checksum (MD5, SHA256, etc.)"
    )

    upload_token = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Unique token for tracking direct uploads"
    )

    upload_status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
        help_text="Current status of the file upload"
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional file metadata and custom properties"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether this file is active and accessible"
    )

    is_public = models.BooleanField(
        default=False,
        help_text="Whether this file can be accessed without authentication"
    )

    upload_url = None

    class Meta:
        indexes = [
            models.Index(fields=['upload_status', 'created']),
            models.Index(fields=['file_manager', 'upload_status']),
            models.Index(fields=['group', 'is_active']),
            models.Index(fields=['content_type', 'is_active']),
        ]

    def __str__(self):
        return f"{self.filename} ({self.get_upload_status_display()})"

    def on_rest_pre_save(self, changed_fields, created):
        if created:
            if not hasattr(self, "file_manager") or self.file_manager is None:
                self.file_manager = FileManager.get_from_request(self.active_request)
            if not self.content_type:
                self.content_type = mimetypes.guess_type(self.filename)[0] or 'application/octet-stream'
            self.category = utils.get_file_category(self.content_type)
            if not self.storage_filename:
                self.generate_storage_filename()

    def on_rest_pre_delete(self):
        # we need to handle the deletion of the file from storage
        if self.storage_file_path:
            name, ext = os.path.splitext(self.filename)
            renditions_path = os.path.join(self.file_manager.root_path, name)
            self.file_manager.backend.delete_folder(renditions_path)
            self.file_manager.backend.delete(self.storage_file_path)

    def generate_upload_token(self, commit=False):
        """Generate a unique upload token"""
        self.upload_token = hashlib.sha256(f"{uuid.uuid4()}{datetime.now()}".encode()).hexdigest()[:32]
        if commit:
            self.save()

    def generate_storage_filename(self):
        """Generate a unique filename for storage"""
        name, ext = os.path.splitext(self.filename)
        # timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        self.storage_filename = f"{name}_{unique_id}{ext}"
        self.storage_file_path = os.path.join(self.file_manager.root_path, self.storage_filename)

    def request_upload_url(self):
        """Request a pre-signed URL for direct upload"""
        if not self.file_manager.backend.supports_direct_upload:
            self.generate_upload_token(True)
            self.upload_url = f"/api/fileman/upload/{self.upload_token}"
        else:
            data = self.file_manager.backend.generate_upload_url(self.storage_file_path, self.content_type, self.file_size)
            self.debug("request_upload_url", data)
            if "url" in data:
                self.upload_url = data['url']
            else:
                self.upload_url = data
        return self.upload_url

    def get_metadata(self, key, default=None):
        """Get a specific metadata value"""
        return self.metadata.get(key, default)

    def set_metadata(self, key, value):
        """Set a specific metadata value"""
        self.metadata[key] = value

    _renditions = None
    @property
    def renditions(self):
        if self._renditions is None:
            self._renditions = objict.from_dict({r.role: r.to_dict() for r in self.file_renditions.all()})
        return self._renditions

    @property
    def is_pending(self):
        return self.upload_status == self.PENDING

    @property
    def is_uploading(self):
        return self.upload_status == self.UPLOADING

    @property
    def is_completed(self):
        return self.upload_status == self.COMPLETED

    @property
    def is_failed(self):
        return self.upload_status == self.FAILED

    @property
    def is_expired(self):
        return self.upload_status == self.EXPIRED

    @property
    def is_upload_expired(self):
        """Check if the upload URL has expired"""
        if not self.upload_expires_at:
            return False
        return datetime.now() > self.upload_expires_at

    @property
    def url(self):
        return self.generate_download_url()

    @property
    def thumbnail(self):
        r = self.get_rendition_by_role('thumbnail')
        if r:
            return r.url
        return None

    def get_rendition_by_role(self, role):
        return self.file_renditions.filter(role=role).first()

    def generate_download_url(self):
        if self.download_url:
            return self.download_url
        if self.file_manager.is_public:
            self.download_url = self.file_manager.backend.get_url(self.storage_file_path)
            return self.download_url
        return self.file_manager.backend.get_url(self.storage_file_path, self.get_setting("urls_expire_in", 3600))

    def on_action_action(self, action):
        if action == "mark_as_completed":
            self.mark_as_completed(commit=True)
        elif action == "mark_as_failed":
            self.mark_as_failed(commit=True)
        elif action == "mark_as_uploading":
            self.mark_as_uploading(commit=True)

    def set_filename(self, filename):
        self.filename = filename
        if not self.content_type:
            self.content_type = mimetypes.guess_type(filename)[0]
            self.category = utils.get_file_category(self.content_type)


    def create_renditions(self):
        """Create renditions for the file"""
        from mojo.apps.fileman import renderer
        renderer.create_all_renditions(self)

    def mark_as_uploading(self, commit=False):
        """Mark file as currently being uploaded"""
        self.upload_status = self.UPLOADING
        if commit:
            self.atomic_save()

    def mark_as_completed(self, file_size=None, checksum=None, commit=False):
        """Mark file upload as completed"""
        if file_size:
            self.file_size = file_size
        if checksum:
            self.checksum = checksum
        if self.file_manager.backend.exists(self.storage_file_path):
            self.upload_status = self.COMPLETED
            self.create_renditions()
        else:
            self.upload_status = self.FAILED
        if commit:
            self.atomic_save()

    def mark_as_failed(self, error_message=None, commit=False):
        """Mark file upload as failed"""
        self.upload_status = self.FAILED
        if error_message:
            self.set_metadata('error_message', error_message)
        if commit:
            self.atomic_save()

    def mark_as_expired(self):
        """Mark file upload as expired"""
        self.upload_status = self.EXPIRED
        self.save(update_fields=['upload_status', 'modified'])

    def get_file_extension(self):
        """Get the file extension"""
        import os
        return os.path.splitext(self.filename)[1].lower()

    def get_human_readable_size(self):
        """Get human readable file size"""
        if not self.file_size:
            return "Unknown"

        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if self.file_size < 1024.0:
                return f"{self.file_size:.1f} {unit}"
            self.file_size /= 1024.0
        return f"{self.file_size:.1f} PB"

    def can_be_accessed_by(self, user=None, group=None):
        """Check if file can be accessed by user/group"""
        if not self.is_active:
            return False

        if self.is_public:
            return True

        if user and self.uploaded_by == user:
            return True

        if group and self.group == group:
            return True

        return False

    def on_rest_save_file(self, name, file):
        self.content_type = file.content_type
        self.category = utils.get_file_category(self.content_type)
        self.set_filename(file.name)
        self.file_manager = FileManager.get_from_request(self.active_request)
        self.generate_storage_filename()
        self.mark_as_uploading(True)
        self.file_manager.backend.save(file, self.storage_file_path, self.content_type)
        self.mark_as_completed(commit=True)

    @classmethod
    def create_from_file(cls, file, name, request=None, user=None, group=None):
        """Create a new file instance from a file"""
        if request:
            file_manager = FileManager.get_from_request(request)
        else:
            file_manager = FileManager.get_for_user_group(user, group)
        instance = cls()
        instance.filename = file.name
        instance.file_size = file.size
        instance.file_manager = file_manager
        instance.user = user
        instance.group = group
        instance.set_filename(file.name)
        instance.category = utils.get_file_category(instance.content_type)
        instance.on_rest_pre_save({}, True)
        instance.save()

        # now we need to upload the file
        instance.on_rest_save_file(name, file)

        return instance

    @classmethod
    def on_rest_related_save(cls, related_instance, related_field_name, field_value, current_instance=None):
        # this allows us to handle json posts with inline base64 file data
        if isinstance(field_value, str):
            mime_type = None
            b64_data = field_value

            # Check for and parse Data URL scheme (e.g., "data:image/png;base64,iVBOR...")
            if field_value.startswith('data:') and ',' in field_value:
                header, b64_data = field_value.split(',', 1)
                mime_type = header.split(';')[0].split(':')[1]

            # Fix incorrect padding, which can occur with base64 strings from web clients
            missing_padding = len(b64_data) % 4
            if missing_padding:
                b64_data += '=' * (4 - missing_padding)

            try:
                file_bytes = base64.b64decode(b64_data)
            except (TypeError, base64.binascii.Error):
                # If decoding fails, it's not a valid base64 string.
                # In a real app, you might want to raise a validation error here.
                return

            # If mime_type wasn't in the data URL, detect it with python-magic
            if not mime_type:
                mime_type = magic.from_buffer(file_bytes, mime=True)

            # Safely guess the extension, defaulting to an empty string if unknown
            ext = mimetypes.guess_extension(mime_type) or ''

            file_obj = io.BytesIO(file_bytes)
            file_obj.name = f"{related_field_name}{ext}"
            file_obj.content_type = mime_type
            file_obj.size = len(file_bytes)

            # now we need to upload the file
            instance = cls.create_from_file(file_obj, file_obj.name)
            setattr(related_instance, related_field_name, instance)

        elif isinstance(field_value, int):
            # assume file id
            instance = File.objects.get(id=field_value)
            setattr(related_instance, related_field_name, instance)
