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


def search_any(query, book=None, limit=10):
    """Return objict(mode, results) — mode is "hybrid", "fts", or "pages"."""
    if apps.is_installed("mojo.apps.docit_kb"):
        from mojo.apps.docit_kb.services import knowledge
        return knowledge.search(query, book=book, limit=limit)
    return objict(mode="pages", results=search_pages(query, book=book, limit=limit))


def search_pages(query, book=None, limit=10):
    """
    Page-level full-text search over published pages of active books.
    Same result shape as the knowledge-base chunk search (heading empty).
    """
    from mojo.apps.docit.models import Page

    qs = Page.objects.filter(is_published=True, book__is_active=True).select_related("book")
    if book is not None and book != "":
        if isinstance(book, int) or (isinstance(book, str) and book.isdigit()):
            qs = qs.filter(book_id=int(book))
        else:
            qs = qs.filter(book__slug=book)
    sq = SearchQuery(query, search_type="websearch")
    vector = SearchVector("title", weight="A") + SearchVector("content", weight="B")
    # Non-HTML highlight sentinels — Postgres defaults are <b></b>, which
    # invites rendering the snippet as HTML; page content is untrusted.
    qs = (qs.annotate(
            rank=SearchRank(vector, sq),
            snippet=SearchHeadline("content", sq, max_words=60,
                                   start_sel="**", stop_sel="**"))
          .filter(rank__gt=0)
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
