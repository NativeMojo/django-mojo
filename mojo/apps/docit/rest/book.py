import mojo.decorators as md
from ..models import Book


@md.URL('book')
@md.URL('book/<int:pk>')
@md.custom_security("uses custom security")
def on_book(request, pk=None):
    """
    Standard CRUD endpoints for Book model

    GET /api/docit/book - List books
    POST /api/docit/book - Create new book
    GET /api/docit/book/<id> - Get single book
    PUT /api/docit/book/<id> - Update book
    DELETE /api/docit/book/<id> - Delete book
    """
    return Book.on_rest_request(request, pk)


@md.URL('book/slug/<str:slug>')
@md.custom_security("uses custom security")
def on_book_by_slug(request, slug=None):
    return Book.objects.get(slug=slug).on_rest_get(request)
