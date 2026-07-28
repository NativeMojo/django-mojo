from testit import helpers as th

KB_USER = "kbtest_user"
KB_PWORD = "kbtest##mojo99"
EMBED_JOB = "mojo.apps.docit_kb.asyncjobs.embed_page"

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

    unfiltered = knowledge.search("COMMONTOK77")
    assert {r.book_id for r in unfiltered.results} == {opts.kb_book_id, other_book.pk}, \
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
