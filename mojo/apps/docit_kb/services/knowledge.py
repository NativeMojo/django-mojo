"""
Docit knowledge base — chunk pipeline and hybrid search.

    embed_page_now(page)  — (re)chunk + (re)embed one page, idempotent
    search(query, ...)    — hybrid pgvector + Postgres FTS with RRF fusion
    reindex_book(book)    — queue embed jobs for every page of a book
"""
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from objict import objict
from pgvector.django import CosineDistance

from mojo.helpers import embeddings, logit
from mojo.apps.docit_kb.models import PageChunk
from mojo.apps.docit_kb.models.page_chunk import EMBEDDING_DIM

logger = logit.get_logger(__name__, "docit_kb.log")

RRF_K = 60
SNIPPET_CHARS = 500
EMBED_JOB = "mojo.apps.docit_kb.asyncjobs.embed_page"


def embed_page_now(page):
    """
    Synchronously refresh chunks + embeddings for one page.

    Chunks are matched to existing rows by content hash, so an unchanged
    chunk keeps its embedding; only new content gets embedded. Idempotent —
    a re-run on unchanged content writes nothing and embeds nothing.
    """
    from mojo.apps.docit_kb.services.chunker import chunk_markdown
    chunks = chunk_markdown(page.content)
    pools = {}
    for row in PageChunk.objects.filter(page=page).order_by("position"):
        pools.setdefault(row.content_hash, []).append(row)
    created = 0
    reused = 0
    for chunk in chunks:
        pool = pools.get(chunk.content_hash)
        if pool:
            row = pool.pop(0)
            reused += 1
            if row.position != chunk.position or row.heading != chunk.heading:
                row.position = chunk.position
                row.heading = chunk.heading
                row.save()
        else:
            PageChunk.objects.create(
                page=page,
                position=chunk.position,
                heading=chunk.heading,
                content=chunk.content,
                content_hash=chunk.content_hash,
            )
            created += 1
    stale = [row.pk for pool in pools.values() for row in pool]
    if stale:
        PageChunk.objects.filter(pk__in=stale).delete()
    embedded = _embed_missing(page)
    logger.info(
        f"embed_page_now page={page.pk} chunks={len(chunks)} created={created} "
        f"reused={reused} removed={len(stale)} embedded={embedded}")
    return objict(chunks=len(chunks), created=created, reused=reused,
                  removed=len(stale), embedded=embedded)


def _embed_missing(page):
    rows = list(PageChunk.objects.filter(page=page, embedding__isnull=True).order_by("position"))
    if not rows or not embeddings.is_available():
        return 0
    dim = embeddings.get_dim()
    if dim != EMBEDDING_DIM:
        logger.error(
            f"EMBEDDINGS_DIM {dim} does not match the PageChunk column "
            f"dimension {EMBEDDING_DIM}; skipping vectors (re-embed migration required)")
        return 0
    provider = embeddings.get_provider()
    vectors = provider.embed([_embed_input(row) for row in rows])
    for row, vector in zip(rows, vectors):
        row.embedding = vector
        row.embed_model = provider.model_id
        row.save()
    return len(rows)


def _embed_input(row):
    return f"{row.heading}\n{row.content}" if row.heading else row.content


def search(query, book=None, limit=10):
    """
    Hybrid search over published chunks. Returns objict(mode, results) where
    mode is "hybrid" (vector + FTS) or "fts" (no embeddings provider).
    """
    qs = PageChunk.objects.filter(
        page__is_published=True,
        page__book__is_active=True,
    ).select_related("page", "page__book")
    if book is not None and book != "":
        qs = _filter_book(qs, book)
    pool = max(limit * 3, 15)
    legs = []
    fts_rows = _fts_leg(qs, query, pool)
    if fts_rows:
        legs.append(fts_rows)
    vector_rows = _vector_leg(qs, query, pool)
    mode = "fts" if vector_rows is None else "hybrid"
    if vector_rows:
        legs.append(vector_rows)
    scores = {}
    rows = {}
    for leg in legs:
        for rank, row in enumerate(leg):
            scores[row.pk] = scores.get(row.pk, 0.0) + 1.0 / (RRF_K + rank + 1)
            rows[row.pk] = row
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    results = [_result(rows[pk], score) for pk, score in ranked]
    return objict(mode=mode, results=results)


def reindex_book(book):
    """Queue an embed job for every page of a book. Returns the count queued."""
    from mojo.apps import jobs
    count = 0
    for page_id in book.pages.values_list("id", flat=True):
        jobs.publish(EMBED_JOB, {"page_id": page_id}, max_retries=2)
        count += 1
    logger.info(f"reindex_book book={book.pk} queued={count}")
    return count


def _filter_book(qs, book):
    if isinstance(book, int) or (isinstance(book, str) and book.isdigit()):
        return qs.filter(page__book_id=int(book))
    return qs.filter(page__book__slug=book)


def _fts_leg(qs, query, pool):
    sq = SearchQuery(query, search_type="websearch")
    vector = SearchVector("heading", weight="A") + SearchVector("content", weight="B")
    return list(
        qs.annotate(rank=SearchRank(vector, sq))
          .filter(rank__gt=0)
          .order_by("-rank")[:pool])


def _vector_leg(qs, query, pool):
    """Top chunks by cosine distance, or None when no provider is available."""
    if not embeddings.is_available():
        return None
    try:
        query_vector = embeddings.embed_query(query)
    except Exception as err:
        logger.error(f"query embedding failed: {err}")
        return None
    if len(query_vector) != EMBEDDING_DIM:
        logger.error(
            f"query embedding dimension {len(query_vector)} does not match "
            f"column dimension {EMBEDDING_DIM}; skipping vector leg")
        return None
    return list(
        qs.exclude(embedding__isnull=True)
          .annotate(distance=CosineDistance("embedding", query_vector))
          .order_by("distance")[:pool])


def _result(row, score):
    return objict(
        page_id=row.page_id,
        page_slug=row.page.slug,
        page_title=row.page.title,
        book_id=row.page.book_id,
        book_slug=row.page.book.slug,
        heading=row.heading,
        snippet=row.content[:SNIPPET_CHARS],
        score=round(score, 6),
    )
