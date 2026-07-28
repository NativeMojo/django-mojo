"""
Tenant-isolation regression suite for docit (maestro item 530).

Before this suite, every docit model declared VIEW_PERMS = ['all'] with no
GROUP_FIELD, so any authenticated user read every tenant's books, pages
(published or not), revisions and assets; the slug endpoints called
on_rest_get directly and served the same content to anonymous callers; and
/api/docit/search returned ranked snippets across every tenant.

Fixture shape: two orgs (A, B), one plain member per org holding NO global
permissions, and one book + published page + draft page + revision + asset
per org. Org A also owns a public book (is_public=True) used by the
public-endpoint tests.
"""
from testit import helpers as th


USER_A = "docit_sec_member_a"
USER_B = "docit_sec_member_b"
USER_GLOBAL = "docit_sec_global"
# Every book and page is owned by this account, which is a member of NEITHER
# org. That keeps "owner" out of the picture entirely, so the tests below
# exercise group membership — the thing this item is about — and never the
# incidental owner grant in SAVE_PERMS.
USER_OWNER = "docit_sec_owner"
TEST_PWORD = "docit##sec99"

ORG_A = "test_docit_sec_org_a"
ORG_B = "test_docit_sec_org_b"

# A term that appears only in org B's published page content, so a search hit
# for it from org A is unambiguously a cross-tenant leak.
SECRET_TERM_B = "zarquonium"
SECRET_TERM_A = "flibbertigib"

# Both books get a page with this slug — the page/slug collision case.
SHARED_SLUG = "test-shared-slug-page"


def _reset_user(username):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember

    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, display_name=username,
                    email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.is_active = True
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()
    user.is_staff = False
    user.is_superuser = False
    user.save()
    GroupMember.objects.filter(user=user).delete()
    return user


def _embed(page):
    """Build knowledge-base chunks for a page, if the KB app is installed."""
    from django.apps import apps
    if not apps.is_installed("mojo.apps.docit_kb"):
        return
    from mojo.apps.docit_kb.services import knowledge
    knowledge.embed_page_now(page)


def _build_org(name, owner, secret_term, is_public=False):
    """One tenant's docit content. Returns objict of the created ids."""
    from objict import objict
    from mojo.apps.account.models.group import Group
    from mojo.apps.docit.models import Book, Page, Asset

    Group.objects.filter(name=name).delete()
    group = Group(name=name, kind="organization")
    group.save()

    book = Book.objects.create(
        title=f"test_{name}_book", group=group, user=owner,
        created_by=owner, modified_by=owner)
    published = Page.objects.create(
        book=book, title=f"test_{name}_published",
        content=f"# Published\n\nThe {secret_term} protocol is documented here.",
        is_published=True, user=owner, created_by=owner, modified_by=owner)
    draft = Page.objects.create(
        book=book, title=f"test_{name}_draft",
        content=f"# Draft\n\nUnreleased {secret_term} notes.",
        is_published=False, user=owner, created_by=owner, modified_by=owner)
    # Same slug in both books — the page/slug collision that used to 500.
    shared = Page.objects.create(
        book=book, title=f"test_{name}_shared", slug=SHARED_SLUG,
        content=f"# Shared slug\n\nBelongs to {name}.",
        is_published=True, user=owner, created_by=owner, modified_by=owner)
    revision = published.create_revision(user=owner, change_summary="test_initial")
    asset = Asset.objects.create(
        book=book, alt_text=f"test_{name}_asset", user=owner, created_by=owner)

    result = objict(
        group_id=group.id, book_id=book.id, book_slug=book.slug,
        published_page_id=published.id, published_slug=published.slug,
        draft_page_id=draft.id, draft_slug=draft.slug,
        shared_page_id=shared.id, revision_id=revision.id, asset_id=asset.id)

    # Search runs over knowledge-base chunks when docit_kb is installed, and
    # those are normally built by a background job that no test worker runs.
    # Build them inline so the search assertions exercise the real query path
    # rather than an empty table. Embeddings are optional — with no provider
    # the chunks still carry text and the full-text leg answers. Only
    # published pages are embedded: drafts are excluded from search anyway, so
    # chunking them would just add noise to a shared database.
    _embed(published)

    if is_public:
        pub_book = Book.objects.create(
            title=f"test_{name}_public_book", group=group, user=owner,
            created_by=owner, modified_by=owner, is_public=True)
        pub_page = Page.objects.create(
            book=pub_book, title=f"test_{name}_public_page",
            content="# Public\n\nAnyone may read this.",
            is_published=True, user=owner, created_by=owner, modified_by=owner)
        pub_draft = Page.objects.create(
            book=pub_book, title=f"test_{name}_public_draft",
            content="# Public draft\n\nNot for the world.",
            is_published=False, user=owner, created_by=owner, modified_by=owner)
        _embed(pub_page)
        result.public_book_id = pub_book.id
        result.public_book_slug = pub_book.slug
        result.public_page_slug = pub_page.slug
        result.public_draft_slug = pub_draft.slug

    return result


@th.django_unit_setup()
def setup_docit_security(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.docit.models import Book, Page, PageRevision, Asset

    GeoLocatedIP.objects.get_or_create(
        ip_address="127.0.0.1", defaults={"subnet": "127.0.0.0/8"})

    # Tests run on long-lived databases — clear anything a prior run left.
    Asset.objects.filter(alt_text__startswith="test_docit_sec").delete()
    PageRevision.objects.filter(page__book__title__startswith="test_docit_sec").delete()
    Page.objects.filter(book__title__startswith="test_docit_sec").delete()
    Book.objects.filter(title__startswith="test_docit_sec").delete()

    user_a = _reset_user(USER_A)
    user_b = _reset_user(USER_B)
    user_owner = _reset_user(USER_OWNER)
    user_global = _reset_user(USER_GLOBAL)
    user_global.add_permission("view_docit")
    user_global.save()

    org_a = _build_org(ORG_A, user_owner, SECRET_TERM_A, is_public=True)
    org_b = _build_org(ORG_B, user_owner, SECRET_TERM_B)

    Group.objects.get(id=org_a.group_id).add_member(user_a)
    Group.objects.get(id=org_b.group_id).add_member(user_b)

    opts.user_a_id = user_a.id
    opts.user_b_id = user_b.id
    opts.org_a = org_a
    opts.org_b = org_b


def _login(opts, username):
    opts.client.logout()
    assert opts.client.login(username, TEST_PWORD), f"login failed for {username}"


def _anon(opts):
    from testit.client import RestClient
    return RestClient(host=opts.client.host, logger=opts.client.logger)


def _ids(resp):
    data = resp.response["data"]
    assert isinstance(data, list), f"Expected a list under 'data', got {data!r}"
    return [row.get("id") for row in data]


# ---------------------------------------------------------------------------
# 1. List endpoints must not cross the tenant boundary
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_member_list_excludes_other_tenants(opts):
    """A plain member of org A must never see org B's rows in any list."""
    _login(opts, USER_A)
    a, b = opts.org_a, opts.org_b

    cases = [
        ("/api/docit/book", a.book_id, b.book_id, "books"),
        ("/api/docit/page", a.published_page_id, b.published_page_id, "pages"),
        ("/api/docit/page/revision", a.revision_id, b.revision_id, "revisions"),
        ("/api/docit/book/asset", a.asset_id, b.asset_id, "assets"),
    ]
    for path, mine, theirs, label in cases:
        resp = opts.client.get(path, params={"start": 0, "size": 1000})
        assert resp.status_code == 200, \
            f"{label}: expected 200 for a member list, got {resp.status_code}: {resp.response!r}"
        ids = _ids(resp)
        assert theirs not in ids, \
            f"{label}: org B's row {theirs} leaked into org A's list at {path} (ids={ids})"
        assert mine in ids, \
            f"{label}: org A's own row {mine} missing from {path} (ids={ids})"


@th.django_unit_test()
def test_member_list_includes_own_unpublished(opts):
    """is_published gates the PUBLIC path only — members see their own drafts."""
    _login(opts, USER_A)
    resp = opts.client.get("/api/docit/page", params={"start": 0, "size": 1000})
    assert resp.status_code == 200, \
        f"Expected 200, got {resp.status_code}: {resp.response!r}"
    ids = _ids(resp)
    assert opts.org_a.draft_page_id in ids, \
        f"A member must still see their own tenant's draft {opts.org_a.draft_page_id} (ids={ids})"


# ---------------------------------------------------------------------------
# 2. Detail endpoints must deny cross-tenant reads
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_member_detail_denied_across_tenants(opts):
    """Org A's member must be refused every one of org B's rows by pk."""
    _login(opts, USER_A)
    b = opts.org_b

    cases = [
        (f"/api/docit/book/{b.book_id}", "book"),
        (f"/api/docit/page/{b.published_page_id}", "published page"),
        (f"/api/docit/page/{b.draft_page_id}", "draft page"),
        (f"/api/docit/page/revision/{b.revision_id}", "revision"),
        (f"/api/docit/book/asset/{b.asset_id}", "asset"),
    ]
    for path, label in cases:
        resp = opts.client.get(path, params={"graph": "detail"})
        assert resp.status_code in (403, 404), \
            (f"{label}: cross-tenant GET {path} must be denied, got "
             f"{resp.status_code}: {str(resp.response)[:300]}")


@th.django_unit_test()
def test_member_detail_allowed_in_own_tenant(opts):
    """The denial above must not be a blanket denial — own tenant still reads."""
    _login(opts, USER_A)
    resp = opts.client.get(f"/api/docit/book/{opts.org_a.book_id}")
    assert resp.status_code == 200, \
        (f"A member must read their own tenant's book, got {resp.status_code}: "
         f"{str(resp.response)[:300]}")
    assert resp.response["data"].get("id") == opts.org_a.book_id, \
        "Wrong book returned for own-tenant detail read"


# ---------------------------------------------------------------------------
# 3. Anonymous access
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_anonymous_denied_on_slug_and_crud_endpoints(opts):
    """The slug endpoints used to serve any page's content with no auth."""
    anon = _anon(opts)
    a = opts.org_a

    resp = anon.get(f"/api/docit/page/slug/{a.published_slug}",
                    params={"book": a.book_id, "graph": "detail"})
    assert resp.status_code in (401, 403), \
        (f"Anonymous page-by-slug must be denied, got {resp.status_code}: "
         f"{str(resp.response)[:300]}")

    resp = anon.get(f"/api/docit/book/slug/{a.book_slug}")
    assert resp.status_code in (401, 403), \
        (f"Anonymous book-by-slug must be denied, got {resp.status_code}: "
         f"{str(resp.response)[:300]}")

    for path in ("/api/docit/book", "/api/docit/page",
                 f"/api/docit/book/{a.book_id}", f"/api/docit/page/{a.published_page_id}"):
        resp = anon.get(path)
        assert resp.status_code in (401, 403), \
            f"Anonymous GET {path} must be denied, got {resp.status_code}"


@th.django_unit_test()
def test_page_by_slug_without_book_is_a_400(opts):
    """`slug` is unique per book, so the book parameter is required."""
    _login(opts, USER_A)
    resp = opts.client.get(f"/api/docit/page/slug/{opts.org_a.published_slug}")
    assert resp.status_code == 400, \
        (f"page/slug without a book param must be a 400, got {resp.status_code}: "
         f"{str(resp.response)[:300]}")


# ---------------------------------------------------------------------------
# 4. Slug collisions and misses: 200/404, never a 500
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_duplicate_slug_resolves_within_the_named_book(opts):
    """Both books hold SHARED_SLUG; .get() used to raise MultipleObjectsReturned."""
    _login(opts, USER_A)
    resp = opts.client.get(f"/api/docit/page/slug/{SHARED_SLUG}",
                           params={"book": opts.org_a.book_id})
    assert resp.status_code == 200, \
        (f"A duplicated slug scoped to my own book must resolve, got "
         f"{resp.status_code}: {str(resp.response)[:300]}")
    assert resp.response["data"].get("id") == opts.org_a.shared_page_id, \
        (f"Expected org A's page {opts.org_a.shared_page_id}, got "
         f"{resp.response['data'].get('id')}")


@th.django_unit_test()
def test_duplicate_slug_in_other_tenants_book_is_denied(opts):
    """Naming org B's book must not hand org A's member org B's page."""
    _login(opts, USER_A)
    resp = opts.client.get(f"/api/docit/page/slug/{SHARED_SLUG}",
                           params={"book": opts.org_b.book_id})
    assert resp.status_code in (403, 404), \
        (f"Cross-tenant page-by-slug must be denied, got {resp.status_code}: "
         f"{str(resp.response)[:300]}")


@th.django_unit_test()
def test_unknown_slug_is_404_not_500(opts):
    """A bare DoesNotExist used to escape as a 500."""
    _login(opts, USER_A)
    resp = opts.client.get("/api/docit/page/slug/test-no-such-page-anywhere",
                           params={"book": opts.org_a.book_id})
    assert resp.status_code == 404, \
        f"Unknown page slug must be a 404, got {resp.status_code}: {str(resp.response)[:300]}"

    resp = opts.client.get("/api/docit/book/slug/test-no-such-book-anywhere")
    assert resp.status_code == 404, \
        f"Unknown book slug must be a 404, got {resp.status_code}: {str(resp.response)[:300]}"


# ---------------------------------------------------------------------------
# 5. Cross-tenant writes via FK attach
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_cannot_create_a_page_in_another_tenants_book(opts):
    """
    A create-capable member of org A must not be able to plant a page inside
    org B's book by naming it in the body. The FK-attach VIEW check denies the
    attach, which leaves the page bookless — and that must surface as a clean
    400, not an IntegrityError 500 and not a cross-tenant row.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.apps.docit.models import Page

    user_a = User.objects.get(id=opts.user_a_id)
    member = Group.objects.get(id=opts.org_a.group_id).get_member_for_user(user_a)
    member.add_permission("manage_docit")
    member.save()

    _login(opts, USER_A)
    before = Page.objects.filter(book_id=opts.org_b.book_id).count()
    resp = opts.client.post("/api/docit/page", json={
        "group": opts.org_a.group_id,
        "book": opts.org_b.book_id,
        "title": "test_docit_sec_cross_tenant_page",
        "content": "planted",
    })
    assert resp.status_code in (400, 403), \
        (f"Creating a page in another tenant's book must fail, got "
         f"{resp.status_code}: {str(resp.response)[:300]}")
    after = Page.objects.filter(book_id=opts.org_b.book_id).count()
    assert after == before, \
        f"A cross-tenant page was created in org B's book ({before} -> {after})"


@th.django_unit_test()
def test_plain_member_cannot_save_own_tenants_book(opts):
    """VIEW_PERMS widened to members; SAVE_PERMS did not."""
    _login(opts, USER_B)
    resp = opts.client.post(f"/api/docit/book/{opts.org_b.book_id}",
                            json={"description": "test_docit_sec_member_edit"})
    assert resp.status_code in (401, 403), \
        (f"A plain member must not be able to save a book, got "
         f"{resp.status_code}: {str(resp.response)[:300]}")


# ---------------------------------------------------------------------------
# 6. Search must be tenant-scoped
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_search_does_not_cross_tenants(opts):
    """/api/docit/search returned ranked snippets of every tenant's content."""
    _login(opts, USER_A)
    resp = opts.client.get("/api/docit/search", params={"q": SECRET_TERM_B})
    assert resp.status_code == 200, \
        f"Expected 200 from search, got {resp.status_code}: {str(resp.response)[:300]}"
    results = resp.response["data"]["results"]
    leaked = [r for r in results if r.get("book_id") == opts.org_b.book_id]
    assert not leaked, \
        (f"Search leaked {len(leaked)} result(s) from org B's book to an org A "
         f"member: {leaked!r}")


@th.django_unit_test()
def test_search_finds_own_tenant_content(opts):
    """The scoping must not simply break search for everyone."""
    _login(opts, USER_A)
    resp = opts.client.get("/api/docit/search", params={"q": SECRET_TERM_A})
    assert resp.status_code == 200, \
        f"Expected 200 from search, got {resp.status_code}: {str(resp.response)[:300]}"
    results = resp.response["data"]["results"]
    assert any(r.get("book_id") == opts.org_a.book_id for r in results), \
        f"An org A member must find their own tenant's content, got {results!r}"


@th.django_unit_test()
def test_search_unrestricted_for_a_global_reader(opts):
    """A global view_docit holder keeps the platform-wide view."""
    _login(opts, USER_GLOBAL)
    resp = opts.client.get("/api/docit/search", params={"q": SECRET_TERM_B})
    assert resp.status_code == 200, \
        f"Expected 200 from search, got {resp.status_code}: {str(resp.response)[:300]}"
    results = resp.response["data"]["results"]
    assert any(r.get("book_id") == opts.org_b.book_id for r in results), \
        f"A global view_docit holder must still see org B's content, got {results!r}"


# ---------------------------------------------------------------------------
# 7. The public opt-in path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_public_endpoints_serve_only_opted_in_books(opts):
    anon = _anon(opts)
    a = opts.org_a

    resp = anon.get(f"/api/docit/public/book/{a.public_book_slug}")
    assert resp.status_code == 200, \
        (f"An is_public book must be readable anonymously, got {resp.status_code}: "
         f"{str(resp.response)[:300]}")
    data = resp.response["data"]
    assert data.get("slug") == a.public_book_slug, \
        f"Wrong book returned from the public endpoint: {data!r}"
    for leaked in ("user", "created_by", "modified_by", "config", "permissions"):
        assert leaked not in data, \
            f"The public book graph must not expose '{leaked}': {data!r}"

    resp = anon.get(f"/api/docit/public/book/{a.book_slug}")
    assert resp.status_code == 404, \
        (f"A book that did not opt in must 404 on the public endpoint, got "
         f"{resp.status_code}: {str(resp.response)[:300]}")


@th.django_unit_test()
def test_public_page_endpoints_hide_drafts(opts):
    anon = _anon(opts)
    a = opts.org_a

    resp = anon.get("/api/docit/public/page", params={
        "book": a.public_book_slug, "slug": a.public_page_slug})
    assert resp.status_code == 200, \
        (f"A published page of a public book must be readable anonymously, got "
         f"{resp.status_code}: {str(resp.response)[:300]}")
    assert "content" in resp.response["data"], \
        f"The public page graph must carry content: {resp.response['data']!r}"

    resp = anon.get("/api/docit/public/page", params={
        "book": a.public_book_slug, "slug": a.public_draft_slug})
    assert resp.status_code == 404, \
        (f"An unpublished page must 404 on the public endpoint, got "
         f"{resp.status_code}: {str(resp.response)[:300]}")

    resp = anon.get("/api/docit/public/pages", params={"book": a.public_book_slug})
    assert resp.status_code == 200, \
        f"Public page list must succeed, got {resp.status_code}: {str(resp.response)[:300]}"
    slugs = [row.get("slug") for row in resp.response["data"]]
    assert a.public_page_slug in slugs, \
        f"Published page missing from the public list: {slugs!r}"
    assert a.public_draft_slug not in slugs, \
        f"Draft leaked into the public list: {slugs!r}"


@th.django_unit_test()
def test_public_endpoints_ignore_a_caller_supplied_graph(opts):
    """The graph is server-pinned; ?graph=detail must not widen the response."""
    anon = _anon(opts)
    a = opts.org_a

    resp = anon.get(f"/api/docit/public/book/{a.public_book_slug}",
                    params={"graph": "detail"})
    assert resp.status_code == 200, \
        f"Expected 200, got {resp.status_code}: {str(resp.response)[:300]}"
    data = resp.response["data"]
    for leaked in ("user", "created_by", "modified_by", "config", "permissions"):
        assert leaked not in data, \
            f"?graph=detail widened the public book response ('{leaked}'): {data!r}"


@th.django_unit_test()
def test_public_endpoints_close_when_the_tenant_is_deactivated(opts):
    """A suspended tenant must stop serving public content."""
    from mojo.apps.account.models.group import Group

    anon = _anon(opts)
    a = opts.org_a
    group = Group.objects.get(id=a.group_id)
    group.is_active = False
    group.save()
    try:
        for path, params in (
            (f"/api/docit/public/book/{a.public_book_slug}", None),
            ("/api/docit/public/pages", {"book": a.public_book_slug}),
            ("/api/docit/public/page",
             {"book": a.public_book_slug, "slug": a.public_page_slug}),
        ):
            resp = anon.get(path, params=params)
            assert resp.status_code == 404, \
                (f"{path} must 404 while the owning group is deactivated, got "
                 f"{resp.status_code}: {str(resp.response)[:300]}")
    finally:
        group.is_active = True
        group.save()
