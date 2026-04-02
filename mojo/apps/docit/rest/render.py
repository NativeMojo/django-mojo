import mojo.decorators as md
from mojo.helpers.response import JsonResponse


@md.URL('render')
@md.requires_auth()
def on_render(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    markdown = request.DATA.get("markdown")
    if not markdown:
        return JsonResponse({"error": "markdown field is required"}, status=400)
    from mojo.apps.docit.services.markdown import MarkdownRenderer
    renderer = MarkdownRenderer()
    html = renderer.render(markdown)
    return {"html": html}
