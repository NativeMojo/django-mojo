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
        # Renditions are derived data produced by the renderer pipeline.
        # Direct create/delete via REST is not allowed — renditions are
        # managed through the parent File (cascade on delete, or the
        # `regenerate_renditions` action on File).
        CAN_CREATE = False
        CAN_DELETE = False
        DEFAULT_SORT = "-created"
        POST_SAVE_ACTIONS = ["share"]
        VIEW_PERMS = ["view_fileman", "manage_files", "files"]
        SAVE_PERMS = ["manage_files", "files"]
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

    shortlink_code = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="Code of the shortlink that wraps this rendition's download URL (tier 1, internal)"
    )

    # Share-action clamps mirrored from File so the rendition share handler
    # can apply the same policy via _mint_share_link.
    MAX_SHARE_EXPIRE_DAYS = 3650
    MAX_SHARE_NOTE_LEN = 512

    @property
    def file_manager(self):
        return self.original_file.file_manager

    @property
    def url(self):
        return self.generate_download_url()

    def get_direct_download_url(self):
        """Return the raw backend URL, bypassing any shortlink wrapping.
        Used by the shortlink resolver and as the fallback when shortlinks are disabled.
        """
        if self.file_manager.is_public:
            if not self.download_url:
                self.download_url = self.file_manager.backend.get_url(self.storage_path)
            return self.download_url
        return self.file_manager.backend.get_url(
            self.storage_path,
            self.file_manager.get_setting("urls_expire_in", 3600),
        )

    def generate_download_url(self):
        """Return the URL clients should use.

        Shortlink-aware when the shortlink app is installed and the file
        manager has `use_shortlinks` enabled. Falls back to the raw backend
        URL in all other cases.
        """
        from mojo.apps.fileman.models.file import (
            _shortlinks_enabled,
            _get_or_create_shortlink_url,
        )
        if not _shortlinks_enabled(self.file_manager):
            return self.get_direct_download_url()
        return _get_or_create_shortlink_url(rendition=self)

    def on_action_share(self, value):
        """Mint a new tier-2 share shortlink for this rendition.

        Triggered by {"share": true | {"expire_days": N, "track_clicks": bool, "note": "..."}}.
        """
        from mojo.apps.fileman.models.file import _mint_share_link
        return _mint_share_link(self, value)

    def on_rest_pre_delete(self):
        from mojo.apps.fileman.models.file import _delete_fileman_shortlinks
        _delete_fileman_shortlinks(rendition=self)
