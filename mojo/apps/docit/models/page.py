from django.db import models
from django.utils.text import slugify
from mojo.models import MojoModel
from mojo.helpers import logit


class Page(models.Model, MojoModel):
    """
    Individual documentation page within a book

    Pages can be organized hierarchically with parent-child relationships
    and support version control through PageRevision records.
    """

    # Relationships
    book = models.ForeignKey(
        'docit.Book',
        on_delete=models.CASCADE,
        related_name='pages',
        help_text="Book this page belongs to"
    )
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='children',
        help_text="Parent page for hierarchical organization"
    )

    # Basic fields
    title = models.CharField(
        max_length=200,
        help_text="Page title"
    )
    slug = models.SlugField(
        max_length=200,
        db_index=True,
        help_text="URL-friendly identifier (unique within book)"
    )
    content = models.TextField(
        help_text="Raw markdown content"
    )

    # Ordering and metadata
    order_priority = models.IntegerField(
        default=0,
        db_index=True,
        help_text="Higher values appear first in listings"
    )
    metadata = models.JSONField(
        default=dict,
        help_text="Frontmatter and additional page metadata"
    )

    # Status
    is_published = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether this page is published and visible"
    )

    # Ownership and tracking (inherits from book permissions)
    user = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        help_text="Page owner (inherited from book for permissions)"
    )
    created_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='created_pages',
        help_text="User who created this page"
    )
    modified_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='modified_pages',
        null=True,
        default=None,
        help_text="User who last modified this page"
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
        verbose_name = 'Page'
        verbose_name_plural = 'Pages'
        # Ensure slug is unique within a book
        unique_together = ['book', 'slug']

    class RestMeta:
        VIEW_PERMS = ['all']
        SAVE_PERMS = ['manage_docit', 'owner']
        DELETE_PERMS = ['manage_docit', 'owner']
        CREATED_BY_OWNER_FIELD = 'created_by'
        UPDATED_BY_OWNER_FIELD = 'modified_by'
        GRAPHS = {
            'default': {
                "graphs": {
                    "user": "basic",
                    "book": "default",
                    "created_by": "basic",
                    "modified_by": "basic"
                }
            },
            'detail': {
                "fields": [
                    'id', 'title', 'slug', 'content', 'order_priority',
                    'metadata', 'is_published', 'created', 'modified',
                    'book', 'parent'
                ],
                "graphs": {
                    "user": "basic",
                    "book": "default",
                    "created_by": "basic",
                    "modified_by": "basic"
                }
            },
            'list': {
                "fields": [
                    'id', 'title', 'slug', 'is_published', 'order_priority', "metadata"
                ],
                "graphs": {
                    # "user": "basic",
                    "book": "default",
                    # "created_by": "basic",
                    "modified_by": "basic"
                }
            },
            'content_only': {
                "fields": [
                'id', 'title', 'content'
                ],
            },
            'html': {
                "fields": [
                    'id', 'title', 'slug', 'order_priority',
                    'metadata', 'is_published', 'created', 'modified',
                ],
                "extra": ["html"],
                "graphs": {
                    "modified_by": "basic"
                }
            },
            'tree': {
                "fields": [
                'id', 'title', 'slug', 'order_priority', 'parent', 'children'
                ],
            }
        }

    def __str__(self):
        return f"{self.book.title} / {self.title}"

    def save(self, *args, **kwargs):
        """Override save to auto-generate slug, validate hierarchy, and log operations"""

        # Auto-generate slug from title if not provided
        if not self.slug:
            self.slug = slugify(self.title.replace('_', '-'))

            # Handle duplicate slugs within the same book
            counter = 1
            original_slug = self.slug
            while Page.objects.filter(
                book=self.book,
                slug=self.slug
            ).exclude(pk=self.pk).exists():
                self.slug = f"{original_slug}-{counter}"
                counter += 1

        if (not hasattr(self, "user") or self.user is None) and self.created_by:
            self.user = self.created_by

        # Validate parent relationship (prevent circular references)
        if self.parent:
            if self.parent == self:
                raise ValueError("A page cannot be its own parent")

            if self.pk and self._would_create_cycle(self.parent):
                raise ValueError("Parent relationship would create a circular reference")

            # Ensure parent belongs to same book
            if self.parent.book != self.book:
                raise ValueError("Parent page must belong to the same book")

        # Inherit user from book if not set
        if not self.user_id and self.book_id:
            self.user = self.book.user

        # Log the operation
        if self.pk:
            logit.info(f"Updating page: {self.title} in book {self.book.title} (ID: {self.pk})")
        else:
            logit.info(f"Creating new page: {self.title} in book {self.book.title}")

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Override delete to log the operation"""
        logit.info(f"Deleting page: {self.title} from book {self.book.title} (ID: {self.pk})")
        super().delete(*args, **kwargs)

    def _would_create_cycle(self, potential_parent):
        """Check if setting potential_parent as parent would create a cycle"""
        current = potential_parent
        while current:
            if current == self:
                return True
            current = current.parent
        return False

    @property
    def full_path(self):
        """Return hierarchical path like: parent/child/grandchild"""
        if self.parent:
            return f"{self.parent.full_path}/{self.slug}"
        return self.slug

    @property
    def html(self):
        """
        Renders the Markdown content of the page to HTML.
        """
        from mojo.apps.docit.services.markdown import MarkdownRenderer
        renderer = MarkdownRenderer()
        rendered_html = renderer.render(self.content)
        return rendered_html

    @property
    def ast(self):
        """
        Return AST representation of page content

        This will be implemented in Phase 2 with markdown processing
        """
        return None

    def get_children(self):
        """Get direct child pages (published only by default)"""
        return self.children.filter(is_published=True).order_by('-order_priority', 'title')

    def get_all_children(self, include_unpublished=False):
        """Get direct child pages with option to include unpublished"""
        queryset = self.children.all()
        if not include_unpublished:
            queryset = queryset.filter(is_published=True)
        return queryset.order_by('-order_priority', 'title')

    def get_descendants(self):
        """Get all descendant pages (recursive, published only)"""
        descendants = []
        for child in self.get_children():
            descendants.append(child)
            descendants.extend(child.get_descendants())
        return descendants

    def get_ancestors(self):
        """Get all ancestor pages from root to immediate parent"""
        ancestors = []
        current = self.parent
        while current:
            ancestors.insert(0, current)  # Insert at beginning for correct order
            current = current.parent
        return ancestors

    def get_breadcrumbs(self):
        """Get breadcrumb trail including this page"""
        breadcrumbs = self.get_ancestors()
        breadcrumbs.append(self)
        return breadcrumbs

    def get_depth(self):
        """Get the depth level in the hierarchy (0 for root pages)"""
        depth = 0
        current = self.parent
        while current:
            depth += 1
            current = current.parent
        return depth

    def create_revision(self, user, change_summary=""):
        """Create a new revision record for this page"""
        from .page_revision import PageRevision

        # Get the next version number
        last_revision = self.revisions.order_by('-version').first()
        next_version = (last_revision.version + 1) if last_revision else 1

        revision = PageRevision.objects.create(
            page=self,
            content=self.content,
            version=next_version,
            change_summary=change_summary,
            created_by=user,
            user=self.user
        )

        logit.info(f"Created revision v{next_version} for page: {self.title}")
        return revision

    def get_latest_revision(self):
        """Get the most recent revision"""
        return self.revisions.order_by('-version').first()

    def get_revision_count(self):
        """Get total number of revisions for this page"""
        return self.revisions.count()
