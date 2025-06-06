from django.db import models
from mojo.models import MojoModel
import uuid
import hashlib
import mimetypes
from datetime import datetime


class File(models.Model, MojoModel):
    """
    File model representing uploaded files with metadata and storage information
    """

    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["view_fileman"]
        SEARCH_FIELDS = ["filename", "original_filename", "content_type"]
        SEARCH_TERMS = [
            "filename", "original_filename", "content_type",
            ("group", "group__name"),
            ("file_manager", "file_manager__name")]

        GRAPHS = {
            "default": {
                "graphs": {
                    "group": "basic",
                    "file_manager": "basic",
                    "uploaded_by": "basic"
                }
            },
            "list": {
                "graphs": {
                    "group": "basic",
                    "file_manager": "basic"
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

    uploaded_by = models.ForeignKey(
        "account.User",
        related_name="uploaded_files",
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
        help_text="Final filename used for storage"
    )

    original_filename = models.CharField(
        max_length=255,
        help_text="Original filename as uploaded by user"
    )

    file_path = models.TextField(
        help_text="Full path to file in storage backend"
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

    checksum = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="File checksum (MD5, SHA256, etc.)"
    )

    upload_token = models.CharField(
        max_length=64,
        unique=True,
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

    upload_url = models.TextField(
        blank=True,
        default="",
        help_text="Pre-signed URL for direct upload (temporary)"
    )

    upload_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the upload URL expires"
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

    class Meta:
        indexes = [
            models.Index(fields=['upload_status', 'created']),
            models.Index(fields=['file_manager', 'upload_status']),
            models.Index(fields=['group', 'is_active']),
            models.Index(fields=['content_type', 'is_active']),
            models.Index(fields=['upload_expires_at']),
        ]

    def __str__(self):
        return f"{self.original_filename} ({self.get_upload_status_display()})"

    def save(self, *args, **kwargs):
        """Custom save to generate upload token and set defaults"""
        if not self.upload_token:
            self.upload_token = self.generate_upload_token()

        if not self.filename:
            self.filename = self.generate_unique_filename()

        if not self.content_type:
            self.content_type = mimetypes.guess_type(self.filename)[0] or 'application/octet-stream'

        super().save(*args, **kwargs)

    @classmethod
    def generate_upload_token(cls):
        """Generate a unique upload token"""
        return hashlib.sha256(f"{uuid.uuid4()}{datetime.now()}".encode()).hexdigest()[:32]

    def generate_unique_filename(self):
        """Generate a unique filename for storage"""
        import os
        name, ext = os.path.splitext(self.original_filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        return f"{name}_{timestamp}_{unique_id}{ext}"

    def get_metadata(self, key, default=None):
        """Get a specific metadata value"""
        return self.metadata.get(key, default)

    def set_metadata(self, key, value):
        """Set a specific metadata value"""
        self.metadata[key] = value

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

    def mark_as_uploading(self):
        """Mark file as currently being uploaded"""
        self.upload_status = self.UPLOADING
        self.save(update_fields=['upload_status', 'modified'])

    def mark_as_completed(self, file_size=None, checksum=None):
        """Mark file upload as completed"""
        self.upload_status = self.COMPLETED
        if file_size:
            self.file_size = file_size
        if checksum:
            self.checksum = checksum
        self.save(update_fields=['upload_status', 'file_size', 'checksum', 'modified'])

    def mark_as_failed(self, error_message=None):
        """Mark file upload as failed"""
        self.upload_status = self.FAILED
        if error_message:
            self.set_metadata('error_message', error_message)
        self.save(update_fields=['upload_status', 'metadata', 'modified'])

    def mark_as_expired(self):
        """Mark file upload as expired"""
        self.upload_status = self.EXPIRED
        self.save(update_fields=['upload_status', 'modified'])

    def get_file_extension(self):
        """Get the file extension"""
        import os
        return os.path.splitext(self.original_filename)[1].lower()

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
