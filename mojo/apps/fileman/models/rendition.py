from django.db import models
from mojo.models import MojoModel
import uuid
import hashlib
import mimetypes
from datetime import datetime
import os
from mojo.apps.fileman import utils
from mojo.apps.fileman.models import FileManager
from typing import Text


class FileRendition(models.Model, MojoModel):
    """
    File model representing uploaded files with metadata and storage information
    """

    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["view_fileman", "manage_files"]
        SEARCH_FIELDS = ["filename", "content_type"]
        SEARCH_TERMS = [
            "filename",  "content_type",
            ("group", "group__name"),
            ("file_manager", "file_manager__name")]

        GRAPHS = {
            "upload": {
                "fields": ["id", "filename", "content_type", "file_size"],
            },
            "default": {
                "extra": ["url"],
            },
            "list": {
                "extra": ["url"],
            }
        }

    # Upload status choices
    PENDING = 'pending'
    RENDERING = 'rendering'
    COMPLETED = 'completed'
    FAILED = 'failed'
    EXPIRED = 'expired'

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)

    original_file = models.ForeignKey(
        "fileman.File",
        related_name="file_renditions",
        on_delete=models.CASCADE,
        help_text="The parent file"
    )

    filename = models.CharField(
        max_length=255,
        db_index=True,
        help_text="rendition filename"
    )

    storage_path = models.TextField(
        help_text="Storage path and filename",
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
        help_text="MIME type of the file"
    )

    category = models.CharField(
        max_length=255,
        help_text="A category for the file, like 'image', 'document', 'video', etc."
    )

    role = models.CharField(
        max_length=255,
        db_index=True,
        help_text="The role of the file, like 'thumbnail', 'preview', 'full', etc."
    )

    upload_status = models.CharField(
        max_length=32,
        default=PENDING,
        db_index=True,
        help_text="Current status of rendering"
    )

    @property
    def file_manager(self):
        return self.original_file.file_manager

    @property
    def url(self):
        return self.generate_download_url()

    def generate_download_url(self):
        if self.download_url:
            return self.download_url
        if self.file_manager.is_public:
            self.download_url = self.file_manager.backend.get_url(self.storage_path)
            return self.download_url
        return self.file_manager.backend.get_url(self.storage_path, self.get_setting("urls_expire_in", 3600))
