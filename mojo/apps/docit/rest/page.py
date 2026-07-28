import mojo.decorators as md
import mojo.errors as me
from ..models import Page


@md.URL('page')
@md.URL('page/<int:pk>')
@md.uses_model_security(Page)
def on_page(request, pk=None):
    """
    Standard CRUD endpoints for Page model

    GET /api/docit/page - List pages (scoped to the caller's tenants)
    POST /api/docit/page - Create new page
    GET /api/docit/page/<id> - Get single page
    PUT /api/docit/page/<id> - Update page
    DELETE /api/docit/page/<id> - Delete page
    """
    return Page.on_rest_request(request, pk)


@md.URL('page/slug/<str:slug>')
@md.uses_model_security(Page)
def on_page_by_slug(request, slug=None):
    """Get one page by slug, within a named book.

    `book` (id or slug) is required: Page.slug is unique per book, not
    globally, so a bare slug is ambiguous by design — the old `.get(slug=...)`
    raised MultipleObjectsReturned (a 500) as soon as two books shared a page
    name, and DoesNotExist (also a 500) for anything unknown.
    """
    book = request.DATA.get("book", None)
    if book is None or book == "":
        raise me.ValueException("book is required (page slugs are unique within a book)")
    qs = Page.objects.filter(slug=slug)
    if isinstance(book, int) or (isinstance(book, str) and book.isdigit()):
        qs = qs.filter(book_id=int(book))
    else:
        qs = qs.filter(book__slug=book)
    page = qs.first()
    if page is None:
        raise me.ValueException("Page not found", code=404, status=404)
    return Page.on_rest_handle_get(request, page)
