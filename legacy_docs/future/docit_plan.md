# DocIt - Complete Django Models Design

## Django Models Implementation

### Core Models with Complete Field Definitions

```python
# books/models.py

from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify

class Book(models.Model):
    """
    Top-level documentation collection
    """
    # Basic fields
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=200)
    description = models.TextField(blank=True)

    # Ordering and permissions
    order_priority = models.IntegerField(default=0, help_text="Higher values appear first")
    permissions = models.CharField(
        max_length=500,
        blank=True,
        help_text="Comma-separated permission strings"
    )

    # Configuration
    config = models.JSONField(
        default=dict,
        help_text="Plugin settings and book configuration"
    )

    # Ownership and tracking
    group = models.ForeignKey(
        'account.Group',
        on_delete=models.CASCADE,
        help_text="Owning group for this book"
    )
    created_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='created_books'
    )
    modified_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='modified_books'
    )

    # Timestamps
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    # Status
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)

class Page(models.Model):
    """
    Individual documentation page within a book
    """
    # Relationships
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='pages')
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='children',
        help_text="Parent page for hierarchical organization"
    )

    # Basic fields
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200)  # unique within book
    content = models.TextField(help_text="Raw markdown content")

    # Ordering and metadata
    order_priority = models.IntegerField(default=0, help_text="Higher values appear first")
    metadata = models.JSONField(
        default=dict,
        help_text="Frontmatter and additional page metadata"
    )

    # Tracking
    created_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='created_pages'
    )
    modified_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='modified_pages'
    )

    # Timestamps
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    # Status
    is_published = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.book.title} / {self.title}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)

    @property
    def full_path(self):
        """Return hierarchical path like: parent/child/grandchild"""
        if self.parent:
            return f"{self.parent.full_path}/{self.slug}"
        return self.slug

    @property
    def html(self):
        """Return HTML representation of page content"""
        return None

    @property
    def ast(self):
        """Return AST representation of page content"""
        return None

    def get_children(self):
        """Get direct child pages"""
        return self.children.filter(is_published=True)

    def get_descendants(self):
        """Get all descendant pages (recursive)"""
        descendants = []
        for child in self.get_children():
            descendants.append(child)
            descendants.extend(child.get_descendants())
        return descendants

class PageRevision(models.Model):
    """
    Version history for pages
    """
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name='revisions')
    content = models.TextField(help_text="Markdown content snapshot")
    version = models.IntegerField(help_text="Sequential version number")

    # Change tracking
    created_by = models.ForeignKey('account.User', on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)

    # Optional metadata
    change_summary = models.CharField(
        max_length=200,
        blank=True,
        help_text="Brief description of changes"
    )

    def __str__(self):
        return f"{self.page.title} v{self.version}"

class Asset(models.Model):
    """
    Files associated with a book (images, documents, etc.)
    """
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='assets')
    file = models.ForeignKey('fileman.File', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')

    # Organization
    order_priority = models.IntegerField(
        default=0,
        help_text="Higher values appear first in asset lists"
    )

    # Optional metadata
    alt_text = models.CharField(
        max_length=200,
        blank=True,
        help_text="Alternative text for images"
    )
    description = models.TextField(blank=True)

    # Tracking
    created_by = models.ForeignKey('account.User', on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.book.title} / {self.filename}"

    @property
    def is_image(self):
        """Check if asset is an image"""
        return self.file.category == 'image'


## Model Relationships Summary

### Primary Relationships
- **Book** ←→ **account.Group** (Many Books to One Group)
- **Book** ←→ **Page** (One Book to Many Pages)
- **Page** ←→ **Page** (Self-referencing hierarchy)
- **Book** ←→ **Asset** (One Book to Many Assets)
- **Page** ←→ **PageRevision** (One Page to Many Revisions)

### User Tracking Relationships
- **Book** ←→ **account.User** (created_by, modified_by)
- **Page** ←→ **account.User** (created_by, modified_by)
- **PageRevision** ←→ **account.User** (created_by only)
- **Asset** ←→ **account.User** (created_by only)

## Keep the REST API inline with what we do with simple CRUD
@md.URL('book')
@md.URL('book/<int:pk>')
def on_book(request, pk=None):
    return Book.on_rest_request(request, pk)

@md.URL('page')
@md.URL('page/<int:pk>')
def on_page(request, pk=None):
    return Page.on_rest_request(request, pk)

@md.URL('page/revision')
@md.URL('page/revision/<int:pk>')
def on_page_revision(request, pk=None):
    return PageRevision.on_rest_request(request, pk)

@md.URL('book/asset')
@md.URL('book/asset/<int:pk>')
def on_book_asset(request, pk=None):
    return Asset.on_rest_request(request, pk)


define special graphs on Page to show just markdown or include "html" or include "ast"

The PLAN:
Phase 1 - High Level Plan
Phase 2 - Implementation
Phase 3 - Write Tests [@tests](file:///Users/ians/Projects/mojo/django-mojo/tests/) using our testit framework
