from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit


class Asset(models.Model, MojoModel):
    """
    Files associated with a book (images, documents, etc.)

    Assets provide a way to attach files to documentation books,
    with support for organization, metadata, and access control.
    """

    # Relationships
    book = models.ForeignKey(
        'docit.Book',
        on_delete=models.CASCADE,
        related_name='assets',
        help_text="Book this asset belongs to"
    )
    file = models.ForeignKey(
        'fileman.File',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='docit_assets',
        help_text="Associated file from fileman"
    )

    # Organization
    order_priority = models.IntegerField(
        default=0,
        db_index=True,
        help_text="Higher values appear first in asset lists"
    )

    # Optional metadata
    alt_text = models.CharField(
        max_length=200,
        blank=True,
        help_text="Alternative text for images and accessibility"
    )
    description = models.TextField(
        blank=True,
        help_text="Detailed description of the asset"
    )

    # Ownership and tracking (inherits from book permissions)
    user = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        help_text="Asset owner (inherited from book for permissions)"
    )
    created_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='created_assets',
        help_text="User who created this asset"
    )

    # Standard MOJO timestamps
    created = models.DateTimeField(
        auto_now_add=True,
        editable=False,
        db_index=True
    )
    modified = models.DateTimeField(
        auto_now=True,
        db_index=True
    )

    class Meta:
        ordering = ['-order_priority', 'id']
        verbose_name = 'Asset'
        verbose_name_plural = 'Assets'

    class RestMeta:
        # See Book.RestMeta for why 'member' is here and what it does not grant.
        VIEW_PERMS = ['view_docit', 'manage_docit', 'docs', 'member']
        SAVE_PERMS = ['manage_docit', 'docs', 'owner']
        DELETE_PERMS = ['manage_docit', 'owner']
        GROUP_FIELD = 'book__group'
        CREATED_BY_OWNER_FIELD = 'created_by'
        GRAPHS = {
            'default': {
                "fields": [
                    'id', 'alt_text', 'description', 'order_priority', 'created'
                ],
                "graphs": {
                    "user": "basic",
                    "book": "default",
                    "created_by": "basic",
                    "file": "basic"
                }
            },
            'detail': {
                "fields": [
                    'id', 'alt_text', 'description', 'order_priority',
                    'file', 'book', 'created', 'created_by'
                ],
                "graphs": {
                    "user": "basic",
                    "book": "default",
                    "created_by": "basic",
                    "file": "basic"
                }
            },
            'list': {
                "fields": [
                    'id', 'alt_text', 'order_priority'
                ],
                "graphs": {
                    "user": "basic",
                    "book": "default",
                    "created_by": "basic",
                    "file": "basic"
                }
            },
            'file_info': {
                "fields": [
                    'id', 'alt_text', 'file', 'order_priority'
                ],
                "graphs": {
                    "user": "basic",
                    "book": "default",
                    "created_by": "basic",
                    "file": "basic"
                }
            }
        }

    def __str__(self):
        if self.file:
            return f"{self.book.title} / {self.filename}"
        return f"{self.book.title} / Asset #{self.id}"

    def on_rest_pre_save(self, changed_fields, created):
        """An asset must have a book — see Page.on_rest_pre_save."""
        import mojo.errors as me
        if created and not self.book_id:
            raise me.ValueException("book is required")

    def save(self, *args, **kwargs):
        """Override save to inherit user from book and log operations"""

        # Inherit user from book if not set
        if not self.user_id and self.book_id:
            self.user = self.book.user

        # Log the operation
        if self.pk:
            logit.info(f"Updating asset in book {self.book.title} (ID: {self.pk})")
        else:
            logit.info(f"Creating new asset in book {self.book.title}")

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Override delete to log the operation"""
        logit.info(f"Deleting asset from book {self.book.title} (ID: {self.pk})")
        super().delete(*args, **kwargs)

    @property
    def filename(self):
        """Get the filename from the associated file"""
        if self.file:
            return self.file.name
        return None

    @property
    def file_size(self):
        """Get the file size from the associated file"""
        if self.file:
            return self.file.size
        return None

    @property
    def file_type(self):
        """Get the file type/category from the associated file"""
        if self.file:
            return self.file.category
        return None

    @property
    def is_image(self):
        """Check if asset is an image"""
        if self.file:
            return self.file.category == 'image'
        return False

    @property
    def is_document(self):
        """Check if asset is a document"""
        if self.file:
            return self.file.category == 'document'
        return False

    @property
    def file_url(self):
        """Get the URL to access the file"""
        if self.file:
            return self.file.url
        return None

    @property
    def thumbnail_url(self):
        """Get thumbnail URL for images"""
        if self.file and self.is_image:
            # This assumes fileman has thumbnail support
            return getattr(self.file, 'thumbnail_url', None)
        return None

    def get_display_name(self):
        """Get the best display name for this asset"""
        if self.alt_text:
            return self.alt_text
        if self.filename:
            return self.filename
        return f"Asset #{self.id}"

    def get_file_extension(self):
        """Get file extension from filename"""
        if self.filename:
            return self.filename.split('.')[-1].lower() if '.' in self.filename else ''
        return None

    def get_mime_type(self):
        """Get MIME type from the file"""
        if self.file:
            return getattr(self.file, 'mime_type', None)
        return None
