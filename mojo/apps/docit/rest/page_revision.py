import mojo.decorators as md
from ..models import PageRevision


@md.URL('page/revision')
@md.URL('page/revision/<int:pk>')
@md.custom_security("uses custom security")
def on_page_revision(request, pk=None):
    """
    Standard CRUD endpoints for PageRevision model

    GET /api/docit/page/revision - List page revisions
    POST /api/docit/page/revision - Create new page revision
    GET /api/docit/page/revision/<id> - Get single page revision
    PUT /api/docit/page/revision/<id> - Update page revision
    DELETE /api/docit/page/revision/<id> - Delete page revision
    """
    return PageRevision.on_rest_request(request, pk)
