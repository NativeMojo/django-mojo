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
    is_public = models.BooleanField(
        default=False,
        db_index=True,
        help_text=("Opt this book into anonymous public reading via the "
                   "/api/docit/public endpoints. Off by default — the "
                   "authenticated endpoints stay tenant-scoped either way.")
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
        # 'member' lets any member of the owning group read that group's docs
        # without a per-member grant — docs are group-visible by nature. It is
        # NOT a global grant: User.has_permission has no 'member' case, so the
        # flat (tenantless) branch stays closed and reads are confined to the
        # caller's own tenants. GROUP_FIELD is what engages that confinement.
        VIEW_PERMS = ['view_docit', 'manage_docit', 'docs', 'member']
        SAVE_PERMS = ['manage_docit', 'docs', 'owner']
        DELETE_PERMS = ['manage_docit', 'owner']
        GROUP_FIELD = 'group'
        CREATED_BY_OWNER_FIELD = 'created_by'
        UPDATED_BY_OWNER_FIELD = 'modified_by'
        POST_SAVE_ACTIONS = ["reindex"]
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
            },
            # Served to anonymous callers by the public endpoints. Owner,
            # config and permissions are deliberately absent — this graph is
            # pinned server-side precisely so a caller cannot ask for another.
            'public': {
                "fields": [
                    'id', 'title', 'slug', 'description', 'modified'
                ],
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

    def on_rest_pre_save(self, changed_fields, created):
        """A book must belong to a tenant, on create AND on every update.

        This is a DATA-INTEGRITY invariant, not a security guard. It used to be
        both: a group-less book once fell outside GROUP_FIELD confinement
        entirely, because the detail check kept whatever `?group=` the caller
        supplied and any member of any tenant passed it. Maestro item 953 fixed
        that in the framework — `_evaluate_permission` now binds request.group
        to the row's owning tenant unconditionally, so a null tenant clears the
        binding and the check falls through to the caller's GLOBAL permissions.

        What remains is the modelling rule: docit books are tenant-owned, and a
        group-less one is a broken row rather than a platform-wide one. The
        framework stamps `group` from request.group on create when the body
        omits it (this hook runs after that), so on create this only fires when
        there was nothing to infer.

        The check is deliberately NOT `if created` — `{"group": 0}` (or null,
        or "") on an update clears the FK with no permission gate of its own,
        which would leave an unowned book behind.
        """
        import mojo.errors as me
        if not self.group_id:
            raise me.ValueException("group is required")
        # is_public publishes to the anonymous internet, so it takes a
        # manage-level grant — ownership alone is not enough.
        if "is_public" in changed_fields and self.is_public:
            actor = self.active_user
            if actor is None or not actor.has_permission(["manage_docit", "docs"]):
                raise me.PermissionDeniedException(
                    "manage_docit or docs is required to publish a book")

    def on_action_reindex(self, value):
        """Queue knowledge-base embed jobs for every page of this book.

        Triggered by {"reindex": true} on save — a falsy value is a no-op
        (POST_SAVE_ACTIONS dispatches on key presence, not truthiness).
        No-op unless mojo.apps.docit_kb is installed.
        """
        from django.apps import apps
        enabled = apps.is_installed("mojo.apps.docit_kb")
        if not value or not enabled:
            return {"status": True, "data": {"queued": 0, "enabled": enabled}}
        from mojo.apps.docit_kb.services import knowledge
        queued = knowledge.reindex_book(self)
        return {"status": True, "data": {"queued": queued, "enabled": True}}

    def get_page_count(self):
        """Get total number of published pages in this book"""
        return self.pages.filter(is_published=True).count()

    def get_asset_count(self):
        """Get total number of assets in this book"""
        return self.assets.count()
