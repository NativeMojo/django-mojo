import mojo.decorators as md
from ..models import Asset


@md.URL('book/asset')
@md.URL('book/asset/<int:pk>')
@md.custom_security("uses custom security")
def on_book_asset(request, pk=None):
    """
    Standard CRUD endpoints for Asset model

    GET /api/docit/book/asset - List book assets
    POST /api/docit/book/asset - Create new book asset
    GET /api/docit/book/asset/<id> - Get single book asset
    PUT /api/docit/book/asset/<id> - Update book asset
    DELETE /api/docit/book/asset/<id> - Delete book asset
    """
    return Asset.on_rest_request(request, pk)
