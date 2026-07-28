import mojo.decorators as md
from mojo.helpers.response import JsonResponse

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_QUERY_CHARS = 512


@md.URL('search')
@md.requires_auth()
@md.rate_limit("docit_search", ip_limit=120, duid_limit=60)
def on_search(request):
    """
    Knowledge-base search over docit content.

    GET/POST /api/docit/search
        q      - search terms (required; `search` accepted as an alias)
        book   - optional book id or slug to scope the search
        limit  - max results (default 10, max 50)

    Response: {"results": [...], "mode": "hybrid"|"fts"|"pages", "count": N}
    """
    query = request.DATA.get("q", None) or request.DATA.get("search", None)
    if not query or not str(query).strip():
        return JsonResponse({"error": "q is required"}, status=400)
    if len(str(query)) > MAX_QUERY_CHARS:
        return JsonResponse({"error": f"q too long (max {MAX_QUERY_CHARS} chars)"}, status=400)
    try:
        limit = int(request.DATA.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))
    from mojo.apps.docit.services.search import search_any, visible_groups
    # Search reads the same content the list endpoints serve, so it has to be
    # confined the same way — without this it was a cross-tenant snippet feed
    # for any authenticated caller.
    groups = visible_groups(
        user=request.user, api_key=getattr(request, "api_key", None))
    found = search_any(str(query).strip(), book=request.DATA.get("book", None),
                       limit=limit, groups=groups)
    return {"results": found.results, "mode": found.mode, "count": len(found.results)}
