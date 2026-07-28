# DocIt — Django Developer Reference

DocIt is a documentation/wiki system with hierarchical pages, Markdown rendering, version history, and assets.

## Models

### Book

Top-level documentation collection.

```python
from mojo.apps.docit.models import Book

book = Book.objects.create(
    title="API Documentation",
    group=group,          # required — the book's tenant (see Permissions)
    user=user,
    is_active=True,
    is_public=False,      # opt in to anonymous reading; default False
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

## Permissions and tenant scoping

Every docit model is **group-scoped**. Reads are confined to the caller's own
tenants by the standard framework machinery — there is no docit-specific
permission code.

```python
class RestMeta:
    VIEW_PERMS = ["view_docit", "manage_docit", "docs", "member"]
    SAVE_PERMS = ["manage_docit", "docs", "owner"]
    DELETE_PERMS = ["manage_docit", "owner"]
    GROUP_FIELD = "group"        # Book; see the table below for the others
```

| Model | `GROUP_FIELD` |
|---|---|
| `Book` | `group` |
| `Page` | `book__group` |
| `Asset` | `book__group` |
| `PageRevision` | `page__book__group` |

`GROUP_FIELD` is what engages tenant scoping: it narrows list queries, and on
detail reads the framework rebinds `request.group` to the **instance's** owning
group, so a caller cannot reach another tenant's row by passing their own
`?group=`.

`"member"` means *any member of the owning group may read that group's docs*,
with no per-member grant — documentation is group-visible by nature. Two things
to know about it:

- It is **not** a global grant. `User.has_permission` has no `"member"` case,
  so the flat (tenantless) branch stays closed and a user with no membership
  reads nothing. Reserve the literal string `member` as a *global* user
  permission for nothing else — granting it platform-wide would widen these
  reads to every tenant.
- `ApiKey.has_permission` **does** auto-satisfy `"member"`, so any
  group-scoped API key of a tenant — including one minted with an empty
  permissions dict — can read that tenant's books, drafts, revisions and
  assets. It stays confined to the key's own group tree.

Members can read their own tenant's **unpublished** pages; `is_published` gates
the public endpoints only. Writing still requires `manage_docit` / `docs` /
ownership — widening VIEW did not widen SAVE.

`Book.group` is required at the REST layer (`on_rest_pre_save` raises a 400
when a create would leave it unset), and `Page`/`Asset` likewise require a
`book`. That last check is what turns a denied cross-tenant FK attach — the
framework skips the assignment silently — into a clean 400 instead of an
`IntegrityError` 500.

## REST Endpoints

```python
@md.URL('page')
@md.URL('page/<int:pk>')
@md.uses_model_security(Page)          # required — RestMeta does the gating
def on_page(request, pk=None):
    return Page.on_rest_request(request, pk)
```

Endpoints that resolve an instance themselves must hand it to
`on_rest_handle_get`, never to `on_rest_get` — the latter only serializes and
applies no permission check at all:

```python
@md.URL('page/slug/<str:slug>')
@md.uses_model_security(Page)
def on_page_by_slug(request, slug=None):
    # `book` is required: Page.slug is unique per book, not globally.
    page = Page.objects.filter(slug=slug, book_id=book_id).first()
    if page is None:
        raise me.ValueException("Page not found", code=404, status=404)
    return Page.on_rest_handle_get(request, page)
```

## Public documentation (opt-in)

Set `Book.is_public = True` to publish a book to anonymous readers. Nothing
else changes: the authenticated endpoints stay tenant-scoped, and anonymous
reading is served only by the dedicated `public/*` endpoints in
`mojo/apps/docit/rest/public.py`.

Those endpoints serve a book only when it opts in, is active, belongs to a
group, and that group's whole ancestor chain is active — a suspended tenant
stops serving public content immediately. They serve published pages only, and
they pin the response graph server-side (`public` / `public_list`), so a caller
cannot widen the response with `?graph=detail`.

Three further constraints on this surface, because it is reachable without a
session:

- The `public` page graph renders **`html_safe`** (escaped), never `html`.
  `Page.html` deliberately preserves raw HTML from the page's markdown — fine
  for authors inside a tenant, not for anonymous readers.
- `public/pages` is capped at `MAX_PUBLIC_PAGES` and each endpoint carries its
  own `@md.rate_limit`; anonymous callers are exempt from the global
  per-identity throttle, so a public endpoint has to bring its own.
- Setting `is_public` requires `manage_docit` / `docs`. `SAVE_PERMS` also
  admits `owner`, and publishing a book to the internet should not be a
  power that ownership alone confers — `Book.on_rest_pre_save` enforces this.

Group-less books are refused outright: `Book.on_rest_pre_save` requires a
group on **every** save, not just create. A book with no group resolves to no
tenant, and the detail permission check then keeps the caller's own `?group=`
rather than the row's — so a null group is an escape from tenant scoping, not
merely an unset field.

## Knowledge-base search

`search_any()` takes a `groups` argument that confines results to those
tenants. `None` means unrestricted and is correct only for internal/system
callers; request-facing callers pass `visible_groups()`:

```python
from mojo.apps.docit.services.search import search_any, visible_groups

groups = visible_groups(user=request.user, api_key=getattr(request, "api_key", None))
found = search_any(query, groups=groups)
```

`visible_groups()` mirrors the list path: a global `view_docit` / `manage_docit`
/ `docs` holder gets `None` (unrestricted), anyone else is narrowed to their own
groups, an `ApiKey` is never unrestricted, and an anonymous caller gets nothing.

## Render Endpoint

`POST /api/docit/render` renders arbitrary Markdown to HTML server-side. Requires authentication.

```python
from mojo.apps.docit.services.markdown import MarkdownRenderer

renderer = MarkdownRenderer()
html = renderer.render("# Hello\n\n```python\nprint('hi')\n```")
```

The REST endpoint exposes the same renderer:

```
POST /api/docit/render
Authorization: Bearer <token>
Content-Type: application/json

{"markdown": "# Hello World"}
```

Response:

```json
{"status": true, "html": "<h1>Hello World</h1>\n"}
```

## Markdown Plugins

The `MarkdownRenderer` supports:
- Syntax highlighting for code blocks via Pygments (`monokai` theme, `highlight` CSS class)
- Custom plugins via `mojo/apps/docit/services/markdown.py`

### HighlightRenderer — invalid language fallback

`HighlightRenderer.block_code` now catches `ClassNotFound` from Pygments when an unrecognized language name is used in a fenced code block. Instead of raising an exception, it falls back to a plain `<pre>` block. This means markdown like ` ```notareallanguage ` renders safely without errors.

## Circular Reference Prevention

The Page model prevents circular parent hierarchies:
```python
# This will raise ValueError
child.parent = child   # "A page cannot be its own parent"
grandchild.parent = child_of_grandchild  # cycle detection
```

## Knowledge Base (optional — mojo.apps.docit_kb)

Installing the optional `mojo.apps.docit_kb` app turns DocIt into a
searchable knowledge base: pages are chunked on heading boundaries, embedded
via AWS Bedrock (pgvector storage, HNSW index), and served through a hybrid
vector + full-text search — exposed as `GET/POST /api/docit/search` and as
the assistant's `search_docs` tool. Without the app, the same endpoint falls
back to page-level Postgres full-text search with no extra schema or
dependencies.

See [knowledge.md](knowledge.md) for enabling, settings
(`EMBEDDINGS_*` / `BEDROCK_*`), architecture, and the reindex/backfill flow.
