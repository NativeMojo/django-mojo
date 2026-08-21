"""
docit_kb knowledge tests that mutate django.conf.settings in the test
process — moved out of tests/test_docit/knowledge.py into this opt-in serial
package (maestro item #1839). Each assigns/deletes EMBEDDINGS_PROVIDER,
EMBEDDINGS_DIM, DOCIT_KB_RECONCILE_ENABLED or DOCIT_KB_MAX_DISTANCE on
django.conf.settings, which is a process-global mutation racing parallel
test threads even with a try/finally restore.

The setup and helpers are duplicated from tests/test_docit/knowledge.py so
this module is self-contained; this package is serial, so sharing the
kbtest_* fixture names with the source module is safe.
"""
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

    th.run_pending_jobs(func=RECON_JOB)   # sweep publishes per-page embed jobs
    assert _recon_job_count(page.pk) == 1, \
        "The sweep job must queue a recon embed job for the stale page"
    th.run_pending_jobs(
        func=EMBED_JOB, payload={"page_id": page.pk})   # embed jobs run
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

    # A setting that is uncoercible or out of range degrades to "no floor" and
    # logs — it must never raise, and must never silently empty the leg.
    # "nan" is the dangerous one: float("nan") does NOT raise, and
    # `distance <= nan` is False for every row, so an unguarded NaN would empty
    # the vector leg for every query on every tenant while still reporting
    # mode="hybrid" — indistinguishable from an empty corpus.
    for bad in ("not-a-number", "nan", "inf", -1.0, 5.0):
        dj_settings.DOCIT_KB_MAX_DISTANCE = bad
        try:
            found = knowledge.search(NONSENSE_QUERY, book=book)
            assert found.mode == "hybrid", \
                f"DOCIT_KB_MAX_DISTANCE={bad!r} must be ignored, not fatal — got mode {found.mode}"
            assert len(found.results) >= 1, \
                (f"DOCIT_KB_MAX_DISTANCE={bad!r} must fall back to NO floor, "
                 f"not silently empty the vector leg")
        finally:
            del dj_settings.DOCIT_KB_MAX_DISTANCE
