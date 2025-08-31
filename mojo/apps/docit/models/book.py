from django.db import models
from django.utils.text import slugify
from mojo.models import MojoModel
from mojo.helpers import logit


class Book(models.Model, MojoModel):
    """
    Top-level documentation collection

    A Book represents a complete documentation collection that can contain
    multiple pages organized hierarchically, along with associated assets.
    """

    # Basic fields
    title = models.CharField(max_length=200, help_text="Book title")
    slug = models.SlugField(
        unique=True,
        max_length=200,
        help_text="URL-friendly identifier"
    )
    description = models.TextField(
        blank=True,
        help_text="Brief description of the book content"
    )

    # Ordering and permissions
    order_priority = models.IntegerField(
        default=0,
        db_index=True,
        help_text="Higher values appear first in listings"
    )
    permissions = models.CharField(
        max_length=500,
        blank=True,
        help_text="Comma-separated permission strings for fine-grained access control"
    )

    # Configuration
    config = models.JSONField(
        default=dict,
        help_text="Book-specific settings, plugin configuration, and custom access rules"
    )

    # Status
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether this book is active and visible"
    )

    # Ownership and tracking
    group = models.ForeignKey(
        'account.Group',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        default=None,
        help_text="Owning group for this book"
    )
    user = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        help_text="Book owner for permission checks"
    )
    created_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='created_books',
        help_text="User who created this book"
    )
    modified_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='modified_books',
        null=True,
        default=None,
        help_text="User who last modified this book"
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
        ordering = ['-order_priority', 'title']
        verbose_name = 'Book'
        verbose_name_plural = 'Books'

    class RestMeta:
        VIEW_PERMS = ['all']
        SAVE_PERMS = ['manage_docit', 'owner']
        DELETE_PERMS = ['manage_docit', 'owner']
        CREATED_BY_OWNER_FIELD = 'created_by'
        UPDATED_BY_OWNER_FIELD = 'modified_by'
        GRAPHS = {
            'default': {
                "fields": [
                    'id', 'title', 'slug', 'description',
                    'is_active', 'created', 'modified'
                ],
                "graphs": {
                    "user": "basic",
                    "group": "basic",
                    "created_by": "basic",
                    "modified_by": "basic"
                }
            },
            'detail': {
                "fields": [
                    'id', 'title', 'slug', 'description', 'order_priority',
                    'config', 'is_active', 'created', 'modified',
                    'created_by', 'modified_by'
                ],
                "graphs": {
                    "user": "basic",
                    "group": "basic",
                    "created_by": "basic",
                    "modified_by": "basic"
                }
            },
            'list': {
                "fields": [
                    'id', 'title', 'slug', 'description', 'is_active'
                ],
                "graphs": {
                    # "user": "basic",
                    "group": "basic",
                    # "created_by": "basic",
                    # "modified_by": "basic"
                }
            }
        }

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        """Override save to auto-generate slug and log operations"""

        # Auto-generate slug from title if not provided
        if not self.slug:
            self.slug = slugify(self.title.replace('_', '-'))

            # Handle duplicate slugs by appending a counter
            counter = 1
            original_slug = self.slug
            while Book.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
                self.slug = f"{original_slug}-{counter}"
                counter += 1

        if (not hasattr(self, "user") or self.user is None) and self.created_by:
            self.user = self.created_by

        # Log the operation
        if self.pk:
            logit.info(f"Updating book: {self.title} (ID: {self.pk})")
        else:
            logit.info(f"Creating new book: {self.title}")

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Override delete to log the operation"""
        logit.info(f"Deleting book: {self.title} (ID: {self.pk})")
        super().delete(*args, **kwargs)

    def get_pages(self, published_only=True):
        """Get all pages in this book"""
        queryset = self.pages.all()
        if published_only:
            queryset = queryset.filter(is_published=True)
        return queryset.order_by('-order_priority', 'title')

    def get_root_pages(self, published_only=True):
        """Get top-level pages (no parent) in this book"""
        queryset = self.pages.filter(parent__isnull=True)
        if published_only:
            queryset = queryset.filter(is_published=True)
        return queryset.order_by('-order_priority', 'title')

    def get_assets(self):
        """Get all assets associated with this book"""
        return self.assets.all().order_by('-order_priority', 'id')

    def can_user_view(self, user):
        """
        Check if a user can view this book

        This provides fine-grained access control beyond the basic RestMeta permissions.
        The Book model handles detailed access logic here.
        """
        # Inactive books are not viewable
        if not self.is_active:
            return False

        # Owner can always view
        if self.user == user:
            return True

        # Group members can view if no specific restrictions
        if user and user.groups.filter(id=self.group.id).exists():
            return True

        # Check custom permissions if defined
        if self.permissions:
            # This is where we'd implement custom permission logic
            # For now, return True for basic implementation
            return True

        # Default to allowing view (since RestMeta has public view)
        return True

    def get_page_count(self):
        """Get total number of published pages in this book"""
        return self.pages.filter(is_published=True).count()

    def get_asset_count(self):
        """Get total number of assets in this book"""
        return self.assets.count()
