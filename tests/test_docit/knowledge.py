from testit import helpers as th

KB_USER = "kbtest_user"
KB_PWORD = "kbtest##mojo99"
EMBED_JOB = "mojo.apps.docit_kb.asyncjobs.embed_page"
RECON_JOB = "mojo.apps.docit_kb.asyncjobs.reconcile_embeddings"
RECON_KEY = "docit_kb.recon:"

PAGE_ALPHA_CONTENT = """# Alpha

Alpha widgets are configured with the alpha panel.

## Alpha Details

More alpha material about widget configuration.
"""

PAGE_BETA_CONTENT = """# Beta

The exact identifier ZXQTOKEN99 lives here for full-text matching.
"""


@th.django_unit_setup()
def setup_knowledge_testing(opts):
    """Create a clean book + pages + user for knowledge-base tests."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.docit.models import Book, Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.jobs.models import Job

    # Long-lived DB: remove anything a previous run left behind.
    PageChunk.objects.filter(page__book__title__startswith="kbtest_").delete()
    Page.objects.filter(book__title__startswith="kbtest_").delete()
    Book.objects.filter(title__startswith="kbtest_").delete()
    Job.objects.filter(func=EMBED_JOB).delete()
    Job.objects.filter(func=RECON_JOB).delete()
    User.objects.filter(username=KB_USER).delete()

    group, _ = Group.objects.get_or_create(name="kbtest_org", kind="organization")
    user = User(username=KB_USER, email=f"{KB_USER}@test.com")
    user.save()
    user.is_email_verified = True
    user.is_active = True
    user.save_password(KB_PWORD)
    user.add_permission("manage_docit")
    user.save()

    book = Book.objects.create(
        title="kbtest_guide", description="kbtest guide", group=group,
        user=user, created_by=user, modified_by=user)
    page_alpha = Page.objects.create(
        book=book, title="kbtest_alpha", content=PAGE_ALPHA_CONTENT,
        user=user, created_by=user, modified_by=user)
    page_beta = Page.objects.create(
        book=book, title="kbtest_beta", content=PAGE_BETA_CONTENT,
        user=user, created_by=user, modified_by=user)

    opts.kb_user_id = user.id
    opts.kb_book_id = book.id
    opts.kb_book_slug = book.slug
    opts.kb_page_alpha_id = page_alpha.id
    opts.kb_page_beta_id = page_beta.id


@th.unit_test("chunker splits on headings with breadcrumbs")
def test_chunker_heading_split(opts):
    from mojo.apps.docit_kb.services.chunker import chunk_markdown

    content = "intro before headings\n\n# A\n\ntext a\n\n## B\n\ntext b\n\n### C\n\ntext c\n\n## B2\n\ntext b2\n"
    chunks = chunk_markdown(content)
    headings = [c.heading for c in chunks]
    assert headings == ["", "A", "A > B", "A > B > C", "A > B2"], \
        f"Expected breadcrumb trail ['', 'A', 'A > B', 'A > B > C', 'A > B2'], got {headings}"
    assert [c.position for c in chunks] == list(range(len(chunks))), \
        f"Positions must be sequential from 0, got {[c.position for c in chunks]}"
    assert chunks[1].content.startswith("# A"), \
        f"Chunk content must include its heading line, got {chunks[1].content[:40]!r}"
    again = chunk_markdown(content)
    assert [c.content_hash for c in again] == [c.content_hash for c in chunks], \
        "Chunker must be deterministic — same content, same hashes"


@th.unit_test("chunker ignores headings inside code fences")
def test_chunker_code_fence(opts):
    from mojo.apps.docit_kb.services.chunker import chunk_markdown

    content = "# Real\n\ntext\n\n```\n# not a heading\n```\n\nmore text\n"
    chunks = chunk_markdown(content)
    assert len(chunks) == 1, \
        f"Fenced '# not a heading' must not split the section, got {len(chunks)} chunks: {[c.heading for c in chunks]}"
    assert chunks[0].heading == "Real", f"Expected heading 'Real', got {chunks[0].heading!r}"


@th.unit_test("chunker splits oversized sections at paragraph boundaries")
def test_chunker_oversize_split(opts):
    from mojo.apps.docit_kb.services.chunker import chunk_markdown, MAX_CHUNK_CHARS

    paragraphs = "\n\n".join(f"paragraph {i} " + ("x" * 1500) for i in range(6))
    content = f"# Big\n\n{paragraphs}\n"
    chunks = chunk_markdown(content)
    assert len(chunks) > 1, \
        f"A {len(content)}-char section must split into multiple chunks, got {len(chunks)}"
    for chunk in chunks:
        assert len(chunk.content) <= MAX_CHUNK_CHARS + 100, \
            f"Chunk exceeds size cap: {len(chunk.content)} chars (cap {MAX_CHUNK_CHARS})"
        assert chunk.heading == "Big", f"Split pieces keep the section heading, got {chunk.heading!r}"


@th.unit_test("chunker handles a page with no headings")
def test_chunker_no_headings(opts):
    from mojo.apps.docit_kb.services.chunker import chunk_markdown

    chunks = chunk_markdown("just some text\n\nwith two paragraphs")
    assert len(chunks) == 1, f"Heading-less content must yield one chunk, got {len(chunks)}"
    assert chunks[0].heading == "", f"Heading must be empty, got {chunks[0].heading!r}"
    assert chunk_markdown("") == [], "Empty content must yield no chunks"


@th.django_unit_test()
def test_mock_embeddings_deterministic(opts):
    """The mock provider must be deterministic, dimensioned, and normalized."""
    from mojo.helpers import embeddings

    assert embeddings.is_available(), "Mock provider must report available in the test env"
    first = embeddings.embed_query("alpha text")
    second = embeddings.embed_query("alpha text")
    other = embeddings.embed_query("completely different text")
    assert first == second, "Identical text must embed identically (deterministic mock)"
    assert first != other, "Different text must embed differently"
    assert len(first) == 1024, f"Expected 1024 dimensions, got {len(first)}"
    norm = sum(v * v for v in first) ** 0.5
    assert abs(norm - 1.0) < 1e-6, f"Mock vectors must be unit-normalized, norm={norm}"


@th.django_unit_test()
def test_embed_pipeline_and_hash_skip(opts):
    """embed_page_now creates chunks, embeds them, and skips unchanged content."""
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    page = Page.objects.get(pk=opts.kb_page_alpha_id)
    stats = knowledge.embed_page_now(page)
    assert stats.chunks == 2, f"Alpha page has two heading sections, got {stats.chunks} chunks"
    assert stats.created == 2, f"First run must create every chunk, created={stats.created}"
    assert stats.embedded == 2, f"Mock provider must embed every chunk, embedded={stats.embedded}"

    rows = list(PageChunk.objects.filter(page=page).order_by("position"))
    assert all(r.embedding is not None for r in rows), "All chunks must carry embeddings"
    assert all(r.embed_model == "mock" for r in rows), \
        f"embed_model must record the provider, got {[r.embed_model for r in rows]}"
    first_pks = [r.pk for r in rows]

    # Unchanged content: nothing created, nothing re-embedded, rows reused.
    stats = knowledge.embed_page_now(page)
    assert stats.created == 0 and stats.embedded == 0 and stats.reused == 2, \
        f"Unchanged content must be a no-op, got {stats}"
    assert [r.pk for r in PageChunk.objects.filter(page=page).order_by("position")] == first_pks, \
        "Unchanged chunks must keep their rows (and embeddings)"

    # Change one section: only that chunk is replaced.
    page.content = PAGE_ALPHA_CONTENT.replace("More alpha material", "Rewritten alpha material")
    page.save()
    stats = knowledge.embed_page_now(page)
    assert stats.created == 1 and stats.removed == 1 and stats.reused == 1, \
        f"Editing one section must replace exactly one chunk, got {stats}"
    surviving = PageChunk.objects.filter(page=page, pk=first_pks[0]).first()
    assert surviving is not None, "The untouched chunk must survive a partial edit"

    # Restore original content for later tests.
    page.content = PAGE_ALPHA_CONTENT
    page.save()
    knowledge.embed_page_now(page)


@th.django_unit_test()
def test_embed_provider_unavailable(opts):
    """Without a provider, chunks are still written — embeddings stay null."""
    from django.conf import settings as dj_settings
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    page = Page.objects.get(pk=opts.kb_page_beta_id)
    PageChunk.objects.filter(page=page).delete()
    original = getattr(dj_settings, "EMBEDDINGS_PROVIDER", "mock")
    dj_settings.EMBEDDINGS_PROVIDER = "bedrock"  # no AWS creds in the test env
    try:
        stats = knowledge.embed_page_now(page)
        assert stats.created >= 1, f"Chunks must be created without a provider, got {stats}"
        assert stats.embedded == 0, f"No provider must mean no embeddings, got {stats}"
        assert PageChunk.objects.filter(page=page, embedding__isnull=False).count() == 0, \
            "No embeddings may be written while the provider is unavailable"
    finally:
        dj_settings.EMBEDDINGS_PROVIDER = original

    # Provider back: the same rows get their embeddings filled in.
    stats = knowledge.embed_page_now(page)
    assert stats.created == 0 and stats.embedded >= 1, \
        f"Re-run with provider must embed the null rows only, got {stats}"


@th.django_unit_test()
def test_embed_dimension_guard(opts):
    """A dimension mismatch must skip vectors instead of writing corrupt data."""
    from django.conf import settings as dj_settings
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    page = Page.objects.get(pk=opts.kb_page_beta_id)
    PageChunk.objects.filter(page=page).delete()
    dj_settings.EMBEDDINGS_DIM = 256
    try:
        stats = knowledge.embed_page_now(page)
        assert stats.embedded == 0, \
            f"EMBEDDINGS_DIM=256 vs 1024 column must skip embedding, got {stats}"
    finally:
        del dj_settings.EMBEDDINGS_DIM
    stats = knowledge.embed_page_now(page)
    assert stats.embedded >= 1, f"Restored dimension must embed again, got {stats}"


@th.django_unit_test()
def test_hybrid_search_ranking(opts):
    """Exact identifiers hit via FTS; fusion ranks the right page first."""
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.services import knowledge

    for page_id in (opts.kb_page_alpha_id, opts.kb_page_beta_id):
        knowledge.embed_page_now(Page.objects.get(pk=page_id))

    found = knowledge.search("ZXQTOKEN99")
    assert found.mode == "hybrid", f"Mock provider present — expected hybrid mode, got {found.mode}"
    assert len(found.results) >= 1, "The exact identifier must be found"
    top = found.results[0]
    assert top.page_id == opts.kb_page_beta_id, \
        f"ZXQTOKEN99 lives on the beta page, top hit was page {top.page_id} ({top.page_title})"
    assert top.book_slug == opts.kb_book_slug, f"Result must carry book refs, got {top.book_slug}"
    assert top.snippet and top.score > 0, f"Result must carry snippet and score, got {top}"

    found = knowledge.search("alpha widgets configuration")
    assert len(found.results) >= 1, "FTS terms from the alpha page must match"
    assert found.results[0].page_id == opts.kb_page_alpha_id, \
        f"Alpha terms must rank the alpha page first, got page {found.results[0].page_id}"


@th.django_unit_test()
def test_search_visibility_filters(opts):
    """Unpublished pages and inactive books never surface in results."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book, Page
    from mojo.apps.docit_kb.services import knowledge

    page_beta = Page.objects.get(pk=opts.kb_page_beta_id)
    page_beta.is_published = False
    page_beta.save()
    try:
        found = knowledge.search("ZXQTOKEN99")
        assert all(r.page_id != page_beta.pk for r in found.results), \
            f"Unpublished page must be hidden from search, got {found.results}"
    finally:
        page_beta.is_published = True
        page_beta.save()

    user = User.objects.get(pk=opts.kb_user_id)
    hidden_book = Book.objects.create(
        title="kbtest_hidden", user=user, created_by=user, modified_by=user)
    hidden_page = Page.objects.create(
        book=hidden_book, title="kbtest_hidden_page",
        content="# Hidden\n\nQQTOKEN55 secret material.",
        user=user, created_by=user, modified_by=user)
    knowledge.embed_page_now(hidden_page)
    assert any(r.page_id == hidden_page.pk for r in knowledge.search("QQTOKEN55").results), \
        "Sanity: the hidden-book page must be findable while its book is active"
    hidden_book.is_active = False
    hidden_book.save()
    found = knowledge.search("QQTOKEN55")
    assert all(r.book_id != hidden_book.pk for r in found.results), \
        f"Inactive-book chunks must be hidden from search, got {found.results}"


@th.django_unit_test()
def test_search_book_filter(opts):
    """The book filter works by id and by slug."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book, Page
    from mojo.apps.docit_kb.services import knowledge

    user = User.objects.get(pk=opts.kb_user_id)
    other_book = Book.objects.create(
        title="kbtest_other", user=user, created_by=user, modified_by=user)
    other_page = Page.objects.create(
        book=other_book, title="kbtest_other_page",
        content="# Other\n\nCOMMONTOK77 lives in the other book too.",
        user=user, created_by=user, modified_by=user)
    knowledge.embed_page_now(other_page)
    main_page = Page.objects.get(pk=opts.kb_page_alpha_id)
    main_page.content = PAGE_ALPHA_CONTENT + "\n\nCOMMONTOK77 also lives in the guide.\n"
    main_page.save()
    knowledge.embed_page_now(main_page)

    # Superset, not equality: this is the control for the two filtered calls
    # below, and an UNfiltered hybrid search legitimately also returns the
    # vector leg's nearest neighbours — which on a long-lived database
    # includes whatever other modules have embedded. The filtered assertions
    # that follow are the actual subject, and those stay exact.
    unfiltered = knowledge.search("COMMONTOK77")
    assert {opts.kb_book_id, other_book.pk} <= {r.book_id for r in unfiltered.results}, \
        f"Unfiltered search must span both books, got {[r.book_slug for r in unfiltered.results]}"

    by_slug = knowledge.search("COMMONTOK77", book=opts.kb_book_slug)
    assert {r.book_id for r in by_slug.results} == {opts.kb_book_id}, \
        f"Slug filter must scope to the guide book, got {[r.book_slug for r in by_slug.results]}"

    by_id = knowledge.search("COMMONTOK77", book=str(other_book.pk))
    assert {r.book_id for r in by_id.results} == {other_book.pk}, \
        f"Id filter must scope to the other book, got {[r.book_slug for r in by_id.results]}"

    # Restore alpha content for any later run.
    main_page.content = PAGE_ALPHA_CONTENT
    main_page.save()
    knowledge.embed_page_now(main_page)


@th.django_unit_test()
def test_page_save_publishes_embed_job(opts):
    """Page.save queues an embed job; running it produces chunks."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book, Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.jobs.models import Job

    Job.objects.filter(func=EMBED_JOB).delete()
    user = User.objects.get(pk=opts.kb_user_id)
    book = Book.objects.get(pk=opts.kb_book_id)
    page = Page.objects.create(
        book=book, title="kbtest_jobflow",
        content="# Jobflow\n\nJOBTOKEN33 arrives via the async pipeline.",
        user=user, created_by=user, modified_by=user)

    queued = Job.objects.filter(func=EMBED_JOB, status="pending")
    assert queued.count() >= 1, "Page.save must queue an embed job when docit_kb is installed"
    assert any((j.payload or {}).get("page_id") == page.pk for j in queued), \
        f"A queued job must carry this page's id, payloads={[j.payload for j in queued]}"

    executed = th.run_pending_jobs()
    assert executed >= 1, f"Expected at least one job to execute, got {executed}"
    chunks = PageChunk.objects.filter(page=page)
    assert chunks.count() == 1, f"The job must have chunked the page, got {chunks.count()} chunks"
    assert all(c.embedding is not None for c in chunks), "The job must embed via the mock provider"


@th.django_unit_test()
def test_book_reindex_action(opts):
    """POST {"reindex": true} on a book queues one embed job per page."""
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.jobs.models import Job

    Job.objects.filter(func=EMBED_JOB).delete()
    PageChunk.objects.filter(page__book_id=opts.kb_book_id).delete()
    page_count = Page.objects.filter(book_id=opts.kb_book_id).count()

    ok = opts.client.login(KB_USER, KB_PWORD)
    assert ok, "Login must succeed before the reindex action"
    resp = opts.client.post(f"/api/docit/book/{opts.kb_book_id}", json={"reindex": True})
    assert resp.status_code == 200, f"Reindex action must return 200, got {resp.status_code}"

    queued = Job.objects.filter(func=EMBED_JOB, status="pending").count()
    assert queued == page_count, \
        f"Reindex must queue one job per page ({page_count}), got {queued}"
    th.run_pending_jobs()
    reindexed = PageChunk.objects.filter(page__book_id=opts.kb_book_id).count()
    assert reindexed >= page_count, \
        f"Every page must have chunks after reindex, got {reindexed} chunks for {page_count} pages"

    # A falsy action value must be a no-op (dispatch fires on key presence).
    resp = opts.client.post(f"/api/docit/book/{opts.kb_book_id}", json={"reindex": False})
    assert resp.status_code == 200, f"Falsy reindex must still return 200, got {resp.status_code}"
    pending = Job.objects.filter(func=EMBED_JOB, status="pending").count()
    assert pending == 0, f"Falsy reindex must queue nothing, got {pending} pending jobs"

    # Repeat reindex of unchanged pages dedupes via idempotency keys —
    # a reindex loop cannot flood the queue.
    resp = opts.client.post(f"/api/docit/book/{opts.kb_book_id}", json={"reindex": True})
    assert resp.status_code == 200, f"Second reindex must return 200, got {resp.status_code}"
    pending = Job.objects.filter(func=EMBED_JOB, status="pending").count()
    assert pending == 0, \
        f"Unchanged pages must dedupe against prior jobs (idempotency), got {pending} new pending"


@th.django_unit_test()
def test_rest_search_endpoint(opts):
    """/api/docit/search: shape, auth gate, validation, clamping."""
    ok = opts.client.login(KB_USER, KB_PWORD)
    assert ok, "Login must succeed before searching"

    resp = opts.client.get("/api/docit/search", params={"q": "ZXQTOKEN99"})
    assert resp.status_code == 200, f"Search must return 200, got {resp.status_code}"
    data = resp.response.data
    assert data.mode == "hybrid", f"Server runs the mock provider — expected hybrid, got {data.mode}"
    assert data.count == len(data.results), f"count must match results, got {data.count} vs {len(data.results)}"
    assert data.count >= 1, "ZXQTOKEN99 must be found over REST"
    top = data.results[0]
    for field in ("page_id", "page_slug", "page_title", "book_id", "book_slug", "heading", "snippet", "score"):
        assert field in top, f"Result missing field {field}: {top}"

    resp = opts.client.get("/api/docit/search", params={"q": "ZXQTOKEN99", "book": opts.kb_book_slug})
    assert resp.status_code == 200 and resp.response.data.count >= 1, \
        f"Book-scoped search must succeed, got {resp.status_code}"

    resp = opts.client.get("/api/docit/search")
    assert resp.status_code == 400, f"Missing q must return 400, got {resp.status_code}"

    resp = opts.client.get("/api/docit/search", params={"q": "x" * 600})
    assert resp.status_code == 400, f"q over 512 chars must return 400, got {resp.status_code}"

    resp = opts.client.get("/api/docit/search", params={"q": "alpha", "limit": 9999})
    assert resp.status_code == 200, f"Oversized limit must clamp, not error — got {resp.status_code}"
    assert resp.response.data.count <= 50, \
        f"Limit must clamp to 50, got {resp.response.data.count} results"

    opts.client.logout()
    resp = opts.client.get("/api/docit/search", params={"q": "ZXQTOKEN99"})
    # requires_auth() raises PermissionDeniedException — the framework maps it to 403.
    assert resp.status_code == 403, f"Anonymous search must be rejected with 403, got {resp.status_code}"


@th.django_unit_test()
def test_search_mode_degrades_without_provider(opts):
    """Without a usable provider, search degrades to FTS mode instead of erroring.

    Tested in-process (not via th.server_settings) — a server_settings
    override of EMBEDDINGS_PROVIDER can leak into var/django.conf under
    parallel full-suite runs and poison the next run's baseline (item 543).
    The REST dispatch layer is covered by test_rest_search_endpoint.
    """
    from django.conf import settings as dj_settings
    from mojo.apps.docit_kb.services import knowledge

    original = getattr(dj_settings, "EMBEDDINGS_PROVIDER", "mock")
    dj_settings.EMBEDDINGS_PROVIDER = "bedrock"  # no AWS creds in the test env
    try:
        found = knowledge.search("ZXQTOKEN99")
        assert found.mode == "fts", f"Bedrock without creds must degrade to fts, got {found.mode}"
        assert len(found.results) >= 1, "FTS-only search must still find the exact identifier"
        assert found.results[0].page_id == opts.kb_page_beta_id, \
            f"Degraded search must still rank the beta page first, got {found.results[0].page_id}"
    finally:
        dj_settings.EMBEDDINGS_PROVIDER = original


@th.django_unit_test()
def test_fallback_page_search(opts):
    """The docit-only fallback (no docit_kb) searches pages directly."""
    from mojo.apps.docit.services.search import search_any, search_pages

    results = search_pages("ZXQTOKEN99")
    assert len(results) >= 1, "Page-level FTS fallback must find the identifier"
    top = results[0]
    assert top.page_id == opts.kb_page_beta_id, \
        f"Fallback must rank the beta page first, got page {top.page_id}"
    assert top.heading == "", "Page-level results carry no chunk heading"
    assert top.snippet, "Fallback results must include a headline snippet"

    # With docit_kb installed (this test env), the dispatcher must NOT use the fallback.
    found = search_any("ZXQTOKEN99")
    assert found.mode in ("hybrid", "fts"), \
        f"Dispatcher must route to the KB when installed, got mode {found.mode}"


# ---------------------------------------------------------------------------
# Reconciliation sweep
#
# The sweep is GLOBAL — other test modules create docit pages in parallel, so
# every assertion below is about MEMBERSHIP of a specific page id in the queued
# recon jobs, never about totals.
# ---------------------------------------------------------------------------


def _clean_recon_pages():
    """Drop every kbtest_recon* fixture (and its chunks/jobs) from prior runs."""
    from mojo.apps.docit.models import Page
    from mojo.apps.jobs.models import Job

    stale = list(Page.objects.filter(title__startswith="kbtest_recon").values_list("id", flat=True))
    Page.objects.filter(id__in=stale).delete()
    for page_id in stale:
        Job.objects.filter(func=EMBED_JOB, payload__page_id=page_id).delete()


def _recon_page(opts, suffix, content):
    """Create a kbtest_recon page and drop the embed job Page.save queued for it."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book, Page

    user = User.objects.get(pk=opts.kb_user_id)
    book = Book.objects.get(pk=opts.kb_book_id)
    page = Page.objects.create(
        book=book, title=f"kbtest_recon_{suffix}", content=content,
        user=user, created_by=user, modified_by=user)
    _drop_embed_jobs(page.pk)
    return page


def _drop_embed_jobs(page_id):
    """Simulate a dropped publish: the job row for this page never made it."""
    from mojo.apps.jobs.models import Job

    Job.objects.filter(func=EMBED_JOB, payload__page_id=page_id).delete()


def _recon_job_count(page_id):
    from mojo.apps.jobs.models import Job

    return Job.objects.filter(
        func=EMBED_JOB, payload__page_id=page_id,
        idempotency_key__startswith=RECON_KEY).count()


def _after_grace(offset_seconds=60):
    """An instant far enough past now that a just-saved page clears the grace window."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.docit_kb.services.knowledge import RECONCILE_GRACE_SEC

    return timezone.now() + timedelta(seconds=RECONCILE_GRACE_SEC + offset_seconds)


@th.django_unit_test()
def test_reconcile_heals_stale_page(opts):
    """A dropped embed publish leaves stale chunks; the sweep re-queues and heals them."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    page = _recon_page(opts, "stale", "# Stale\n\nRECONOLDTOK11 is the original body.\n")
    knowledge.embed_page_now(page)

    page.content = "# Stale\n\nRECONNEWTOK22 replaced the original body.\n"
    page.save()
    _drop_embed_jobs(page.pk)
    PageChunk.objects.filter(page=page).update(modified=page.modified - timedelta(hours=1))

    result = knowledge.reconcile_stale_pages(now=_after_grace())
    assert _recon_job_count(page.pk) == 1, \
        f"The sweep must queue exactly one recon embed job for the stale page, sweep={result}"

    th.run_pending_jobs()
    bodies = " ".join(PageChunk.objects.filter(page=page).values_list("content", flat=True))
    assert "RECONOLDTOK11" not in bodies, \
        f"The healed page must no longer serve pre-edit content, chunks={bodies!r}"
    assert "RECONNEWTOK22" in bodies, \
        f"The healed page must serve the edited content, chunks={bodies!r}"

    # Re-sweep in a later bucket with an empty job window, so nothing but the
    # watermark itself can suppress the page.
    _drop_embed_jobs(page.pk)
    later = timezone.now() + timedelta(hours=2)
    knowledge.reconcile_stale_pages(now=later)
    assert _recon_job_count(page.pk) == 0, \
        "A healed page must not be re-queued — the chunk watermark now matches the page"


@th.django_unit_test()
def test_watermark_uses_page_modified_snapshot(opts):
    """Chunks certify the page version they were built from, not 'now'."""
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    page = _recon_page(opts, "snap", "# Snap\n\nRECONSNAP33 body.\n")
    knowledge.embed_page_now(page)

    # A save landing after the embed bumps page.modified past the watermark.
    # Its embed job is dropped (queue outage) so only the watermark decides.
    page.metadata = {"note": "touched after embed"}
    page.save()
    _drop_embed_jobs(page.pk)

    result = knowledge.reconcile_stale_pages(now=_after_grace())
    assert _recon_job_count(page.pk) == 1, \
        ("Chunks stamped with the OLD page version must leave the page flagged; "
         f"stamping timezone.now() would hide it. sweep={result}")


@th.django_unit_test()
def test_reconcile_metadata_only_save_not_requeued(opts):
    """Once the embed job runs, the re-stamped watermark clears the page."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    page = _recon_page(opts, "meta", "# Meta\n\nRECONMETA44 body.\n")
    knowledge.embed_page_now(page)

    page.metadata = {"note": "metadata only"}
    page.save()
    executed = th.run_pending_jobs()
    assert executed >= 1, f"The save must queue an embed job to run, executed={executed}"

    later = timezone.now() + timedelta(hours=2)
    result = knowledge.reconcile_stale_pages(now=later)
    assert _recon_job_count(page.pk) == 0, \
        f"A page whose embed job ran must not be swept again, sweep={result}"


@th.django_unit_test()
def test_reconcile_never_chunked_heals_beyond_lookback(opts):
    """The never-chunked arm has no lookback — adoption backfill is automatic."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    page = _recon_page(opts, "ancient", "# Ancient\n\nRECONOLD55 body.\n")
    assert PageChunk.objects.filter(page=page).count() == 0, \
        "Fixture must start with no chunks — the embed job was dropped"

    # Queryset update bypasses auto_now. Both instants are parked ahead of the
    # real clock so that no page another module is writing in parallel can be
    # inside the lookback window or newer than this one.
    base = timezone.now() + timedelta(days=30)
    Page.objects.filter(pk=page.pk).update(modified=base)
    when = base + timedelta(hours=200)   # 200h > the 168h stale-arm lookback

    result = knowledge.reconcile_stale_pages(now=when)
    assert _recon_job_count(page.pk) == 1, \
        f"A never-chunked page must be queued regardless of age, sweep={result}"


@th.django_unit_test()
def test_reconcile_null_embeddings_arm(opts):
    """Null vectors are healed when a provider exists, and ignored when none does."""
    from django.conf import settings as dj_settings
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    page = _recon_page(opts, "nullvec", "# Nullvec\n\nRECONNULL66 body.\n")
    original = getattr(dj_settings, "EMBEDDINGS_PROVIDER", "mock")
    dj_settings.EMBEDDINGS_PROVIDER = "bedrock"  # no AWS creds in the test env
    try:
        knowledge.embed_page_now(page)
    finally:
        dj_settings.EMBEDDINGS_PROVIDER = original
    _drop_embed_jobs(page.pk)
    assert PageChunk.objects.filter(page=page, embedding__isnull=True).exists(), \
        "Fixture must have chunks with null embeddings"

    result = knowledge.reconcile_stale_pages(now=_after_grace())
    assert _recon_job_count(page.pk) == 1, \
        f"A chunked page with null vectors must be re-queued when a provider exists, sweep={result}"

    # No provider: null vectors are the normal steady state of an FTS-only
    # install and must never be queued, or the sweep loops forever.
    _drop_embed_jobs(page.pk)
    dj_settings.EMBEDDINGS_PROVIDER = "bedrock"
    try:
        result = knowledge.reconcile_stale_pages(now=_after_grace(120))
        assert _recon_job_count(page.pk) == 0, \
            f"Without a provider the null-embedding arm must be skipped entirely, sweep={result}"
    finally:
        dj_settings.EMBEDDINGS_PROVIDER = original


@th.django_unit_test()
def test_reconcile_blank_guard_asymmetry(opts):
    """Blank + never chunked is skipped; blank + still chunked must heal."""
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    blank = _recon_page(opts, "blank", "   \n\n  \n")
    blanked = _recon_page(opts, "blanked", "# Blanked\n\nRECONBLANK77 body.\n")
    knowledge.embed_page_now(blanked)
    blanked.content = "   \n"
    blanked.save()
    _drop_embed_jobs(blanked.pk)

    result = knowledge.reconcile_stale_pages(now=_after_grace())
    assert _recon_job_count(blank.pk) == 0, \
        f"A blank never-chunked page yields zero chunks by design and must not loop, sweep={result}"
    assert result.skipped_blank >= 1, \
        f"The blank page must be counted as skipped, sweep={result}"
    assert _recon_job_count(blanked.pk) == 1, \
        f"A blanked page that still serves chunks must heal, sweep={result}"

    th.run_pending_jobs()
    assert PageChunk.objects.filter(page=blanked).count() == 0, \
        "Healing a blanked page must remove the chunks it was still serving"
    _drop_embed_jobs(blanked.pk)
    knowledge.reconcile_stale_pages(now=_after_grace(180))
    assert _recon_job_count(blanked.pk) == 0, \
        "Once emptied, the page falls under the blank guard and must not be re-queued"


@th.django_unit_test()
def test_reconcile_grace(opts):
    """A page saved seconds ago is left to its own embed job."""
    from django.utils import timezone
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    page = _recon_page(opts, "fresh", "# Fresh\n\nRECONFRESH88 body.\n")

    result = knowledge.reconcile_stale_pages(now=timezone.now())
    assert _recon_job_count(page.pk) == 0, \
        f"A page inside the grace window must not be swept, sweep={result}"

    result = knowledge.reconcile_stale_pages(now=_after_grace())
    assert _recon_job_count(page.pk) == 1, \
        f"Past the grace window the same page must be swept, sweep={result}"


@th.django_unit_test()
def test_reconcile_inflight_and_bucket_dedupe(opts):
    """An in-flight embed suppresses the page; repeat sweeps in one bucket queue once."""
    from mojo.apps import jobs
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    page = _recon_page(opts, "inflight", "# Inflight\n\nRECONFLIGHT99 body.\n")
    jobs.publish(EMBED_JOB, {"page_id": page.pk}, max_retries=2)

    result = knowledge.reconcile_stale_pages(now=_after_grace())
    assert _recon_job_count(page.pk) == 0, \
        f"A page with a pending embed job must not be re-queued, sweep={result}"
    assert result.skipped_inflight >= 1, \
        f"The in-flight page must be counted, sweep={result}"

    executed = th.run_pending_jobs()
    assert executed >= 1, f"The in-flight embed job must run, executed={executed}"

    # Make it stale again with a dropped publish, then sweep twice in one bucket.
    page.content = "# Inflight\n\nRECONFLIGHT99 body, edited.\n"
    page.save()
    _drop_embed_jobs(page.pk)
    when = _after_grace()
    knowledge.reconcile_stale_pages(now=when)
    knowledge.reconcile_stale_pages(now=when)
    assert _recon_job_count(page.pk) == 1, \
        (f"Two sweeps in one hourly bucket must leave exactly one recon job, "
         f"got {_recon_job_count(page.pk)}")


@th.django_unit_test()
def test_reconcile_limit(opts):
    """The cap is honored and the newest edit wins the budget."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.services import knowledge

    _clean_recon_pages()
    older = _recon_page(opts, "limit_older", "# Older\n\nRECONLIMITA body.\n")
    newer = _recon_page(opts, "limit_newer", "# Newer\n\nRECONLIMITB body.\n")

    # Park both ahead of every real page so the DESC ordering is deterministic
    # even while other modules are writing pages in parallel.
    base = timezone.now() + timedelta(days=1)
    Page.objects.filter(pk=older.pk).update(modified=base)
    Page.objects.filter(pk=newer.pk).update(modified=base + timedelta(seconds=5))
    when = base + timedelta(seconds=700)

    result = knowledge.reconcile_stale_pages(limit=1, now=when)
    assert result.queued == 1, f"limit=1 must publish exactly one job, sweep={result}"
    assert _recon_job_count(newer.pk) == 1, \
        f"The newest stale page must win the budget, sweep={result}"
    assert _recon_job_count(older.pk) == 0, \
        f"The older stale page must wait for the next sweep, sweep={result}"

    _clean_recon_pages()


@th.django_unit_test()
def test_reconcile_cron_dispatcher(opts):
    """The cron dispatcher queues the sweep job, and the kill switch stops it."""
    from datetime import timedelta
    from django.conf import settings as dj_settings
    from django.utils import timezone
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb import cronjobs
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.jobs.models import Job

    _clean_recon_pages()
    Job.objects.filter(func=RECON_JOB).delete()
    page = _recon_page(opts, "cron", "# Cron\n\nRECONCRON10 body.\n")
    # The sweep runs on the real clock inside the job, so age the page out of
    # the grace window rather than passing a simulated instant.
    Page.objects.filter(pk=page.pk).update(modified=timezone.now() - timedelta(hours=2))

    job_id = cronjobs.reconcile_embeddings()
    assert job_id, f"The dispatcher must return a job id, got {job_id!r}"
    assert Job.objects.filter(func=RECON_JOB, status="pending").count() == 1, \
        "The dispatcher must queue exactly one sweep job"

    th.run_pending_jobs()   # the sweep runs and publishes per-page embed jobs
    assert _recon_job_count(page.pk) == 1, \
        "The sweep job must queue a recon embed job for the stale page"
    th.run_pending_jobs()   # the embed jobs run
    assert PageChunk.objects.filter(page=page).count() >= 1, \
        "The full cron -> sweep -> embed chain must chunk the page"

    Job.objects.filter(func=RECON_JOB).delete()
    dj_settings.DOCIT_KB_RECONCILE_ENABLED = False
    try:
        result = cronjobs.reconcile_embeddings()
        assert result is None, f"The disabled dispatcher must return None, got {result!r}"
        assert Job.objects.filter(func=RECON_JOB).count() == 0, \
            "DOCIT_KB_RECONCILE_ENABLED=False must queue nothing"
    finally:
        del dj_settings.DOCIT_KB_RECONCILE_ENABLED


# ---------------------------------------------------------------------------
# Relevance floor (max_distance / DOCIT_KB_MAX_DISTANCE)
#
# Why 0.5 and not the 0.80 shipped in the docs: under EMBEDDINGS_PROVIDER=mock
# every vector is a hash-derived unit vector in 1024 dims, so two UNRELATED
# texts sit at cosine distance 1.0 +/- 0.031 (sigma = 1/sqrt(1024)) — the
# minimum measured over 20,000 synthetic chunks was 0.886. A chunk whose text
# is byte-identical to the query embeds to the same vector, i.e. distance ~= 0.
# A 0.5 floor therefore separates the two populations with ~16 sigma of
# headroom and cannot be crossed by chance, where 0.80 leaves only ~2.7 sigma
# against whatever this long-lived database has accumulated. Both values
# exercise the identical code path.
#
# These tests live at the END of the module on purpose: the floor fixture adds
# a permanently-embedded chunk to a shared corpus, and test_hybrid_search_ranking
# above runs an unscoped search that is already flaky for that reason (item 1004).
# ---------------------------------------------------------------------------

FLOOR_QUERY = "FLOORTOKEN44 this paragraph is the entire chunk body."
NONSENSE_QUERY = "xyzzyplughnotinanypage quorbanth vexlimoor"
FLOOR = 0.5


def _floor_page(opts):
    """A page whose entire content is FLOOR_QUERY — one chunk at distance ~= 0.

    No '#' heading and a single paragraph, so the chunker yields exactly one
    chunk with heading="" and _embed_input returns the content verbatim;
    embedding FLOOR_QUERY as a query produces the identical vector.

    Deletes before creating: Page.save auto-slugs and de-duplicates with a
    -1/-2 suffix, so a create-only helper called from three tests would leave
    three distinct pages all sitting at distance ~= 0.
    """
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book, Page
    from mojo.apps.docit_kb.services import knowledge

    Page.objects.filter(book_id=opts.kb_book_id, title="kbtest_floor").delete()
    user = User.objects.get(pk=opts.kb_user_id)
    book = Book.objects.get(pk=opts.kb_book_id)
    page = Page.objects.create(
        book=book, title="kbtest_floor", content=FLOOR_QUERY,
        user=user, created_by=user, modified_by=user)
    knowledge.embed_page_now(page)
    return page


@th.django_unit_test()
def test_vector_leg_relevance_floor(opts):
    """The vector leg keeps rows within max_distance and drops the rest."""
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    floor_page = _floor_page(opts)
    # Embed what this test asserts on — the setup only creates the fixture
    # pages, so their chunks otherwise exist solely as a side effect of
    # earlier tests in this module.
    for page_id in (opts.kb_page_alpha_id, opts.kb_page_beta_id):
        knowledge.embed_page_now(Page.objects.get(pk=page_id))

    qs = PageChunk.objects.filter(
        page__book_id=opts.kb_book_id).select_related("page", "page__book")

    # Control: unfloored the leg is a plain kNN, so it returns the exact match
    # AND the unrelated neighbours riding along at distance ~= 1.0.
    rows = knowledge._vector_leg(qs, FLOOR_QUERY, 30)
    assert any(r.page_id == floor_page.pk for r in rows), \
        f"Unfloored leg must find the byte-identical chunk, got pages {[r.page_id for r in rows]}"
    assert len(rows) > 1, \
        f"Control: an unbounded kNN also returns unrelated neighbours, got {len(rows)} row(s)"

    # Floored: only the genuine match survives.
    rows = knowledge._vector_leg(qs, FLOOR_QUERY, 30, max_distance=FLOOR)
    assert [r.page_id for r in rows] == [floor_page.pk], \
        f"A {FLOOR} floor must keep only the exact match, got pages {[r.page_id for r in rows]}"

    # A query matching nothing: empty, and still a LIST. search() reads None as
    # "no provider" and would misreport mode as "fts".
    rows = knowledge._vector_leg(qs, NONSENSE_QUERY, 30, max_distance=FLOOR)
    assert rows == [], f"An unmatched query must yield no rows above the floor, got {rows}"
    assert isinstance(rows, list), \
        f"An emptied leg must stay a list, never None — got {type(rows).__name__}"

    # max_distance=0.0 is a real floor, not "off": a truthiness guard
    # (`if max_distance:`) would skip the filter and return the whole pool.
    unfloored = knowledge._vector_leg(qs, NONSENSE_QUERY, 30)
    assert len(unfloored) > 0, \
        "Control: with no floor the leg returns its nearest neighbours regardless of relevance"
    assert knowledge._vector_leg(qs, NONSENSE_QUERY, 30, max_distance=0.0) == [], \
        "max_distance=0.0 must be honored as a floor, not treated as absent"


@th.django_unit_test()
def test_fts_leg_relevance_bound(opts):
    """The FTS leg admits only genuine text matches.

    Regression: the bound used to be `rank__gt=0`, but ts_rank returns 1e-20 —
    not 0 — for a non-matching document on a multi-term query, so every chunk
    passed and the FTS leg was as unbounded as the vector leg. Both legs have
    to bound themselves or an unmatched query can never return nothing.
    """
    from mojo.apps.docit.models import Page
    from mojo.apps.docit.services.search import search_pages
    from mojo.apps.docit_kb.models import PageChunk
    from mojo.apps.docit_kb.services import knowledge

    _floor_page(opts)
    for page_id in (opts.kb_page_alpha_id, opts.kb_page_beta_id):
        knowledge.embed_page_now(Page.objects.get(pk=page_id))

    qs = PageChunk.objects.filter(
        page__book_id=opts.kb_book_id).select_related("page", "page__book")

    rows = knowledge._fts_leg(qs, NONSENSE_QUERY, 30)
    assert rows == [], \
        (f"Text that appears in no chunk must match nothing — got "
         f"{[(r.page.title, r.heading) for r in rows]}")

    # The bound must not cost genuine matches.
    rows = knowledge._fts_leg(qs, "ZXQTOKEN99", 30)
    assert [r.page_id for r in rows] == [opts.kb_page_beta_id], \
        f"The exact identifier must still match its chunk, got pages {[r.page_id for r in rows]}"
    rows = knowledge._fts_leg(qs, "alpha widgets configuration", 30)
    assert rows and all(r.page_id == opts.kb_page_alpha_id for r in rows), \
        f"Alpha terms must match alpha chunks only, got pages {[r.page_id for r in rows]}"

    # The page-level fallback (no docit_kb installed) carries the same bound.
    assert search_pages(NONSENSE_QUERY, book=opts.kb_book_slug) == [], \
        "The page-level fallback must also return nothing for unmatched text"
    found = search_pages("ZXQTOKEN99", book=opts.kb_book_slug)
    assert [r.page_id for r in found] == [opts.kb_page_beta_id], \
        f"The page-level fallback must still find real matches, got {[r.page_id for r in found]}"


@th.django_unit_test()
def test_search_relevance_floor(opts):
    """knowledge.search: floor off by default, honored via kwarg or setting."""
    from django.conf import settings as dj_settings
    from mojo.apps.docit_kb.services import knowledge

    floor_page = _floor_page(opts)
    book = opts.kb_book_slug

    # Default-off: omitting max_distance must match an explicit no-op floor
    # (2.0 is the cosine-distance maximum). This fails the day anyone ships a
    # default value for DOCIT_KB_MAX_DISTANCE without a decision.
    default = knowledge.search(FLOOR_QUERY, book=book)
    wide = knowledge.search(FLOOR_QUERY, book=book, max_distance=2.0)
    assert [r.page_id for r in default.results] == [r.page_id for r in wide.results], \
        (f"Omitting max_distance must equal an explicit no-op floor, got "
         f"{[r.page_id for r in default.results]} vs {[r.page_id for r in wide.results]}")
    assert any(r.page_id == floor_page.pk for r in default.results), \
        f"Sanity: the floor page must be findable, got {[r.page_id for r in default.results]}"

    # An unmatched query under a floor: honestly empty, and still hybrid.
    found = knowledge.search(NONSENSE_QUERY, book=book, max_distance=FLOOR)
    assert found.mode == "hybrid", \
        f"A live provider still reports hybrid when the floor empties the leg, got {found.mode}"
    assert found.results == [], \
        f"An unmatched query must return zero rows under a floor, got {found.results}"

    # The same floor must not break a genuine match end to end.
    found = knowledge.search(FLOOR_QUERY, book=book, max_distance=FLOOR)
    assert any(r.page_id == floor_page.pk for r in found.results), \
        f"The floor must not drop a genuine match, got {[r.page_id for r in found.results]}"

    # DOCIT_KB_MAX_DISTANCE applies when the caller passes nothing; the kwarg
    # overrides it. Mutated in-process rather than via th.server_settings — a
    # server_settings override can leak into var/django.conf under parallel
    # full-suite runs (item 543), and these calls need no server.
    dj_settings.DOCIT_KB_MAX_DISTANCE = FLOOR
    try:
        found = knowledge.search(NONSENSE_QUERY, book=book)
        assert found.results == [], \
            f"DOCIT_KB_MAX_DISTANCE must apply when no kwarg is given, got {found.results}"
        found = knowledge.search(NONSENSE_QUERY, book=book, max_distance=2.0)
        assert len(found.results) >= 1, \
            "An explicit max_distance must override the setting, got no results"
    finally:
        del dj_settings.DOCIT_KB_MAX_DISTANCE

    # An uncoercible setting degrades to "no floor" and logs — never raises.
    dj_settings.DOCIT_KB_MAX_DISTANCE = "not-a-number"
    try:
        found = knowledge.search(NONSENSE_QUERY, book=book)
        assert found.mode == "hybrid", \
            f"An uncoercible setting must be ignored, not fatal — got mode {found.mode}"
    finally:
        del dj_settings.DOCIT_KB_MAX_DISTANCE


@th.django_unit_test()
def test_search_any_forwards_max_distance(opts):
    """The docit dispatcher threads max_distance through to the KB search."""
    from mojo.apps.docit.services.search import search_any

    _floor_page(opts)
    book = opts.kb_book_slug

    # Control: same call, no floor — the dispatcher reaches a live vector leg.
    found = search_any(NONSENSE_QUERY, book=book)
    assert found.mode == "hybrid", \
        f"Dispatcher must reach the KB with a provider present, got mode {found.mode}"
    assert len(found.results) >= 1, \
        "Control: with no floor the dispatcher returns the vector leg's neighbours"

    found = search_any(NONSENSE_QUERY, book=book, max_distance=FLOOR)
    assert found.mode == "hybrid", \
        f"mode must stay hybrid when the floor empties the vector leg, got {found.mode}"
    assert found.results == [], \
        f"search_any must forward max_distance to knowledge.search, got {found.results}"
