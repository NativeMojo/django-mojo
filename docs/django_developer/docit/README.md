# DocIt — Django Developer Reference

DocIt is a documentation/wiki system with hierarchical pages, Markdown rendering, version history, and assets.

## Models

### Book

Top-level documentation collection.

```python
from mojo.apps.docit.models import Book

book = Book.objects.create(
    title="API Documentation",
    group=group,
    user=user,
    is_published=True
)
```

### Page

Hierarchical pages within a book, with Markdown content.

```python
from mojo.apps.docit.models import Page

# Create a root page
page = Page.objects.create(
    book=book,
    title="Getting Started",
    content="# Getting Started\n\nWelcome to the API...",
    is_published=True
)

# Create a child page
child = Page.objects.create(
    book=book,
    title="Authentication",
    content="## Authentication\n\nUse Bearer tokens...",
    parent=page,
    is_published=True
)

# Render to HTML
html = page.html   # Markdown → HTML via MarkdownRenderer

# Navigation
ancestors = page.get_ancestors()    # list of Page from root to parent
children = page.get_children()      # direct children (published only)
breadcrumbs = page.get_breadcrumbs() # ancestors + self
depth = page.get_depth()            # 0 = root, 1 = child, etc.
path = page.full_path               # "parent-slug/child-slug"
```

### PageRevision

Version history for page content.

```python
# Create a revision before editing
revision = page.create_revision(user=request.user, change_summary="Updated auth section")

# Get revision history
revisions = page.revisions.order_by('-version')
latest = page.get_latest_revision()
count = page.get_revision_count()
```

## Key Fields — Page

| Field | Type | Description |
|---|---|---|
| `book` | FK → Book | Parent book |
| `parent` | FK → Page (self) | Parent page (null = root) |
| `title` | CharField | Page title |
| `slug` | SlugField | Auto-generated URL slug (unique within book) |
| `content` | TextField | Markdown content |
| `is_published` | BooleanField | Published flag |
| `order_priority` | IntegerField | Sort order (higher = first) |
| `user` | FK → User | Owner |
| `created_by` | FK → User | Original creator |

## RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["all"]        # public reading
    SAVE_PERMS = ["manage_docit", "owner"]
    DELETE_PERMS = ["manage_docit"]
    CAN_DELETE = True
    GRAPHS = {
        "list": {"fields": ["id", "title", "slug", "is_published", "order_priority", "parent"]},
        "default": {"fields": ["id", "title", "slug", "content", "is_published", "created", "modified"]},
        "html": {"extra": ["html"]},      # includes rendered HTML
        "tree": {"extra": ["children"]},  # hierarchical with children
    }
```

## REST Endpoints

```python
@md.URL('page')
@md.URL('page/<int:pk>')
def on_page(request, pk=None):
    return Page.on_rest_request(request, pk)

@md.URL('page/slug/<str:slug>')
def on_page_by_slug(request, slug=None):
    return Page.objects.get(slug=slug).on_rest_get(request)
```

## Markdown Plugins

The `MarkdownRenderer` supports:
- Table of contents generation
- Syntax highlighting for code blocks
- Custom plugins via `mojo/apps/docit/services/markdown.py`

## Circular Reference Prevention

The Page model prevents circular parent hierarchies:
```python
# This will raise ValueError
child.parent = child   # "A page cannot be its own parent"
grandchild.parent = child_of_grandchild  # cycle detection
```
