import mojo.decorators as md
import mojo.errors as me
from ..models import Book


@md.URL('book')
@md.URL('book/<int:pk>')
@md.uses_model_security(Book)
def on_book(request, pk=None):
    """
    Standard CRUD endpoints for Book model

    GET /api/docit/book - List books (scoped to the caller's tenants)
    POST /api/docit/book - Create new book
    GET /api/docit/book/<id> - Get single book
    PUT /api/docit/book/<id> - Update book
    DELETE /api/docit/book/<id> - Delete book
    """
    return Book.on_rest_request(request, pk)


@md.URL('book/slug/<str:slug>')
@md.uses_model_security(Book)
def on_book_by_slug(request, slug=None):
    """Get one book by its (globally unique) slug.

    Resolve, then delegate: on_rest_handle_get applies VIEW_PERMS to the
    instance, which is what confines this to the caller's tenants. The old
    version called on_rest_get directly — that method only serializes, so this
    endpoint answered anyone, unauthenticated, with any book.
    """
    book = Book.objects.filter(slug=slug).first()
    if book is None:
        raise me.ValueException("Book not found", code=404, status=404)
    return Book.on_rest_handle_get(request, book)
