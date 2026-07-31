"""
Docit search dispatch + page-level FTS fallback.

search_any() is the single entry point used by the REST endpoint and the
assistant tool: it routes to the chunk-level hybrid search when
mojo.apps.docit_kb is installed, and otherwise falls back to page-level
Postgres full-text search (no extra schema, no pgvector required).
"""
from django.apps import apps
from django.contrib.postgres.search import (
    SearchHeadline, SearchQuery, SearchRank, SearchVector,
)
from objict import objict

SNIPPET_CHARS = 500

# A grant at the USER level that reads across every tenant. Deliberately not
# the model's whole VIEW_PERMS list: 'member' is a per-membership term, and
# treating it as global here would hand every authenticated user the unscoped
# search this item exists to close.
GLOBAL_READ_PERMS = ["view_docit", "manage_docit", "docs"]


def visible_groups(user=None, api_key=None):
    """Groups whose docit content this caller may search, or None = unrestricted.

    Mirrors on_rest_handle_list's two steps so search cannot see more than a
    list call would: a global docit-read grant reads every tenant, otherwise
    the caller is narrowed to the groups where they hold a read permission
    ('member' satisfies that for any membership — see Book.RestMeta).

    An ApiKey is NEVER unrestricted, even holding a read perm: a key is issued
    for one tenant, and its group tree is the boundary. Anonymous callers get
    an empty queryset — the public endpoints, not search, are the public path.
    """
    from mojo.apps.account.models import Group
    from mojo.apps.docit.models import Book

    perms = Book.get_rest_meta_prop("VIEW_PERMS", [])
    if api_key is not None:
        return api_key.get_groups_with_permission(perms)
    if user is None or not getattr(user, "is_authenticated", False):
        return Group.objects.none()
    if user.has_permission(GLOBAL_READ_PERMS):
        return None
    return user.get_groups_with_permission(perms)


def search_any(query, book=None, limit=10, groups=None, max_distance=None):
    """Return objict(mode, results) — mode is "hybrid", "fts", or "pages".

    `groups` confines the search to those tenants. None means unrestricted —
    the right default for internal/system callers, but request-facing callers
    must pass the result of visible_groups(), which is never None for a caller
    that lacks a global grant.

    `max_distance` is the knowledge base's vector relevance floor — see
    docit_kb.services.knowledge.search. It ships off; None falls back to the
    DOCIT_KB_MAX_DISTANCE setting, which is unset by default. The search_pages
    fallback below has no vector leg for it to apply to, so it ignores the
    parameter and a floored call on an install without docit_kb behaves exactly
    as an unfloored one — it carries the same `@@` match bound either way.
    """
    if apps.is_installed("mojo.apps.docit_kb"):
        from mojo.apps.docit_kb.services import knowledge
        return knowledge.search(query, book=book, limit=limit, groups=groups,
                                max_distance=max_distance)
    return objict(
        mode="pages",
        results=search_pages(query, book=book, limit=limit, groups=groups))


def search_pages(query, book=None, limit=10, groups=None):
    """
    Page-level full-text search over published pages of active books.
    Same result shape as the knowledge-base chunk search (heading empty).
    """
    from mojo.apps.docit.models import Page

    qs = Page.objects.filter(is_published=True, book__is_active=True).select_related("book")
    if groups is not None:
        # Also drops group-less books, which belong to no tenant at all.
        qs = qs.filter(book__group__in=groups)
    if book is not None and book != "":
        if isinstance(book, int) or (isinstance(book, str) and book.isdigit()):
            qs = qs.filter(book_id=int(book))
        else:
            qs = qs.filter(book__slug=book)
    sq = SearchQuery(query, search_type="websearch")
    vector = SearchVector("title", weight="A") + SearchVector("content", weight="B")
    # Non-HTML highlight sentinels — Postgres defaults are <b></b>, which
    # invites rendering the snippet as HTML; page content is untrusted.
    #
    # Bounded by the tsvector match operator (`@@`), NOT by `rank__gt=0`:
    # ts_rank returns 1e-20 rather than 0 for a non-matching document on a
    # multi-term query, so `rank__gt=0` admitted every page and this fallback
    # could never report "nothing matched" either. Rank remains the ordering
    # key. Same defect and same fix as docit_kb's _fts_leg.
    # alias(), not annotate(): the match predicate needs the tsvector, but
    # nothing reads it back, and annotate() would build and ship a full
    # to_tsvector(title||content) per returned row.
    qs = (qs.alias(search=vector)
            .annotate(
                rank=SearchRank(vector, sq),
                snippet=SearchHeadline("content", sq, max_words=60,
                                       start_sel="**", stop_sel="**"))
            .filter(search=sq)
            .order_by("-rank")[:limit])
    results = []
    for page in qs:
        results.append(objict(
            page_id=page.pk,
            page_slug=page.slug,
            page_title=page.title,
            book_id=page.book_id,
            book_slug=page.book.slug,
            heading="",
            snippet=(page.snippet or "")[:SNIPPET_CHARS],
            score=round(float(page.rank), 6),
        ))
    return results
