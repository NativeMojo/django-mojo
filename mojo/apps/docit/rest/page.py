import mojo.decorators as md
from ..models import Page


@md.URL('page')
@md.URL('page/<int:pk>')
@md.custom_security("docit custom security")
def on_page(request, pk=None):
    """
    Standard CRUD endpoints for Page model

    GET /api/docit/page - List pages
    POST /api/docit/page - Create new page
    GET /api/docit/page/<id> - Get single page
    PUT /api/docit/page/<id> - Update page
    DELETE /api/docit/page/<id> - Delete page
    """
    return Page.on_rest_request(request, pk)


@md.URL('page/slug/<str:slug>')
@md.custom_security("docit custom security")
def on_page_by_slug(request, slug=None):
    return Page.objects.get(slug=slug).on_rest_get(request)
