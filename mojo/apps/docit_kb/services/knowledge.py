"""
Docit knowledge base — chunk pipeline and hybrid search.

    embed_page_now(page)        — (re)chunk + (re)embed one page, idempotent
    search(query, ...)          — hybrid pgvector + Postgres FTS with RRF fusion
    reindex_book(book)          — queue embed jobs for every page of a book
    reconcile_stale_pages(...)  — sweep that heals pages whose chunks fell behind
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

# Reconciliation sweep. Per-page publishes are deduped in hourly buckets, so a
# sweep that runs every 15 minutes queues a given page at most once an hour.
RECONCILE_BUCKET_SEC = 3600
RECONCILE_LIMIT = 200
RECONCILE_LOOKBACK_HOURS = 168
RECONCILE_GRACE_SEC = 600
RECONCILE_KEY_PREFIX = "docit_kb.recon:"


def embed_page_now(page):
    """
    Synchronously refresh chunks + embeddings for one page.

    Chunks are matched to existing rows by content hash, so an unchanged
    chunk keeps its embedding; only new content gets embedded. Idempotent —
    a re-run on unchanged content writes nothing and embeds nothing.

    On success every chunk row is stamped with the page's `modified` value as
    it stood when this run started — see the comment on the stamp below.
    `PageChunk.modified` is therefore a pipeline verification watermark, not
    a last-content-change timestamp, and `reconcile_stale_pages` reads it as
    "these chunks were verified against page version X".
    """
    from mojo.apps.docit_kb.services.chunker import chunk_markdown
    # Snapshot BEFORE chunking: everything below certifies this page version.
    page_modified = page.modified
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
    if chunks:
        # Stamp the SNAPSHOT taken at entry, never timezone.now(): the chunks
        # certify the page version this run read. A save landing mid-embed
        # bumps page.modified past the snapshot, so the reconcile predicate
        # (page.modified > MAX(chunk.modified)) stays exact and the page
        # remains flagged for another pass instead of being silently declared
        # current. update() bypasses auto_now, so the explicit value sticks.
        #
        # This also makes the pre-existing concurrent-embed race (two
        # embed_page_now runs on one page, no lock) self-healing: a late,
        # stale writer stamps its OLD snapshot, which leaves the page flagged,
        # and the next sweep re-heals it.
        PageChunk.objects.filter(page=page).update(modified=page_modified)
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
    # embed_texts (not provider.embed) so MAX_INPUT_CHARS truncation applies —
    # a single oversized paragraph passes through the chunker whole by design.
    provider = embeddings.get_provider()
    vectors = embeddings.embed_texts([_embed_input(row) for row in rows])
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
    """Queue an embed job for every page of a book. Returns the count queued.

    The idempotency key is (page id, page modified) — repeat reindexes of an
    unchanged page dedupe against the earlier job instead of flooding the
    queue; editing the page mints a new key. A permanently-failed job for an
    unchanged page therefore won't re-run until the page is touched —
    reconcile_stale_pages is what re-queues it.
    """
    from mojo.apps import jobs
    count = 0
    for page_id, modified in book.pages.values_list("id", "modified"):
        jobs.publish(
            EMBED_JOB, {"page_id": page_id}, max_retries=2,
            idempotency_key=f"docit_kb.embed:{page_id}:{int(modified.timestamp())}")
        count += 1
    logger.info(f"reindex_book book={book.pk} queued={count}")
    return count


def reconcile_stale_pages(limit=None, lookback_hours=None, now=None):
    """
    Queue embed jobs for pages whose chunks fell behind. Returns an objict of
    counts: scanned, queued, skipped_inflight, skipped_recent, skipped_blank.

    The embed publish on Page.save is fire-and-forget, so a queue outage (or a
    permanently failed job) leaves a page serving its pre-edit chunks forever.
    This sweep is the repair path. Three arms, evaluated in order and capped
    together at `limit`:

      1. STALE — a page that HAS chunks whose watermark is behind
         page.modified, within `lookback_hours`. The watermark embed_page_now
         stamps makes this predicate exact (see its comments). No blank check
         here: a page blanked by an edit still serves its old chunks and must
         heal.
      2. NEVER CHUNKED — a page with no chunk rows at all. No lookback: the
         arm is self-terminating (one successful embed removes the page from
         it), which makes adoption backfill automatic. Blank pages are
         skipped here ONLY, since they yield zero chunks by design and would
         otherwise be queued on every sweep forever. Never-chunked pages are
         left entirely to this arm — arm 1 must not also match them, or the
         blank guard would be bypassed for the first `lookback_hours`.
      3. NULL EMBEDDINGS — chunks exist but carry no vector, i.e. an embed
         that ran while the provider was failing. Gated on a provider being
         available: on an FTS-only install null vectors are the normal steady
         state, and queueing them would loop forever.

    Pages saved in the last RECONCILE_GRACE_SEC are left alone so the sweep
    never races the embed job the save just published.

    `now` defaults to timezone.now(); pass an aware UTC datetime to sweep as
    of a simulated instant (test seam only — cron calls this with no args).
    """
    from datetime import timedelta
    from django.db.models import F, Max
    from django.utils import timezone
    from mojo.apps import jobs, metrics
    from mojo.apps.docit.models import Page
    from mojo.apps.jobs.models import Job
    from mojo.helpers.settings import settings

    if now is None:
        now = timezone.now()
    if limit is None:
        limit = settings.get("DOCIT_KB_RECONCILE_LIMIT", RECONCILE_LIMIT, kind="int")
    if lookback_hours is None:
        lookback_hours = settings.get(
            "DOCIT_KB_RECONCILE_LOOKBACK_HOURS", RECONCILE_LOOKBACK_HOURS, kind="int")
    limit = max(int(limit), 0)
    grace_cutoff = now - timedelta(seconds=RECONCILE_GRACE_SEC)

    # Exclusions are computed BEFORE any slice so they cannot consume budget.
    # The 1h recency bound is deliberate: a zombie `pending` row must suppress
    # its page for an hour, not for the whole lookback window.
    recent = Job.objects.filter(func=EMBED_JOB, created__gte=now - timedelta(hours=1))
    inflight = {
        pid for pid in recent.filter(status__in=("pending", "running"))
                             .values_list("payload__page_id", flat=True)
        if pid is not None}
    attempted = {
        pid for pid in recent.filter(idempotency_key__startswith=RECONCILE_KEY_PREFIX)
                             .values_list("payload__page_id", flat=True)
        if pid is not None}
    excluded = inflight | attempted

    # order_by is about determinism of the slice ONLY — newest edits heal
    # first, and a page that misses the cap this sweep is picked up by the
    # next one.
    # modified > newest_chunk is false when newest_chunk is NULL, so a
    # never-chunked page is deliberately NOT matched here — arm 2 owns those,
    # and it is the only arm that applies the blank guard.
    page_ids = list(
        Page.objects.annotate(newest_chunk=Max("chunks__modified"))
            .filter(modified__gte=now - timedelta(hours=lookback_hours),
                    modified__lt=grace_cutoff)
            .filter(modified__gt=F("newest_chunk"))
            .exclude(id__in=excluded)
            .order_by("-modified")
            .values_list("id", flat=True)[:limit])

    skipped_blank = 0
    remaining = limit - len(page_ids)
    if remaining > 0:
        for page_id, content in (
                Page.objects.filter(chunks__isnull=True, modified__lt=grace_cutoff)
                    .exclude(id__in=excluded)
                    .exclude(id__in=page_ids)
                    .exclude(content="")
                    .order_by("-modified")
                    .values_list("id", "content")[:remaining]):
            if not (content or "").strip():
                skipped_blank += 1
                continue
            page_ids.append(page_id)

    remaining = limit - len(page_ids)
    if remaining > 0 and embeddings.is_available():
        # Subquery rather than a chunks__embedding__isnull join: Postgres
        # rejects SELECT DISTINCT with an ORDER BY column outside the select
        # list, and the join alone would return a page once per null chunk.
        null_chunk_pages = PageChunk.objects.filter(
            embedding__isnull=True).values_list("page_id", flat=True)
        page_ids.extend(
            Page.objects.filter(id__in=null_chunk_pages, modified__lt=grace_cutoff)
                .exclude(id__in=excluded)
                .exclude(id__in=page_ids)
                .order_by("-modified")
                .values_list("id", flat=True)[:remaining])

    bucket = int(now.timestamp() // RECONCILE_BUCKET_SEC)
    queued = 0
    for page_id in page_ids:
        try:
            jobs.publish(
                EMBED_JOB, {"page_id": page_id}, max_retries=2,
                idempotency_key=f"{RECONCILE_KEY_PREFIX}{page_id}:{bucket}")
            queued += 1
        except Exception as err:
            logger.error(f"reconcile: embed publish failed for page {page_id}: {err}")
    if queued:
        try:
            metrics.record("docit_kb.reconcile.queued", count=queued, category="docit_kb")
        except Exception as err:
            logger.error(f"reconcile: metrics record failed: {err}")

    result = objict(
        scanned=len(page_ids) + skipped_blank,
        queued=queued,
        skipped_inflight=len(inflight),
        skipped_recent=len(attempted),
        skipped_blank=skipped_blank)
    logger.info(
        f"reconcile_stale_pages scanned={result.scanned} queued={result.queued} "
        f"skipped_inflight={result.skipped_inflight} skipped_recent={result.skipped_recent} "
        f"skipped_blank={result.skipped_blank}")
    return result


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
