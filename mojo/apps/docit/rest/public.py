"""
Anonymous read endpoints for books that explicitly opted in (Book.is_public).

Public documentation is a legitimate use of docit, but it must be a decision
per book rather than the default. The authenticated CRUD endpoints stay
tenant-scoped through RestMeta; everything reachable without a session lives
here, where each query names its own conditions and the response graph is
pinned server-side.

Three things this module never does: trust a caller-supplied `graph`, serve a
book that did not opt in, or serve an unpublished page.
"""
import mojo.decorators as md
import mojo.errors as me
from mojo.serializers import serialize
from ..models import Book

# A public book's page list is served whole (clients build the nav tree from
# it), so it is capped rather than paginated. Books larger than this are not a
# shape this endpoint is meant to serve.
MAX_PUBLIC_PAGES = 500

# Anonymous callers are exempt from the global per-identity throttle, so these
# endpoints carry their own. Page reads render markdown per request, which is
# the expensive part.
PUBLIC_IP_LIMIT = 240


def _public_book(slug):
    """Resolve an opted-in, readable book — or None.

    Fails closed on every axis: the book opts in (`is_public`), is active,
    belongs to a group, and that group's whole ancestor chain is active, so a
    suspended tenant stops serving public content immediately.
    """
    if not slug:
        return None
    book = (Book.objects
            .filter(slug=slug, is_public=True, is_active=True, group__isnull=False)
            .select_related("group")
            .first())
    if book is None or not book.group.is_effectively_active():
        return None
    return book


def _require_public_book(slug):
    book = _public_book(slug)
    if book is None:
        # One response for "no such book", "not public", "inactive" and
        # "suspended tenant" — the public surface should not distinguish them.
        raise me.ValueException("Book not found", code=404, status=404)
    return book


@md.GET('public/book/<str:slug>')
@md.public_endpoint("Public documentation — book metadata for an opted-in book")
@md.rate_limit("docit_public_book", ip_limit=PUBLIC_IP_LIMIT)
def on_public_book(request, slug=None):
    """Book metadata for a book with is_public=True."""
    book = _require_public_book(slug)
    return serialize(book, graph="public")


@md.GET('public/pages')
@md.public_endpoint("Public documentation — published page list for an opted-in book")
@md.rate_limit("docit_public_pages", ip_limit=PUBLIC_IP_LIMIT)
def on_public_pages(request):
    """Published pages of a public book, flat. Clients build the tree from `parent`.

    `book` is the book slug.
    """
    book = _require_public_book(request.DATA.get("book", None))
    pages = (book.pages.filter(is_published=True)
             .order_by('-order_priority', 'title')[:MAX_PUBLIC_PAGES])
    return [serialize(page, graph="public_list") for page in pages]


@md.GET('public/page')
@md.public_endpoint("Public documentation — one published page of an opted-in book")
@md.rate_limit("docit_public_page", ip_limit=PUBLIC_IP_LIMIT)
def on_public_page(request):
    """One published page of a public book. `book` is the book slug, `slug` the page's."""
    book = _require_public_book(request.DATA.get("book", None))
    slug = request.DATA.get("slug", None)
    if not slug:
        raise me.ValueException("slug is required")
    page = book.pages.filter(slug=slug, is_published=True).first()
    if page is None:
        raise me.ValueException("Page not found", code=404, status=404)
    return serialize(page, graph="public")
