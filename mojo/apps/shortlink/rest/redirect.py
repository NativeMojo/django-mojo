import mojo.decorators as md
from django.http import HttpResponseRedirect, HttpResponse
from mojo.helpers.settings import settings


def _get_fallback_url():
    return settings.get("SHORTLINK_FALLBACK_URL", None) or \
           settings.get("BASE_URL", "/")


def _render_og_html(link, destination_url):
    """Render a minimal HTML page with OG meta tags for bot previews."""
    og = link.get_og_metadata()
    if not og:
        # No metadata at all — just redirect the bot too
        return None

    meta_tags = []
    for key, value in og.items():
        if key.startswith("_"):
            continue
        safe_key = str(key).replace('"', '&quot;')
        safe_val = str(value).replace('"', '&quot;')
        if key.startswith("twitter:"):
            meta_tags.append(f'<meta name="{safe_key}" content="{safe_val}">')
        else:
            meta_tags.append(f'<meta property="{safe_key}" content="{safe_val}">')

    if not meta_tags:
        return None

    title = og.get("og:title", "")
    safe_dest = destination_url.replace('"', '&quot;').replace("'", "&#39;")
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
{chr(10).join(meta_tags)}
<meta http-equiv="refresh" content="0;url={safe_dest}">
</head>
<body>
<p>Redirecting to <a href="{safe_dest}">{safe_dest}</a></p>
</body>
</html>"""
    return html


@md.GET("/s/<str:code>")
@md.public_endpoint(reason="Short link redirect must be accessible without authentication")
def on_shortlink_redirect(request, code):
    """Redirect a short link to its destination URL."""
    from mojo.apps.shortlink.models import ShortLink, is_bot_user_agent

    link = ShortLink.objects.filter(code=code, is_active=True).first()
    if not link:
        return HttpResponseRedirect(_get_fallback_url())

    # Resolve destination (increments hit_count, records metric)
    destination = link.resolve()
    if not destination:
        return HttpResponseRedirect(_get_fallback_url())

    # Log click if tracking enabled
    link.log_click(request)

    # Bot preview (unless bot_passthrough)
    if not link.bot_passthrough:
        user_agent = request.META.get("HTTP_USER_AGENT", "")
        if is_bot_user_agent(user_agent):
            html = _render_og_html(link, destination)
            if html:
                return HttpResponse(html, content_type="text/html; charset=utf-8")

    return HttpResponseRedirect(destination)
