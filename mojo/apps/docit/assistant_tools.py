"""
Assistant tools for docit — knowledge-base search over books and pages.

Autodiscovered by mojo.apps.assistant (module name `assistant_tools`).
"""
from mojo.apps.assistant import tool


@tool(
    name="search_docs",
    domain="docit",
    permission="all",
    description=(
        "Search the documentation knowledge base (docit books and pages). "
        "Returns ranked excerpts with page and book references. Use `book` "
        "to scope the search to one book by id or slug."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms"},
            "book": {"type": "string", "description": "Optional book id or slug"},
            "limit": {"type": "integer", "description": "Max results (default 10, max 50)"},
        },
        "required": ["query"],
    },
)
def _tool_search_docs(params, user):
    """The tool stays visible to every assistant user (permission="all"), but
    what it RETURNS is confined to the asking user's own tenants — same rule
    as the REST endpoint and the list endpoints."""
    from mojo.apps.docit.services.search import search_any, visible_groups

    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    try:
        limit = int(params.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 50))
    found = search_any(query, book=params.get("book"), limit=limit,
                       groups=visible_groups(user=user))
    return {"mode": found.mode, "count": len(found.results), "results": found.results}
