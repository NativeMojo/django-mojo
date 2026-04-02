import mojo.decorators as md
from mojo.helpers.response import JsonResponse

MAX_MARKDOWN_BYTES = 400_000


@md.URL('render')
@md.requires_auth()
def on_render(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    markdown = request.DATA.get("markdown")
    if not markdown:
        return JsonResponse({"error": "markdown field is required"}, status=400)
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        return JsonResponse({"error": "markdown input too large"}, status=413)
    from mojo.apps.docit.services.markdown import MarkdownRenderer
    renderer = MarkdownRenderer()
    html = renderer.render_safe(markdown)
    return {"html": html}
