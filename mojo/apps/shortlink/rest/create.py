import mojo.decorators as md
from mojo.apps.shortlink.models import ShortLink, ShortLinkClick


@md.URL('link')
@md.URL('link/<int:pk>')
@md.uses_model_security(ShortLink)
def on_link(request, pk=None):
    return ShortLink.on_rest_request(request, pk)


@md.POST('link/create')
@md.requires_auth()
def on_create_link(request):
    from mojo.apps import shortlink
    from mojo.apps.shortlink.services.access import require_group_access, shareable_file
    from mojo.models.rest import _resolve_stamp_actor

    require_group_access(request)

    data = request.DATA
    original_url = data.get("url", "")
    file_id = data.get("file")
    file = None

    if file_id not in [None, ""]:
        file = shareable_file(request, file_id)

    short_link = shortlink.shorten(
        url=original_url,
        file=file,
        source=data.get("source", ""),
        expire_days=data.get_typed("expire_days", 3, int),
        expire_hours=data.get_typed("expire_hours", 0, int),
        metadata=data.get("metadata", None),
        track_clicks=data.get_typed("track_clicks", False, bool),
        resolve_file=data.get_typed("resolve_file", True, bool),
        bot_passthrough=data.get_typed("bot_passthrough", False, bool),
        is_protected=data.get_typed("is_protected", False, bool),
        user=_resolve_stamp_actor(request),
        group=request.group,
        base_url=data.get("base_url", None),
    )
    return {"status": True, "data": {"short_link": short_link, "original_url": original_url}}


@md.URL('history')
@md.URL('history/<int:pk>')
@md.uses_model_security(ShortLinkClick)
def on_history(request, pk=None):
    return ShortLinkClick.on_rest_request(request, pk)
