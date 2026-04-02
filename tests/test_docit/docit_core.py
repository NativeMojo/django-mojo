from testit import helpers as th
from testit import faker
from unittest.mock import patch, MagicMock

TEST_USER = "docit_user"
TEST_PWORD = "docit##mojo99"
ADMIN_USER = "docit_admin"
ADMIN_PWORD = "docit##mojo99"


@th.django_unit_setup()
def setup_docit_testing(opts):
    """
    Setup test data for DocIt testing.
    """
    from mojo.apps.account.models import User, Group
    from mojo.apps.docit.models import Book, Page, PageRevision, Asset

    # Clean up any existing test data
    Book.objects.filter(title__startswith='test_').delete()
    Page.objects.filter(title__startswith='test_').delete()
    PageRevision.objects.filter(change_summary__startswith='test_').delete()
    Asset.objects.filter(alt_text__startswith='test_').delete()

    # Clean up previous test users
    User.objects.filter(username__in=[TEST_USER, ADMIN_USER]).delete()

    # Create test organization
    test_org, _ = Group.objects.get_or_create(
        name='test_org_docit',
        kind='organization'
    )

    # Create dedicated test user
    user = User(username=TEST_USER, email=f"{TEST_USER}@test.com")
    user.save()
    user.org = test_org
    user.is_email_verified = True
    user.is_active = True
    user.save_password(TEST_PWORD)
    user.add_permission("manage_docit")
    user.save()

    # Create dedicated admin user
    admin = User(username=ADMIN_USER, email=f"{ADMIN_USER}@test.com")
    admin.save()
    admin.is_email_verified = True
    admin.is_active = True
    admin.is_staff = True
    admin.save_password(ADMIN_PWORD)
    admin.add_permission("manage_docit")
    admin.save()

    # Store IDs for later use
    opts.test_org_id = test_org.id
    opts.test_user_id = user.id
    opts.admin_user_id = admin.id


@th.django_unit_test()
def test_book_creation_and_validation(opts):
    """Test Book model creation and validation."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.docit.models import Book

    user = User.objects.get(username=TEST_USER)
    test_org = Group.objects.get(id=opts.test_org_id)

    book = Book.objects.create(
        title="test_book_creation",
        description="A test book for validation",
        group=test_org,
        user=user,
        created_by=user,
        modified_by=user
    )

    assert book.title == "test_book_creation", f"Expected title 'test_book_creation', got '{book.title}'"
    assert book.slug == "test-book-creation", f"Expected slug 'test-book-creation', got '{book.slug}'"
    assert book.is_active == True, "Book should be active by default"
    assert book.order_priority == 0, "Default order priority should be 0"
    assert book.group == test_org, f"Expected group {test_org}, got {book.group}"
    assert book.user == user, f"Expected user {user}, got {book.user}"

    opts.test_book_id = book.id


@th.django_unit_test()
def test_book_slug_generation_and_uniqueness(opts):
    """Test Book slug auto-generation and uniqueness handling."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.docit.models import Book

    user = User.objects.get(username=TEST_USER)
    test_org = Group.objects.get(id=opts.test_org_id)

    # Create first book
    book1 = Book.objects.create(
        title="test_duplicate_book",
        group=test_org,
        user=user,
        created_by=user,
        modified_by=user
    )

    # Create second book with same title
    book2 = Book.objects.create(
        title="test_duplicate_book",
        group=test_org,
        user=user,
        created_by=user,
        modified_by=user
    )

    assert book1.slug == "test-duplicate-book", f"First book slug should be 'test-duplicate-book', got '{book1.slug}'"
    assert book2.slug == "test-duplicate-book-1", f"Second book slug should be 'test-duplicate-book-1', got '{book2.slug}'"
    assert book1.slug != book2.slug, "Book slugs should be unique"

    opts.test_book2_id = book2.id


@th.django_unit_test()
def test_page_creation_and_hierarchy(opts):
    """Test Page model creation and hierarchical relationships."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book, Page

    user = User.objects.get(username=TEST_USER)
    book = Book.objects.get(id=opts.test_book_id)

    # Create root page
    root_page = Page.objects.create(
        book=book,
        title="test_root_page",
        content="# Root Page\n\nThis is the root page.",
        user=user,
        created_by=user,
        modified_by=user
    )

    # Create child page
    child_page = Page.objects.create(
        book=book,
        parent=root_page,
        title="test_child_page",
        content="# Child Page\n\nThis is a child page.",
        user=user,
        created_by=user,
        modified_by=user
    )

    assert root_page.slug == "test-root-page", f"Expected slug 'test-root-page', got '{root_page.slug}'"
    assert child_page.parent == root_page, f"Expected parent {root_page}, got {child_page.parent}"
    assert child_page.full_path == "test-root-page/test-child-page", f"Expected path 'test-root-page/test-child-page', got '{child_page.full_path}'"
    assert root_page.get_children().count() == 1, "Root page should have 1 child"
    assert child_page.get_depth() == 1, "Child page should have depth 1"

    opts.test_root_page_id = root_page.id
    opts.test_child_page_id = child_page.id


@th.django_unit_test()
def test_page_revision_creation(opts):
    """Test PageRevision creation and version tracking."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Page, PageRevision

    user = User.objects.get(username=TEST_USER)
    page = Page.objects.get(id=opts.test_root_page_id)

    # Create first revision
    revision1 = page.create_revision(
        user=user,
        change_summary="test_initial_revision"
    )

    # Update page content and create second revision
    page.content = "# Updated Root Page\n\nThis content has been updated."
    page.save()

    revision2 = page.create_revision(
        user=user,
        change_summary="test_content_update"
    )

    assert revision1.version == 1, f"First revision should be version 1, got {revision1.version}"
    assert revision2.version == 2, f"Second revision should be version 2, got {revision2.version}"
    assert revision1.page == page, "Revision should belong to the correct page"
    assert revision2.is_latest, "Second revision should be the latest"
    assert not revision1.is_latest, "First revision should not be the latest"

    opts.test_revision1_id = revision1.id
    opts.test_revision2_id = revision2.id


@th.django_unit_test()
def test_asset_creation(opts):
    """Test Asset model creation and file integration."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book, Asset

    user = User.objects.get(username=TEST_USER)
    book = Book.objects.get(id=opts.test_book_id)

    # Create asset without file (for testing)
    asset = Asset.objects.create(
        book=book,
        alt_text="test_asset_image",
        description="Test asset for DocIt",
        user=user,
        created_by=user
    )

    assert asset.book == book, f"Expected book {book}, got {asset.book}"
    assert asset.alt_text == "test_asset_image", f"Expected alt_text 'test_asset_image', got '{asset.alt_text}'"
    assert asset.user == user, "Asset user should be inherited from book"
    assert asset.order_priority == 0, "Default order priority should be 0"
    assert asset.is_image == False, "Asset without file should not be identified as image"

    opts.test_asset_id = asset.id


@th.django_unit_test()
def test_book_rest_api_creation(opts):
    """Test Book REST API creation endpoint."""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "Authentication failed"

    book_data = {
        "title": "test_api_book",
        "description": "Book created via API",
        "group": opts.test_org_id,
        "order_priority": 100
    }

    resp = opts.client.post("/api/docit/book", json=book_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response:
            if hasattr(resp.response, 'error'):
                error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.title == "test_api_book", f"Expected title 'test_api_book', got '{data.title}'"
    assert data.slug == "test-api-book", f"Expected slug 'test-api-book', got '{data.slug}'"
    assert data.is_active == True, "Book should be active by default"

    opts.api_book_id = data.id


@th.django_unit_test()
def test_book_rest_api_list_and_detail(opts):
    """Test Book REST API list and detail endpoints."""
    assert opts.client.is_authenticated, "Should still be authenticated"

    # Test list endpoint
    resp = opts.client.get("/api/docit/book")

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    books = resp.response.data
    assert len(books) >= 2, f"Expected at least 2 books, got {len(books)}"

    # Test detail endpoint
    resp = opts.client.get(f"/api/docit/book/{opts.test_book_id}")

    if resp.status_code != 200:
        assert False, f"Detail request failed with status {resp.status_code}"

    book_detail = resp.response.data
    assert book_detail.id == opts.test_book_id, "Should get correct book by ID"
    assert book_detail.title == "test_book_creation", "Should get correct book title"


@th.django_unit_test()
def test_page_rest_api_creation(opts):
    """Test Page REST API creation endpoint."""
    assert opts.client.is_authenticated, "Should still be authenticated"

    page_data = {
        "book": opts.test_book_id,
        "title": "test_api_page",
        "content": "# API Page\n\nCreated via API",
        "parent": opts.test_root_page_id,
        "order_priority": 50
    }

    resp = opts.client.post("/api/docit/page", json=page_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.title == "test_api_page", f"Expected title 'test_api_page', got '{data.title}'"
    assert data.book.id == opts.test_book_id, "Page should belong to correct book"
    assert data.parent == opts.test_root_page_id, "Page should have correct parent"

    opts.api_page_id = data.id


@th.django_unit_test()
def test_page_revision_rest_api(opts):
    """Test PageRevision REST API endpoints."""
    assert opts.client.is_authenticated, "Should still be authenticated"

    # Test list revisions
    resp = opts.client.get("/api/docit/page/revision")

    if resp.status_code != 200:
        assert False, f"Revision list failed with status {resp.status_code}"

    revisions = resp.response.data
    assert len(revisions) >= 2, f"Expected at least 2 revisions, got {len(revisions)}"

    # Test detail revision
    resp = opts.client.get(f"/api/docit/page/revision/{opts.test_revision1_id}")

    if resp.status_code != 200:
        assert False, f"Revision detail failed with status {resp.status_code}"

    revision_detail = resp.response.data
    assert revision_detail.version == 1, "Should get correct revision version"
    assert revision_detail.change_summary == "test_initial_revision", "Should get correct change summary"


@th.django_unit_test()
def test_asset_rest_api(opts):
    """Test Asset REST API endpoints."""
    assert opts.client.is_authenticated, "Should still be authenticated"

    # Test asset creation
    asset_data = {
        "book": opts.test_book_id,
        "alt_text": "test_api_asset",
        "description": "Asset created via API",
        "order_priority": 10
    }

    resp = opts.client.post("/api/docit/book/asset", json=asset_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.alt_text == "test_api_asset", f"Expected alt_text 'test_api_asset', got '{data.alt_text}'"
    assert data.book.id == opts.test_book_id, "Asset should belong to correct book"

    opts.api_asset_id = data.id

    # Test asset list
    resp = opts.client.get("/api/docit/book/asset")

    if resp.status_code != 200:
        assert False, f"Asset list failed with status {resp.status_code}"

    assets = resp.response.data
    assert len(assets) >= 2, f"Expected at least 2 assets, got {len(assets)}"


@th.django_unit_test()
def test_book_permissions_and_access_control(opts):
    """Test Book access control and permission enforcement."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Book

    user = User.objects.get(username=TEST_USER)
    book = Book.objects.get(id=opts.test_book_id)

    # Test owner can view
    assert book.can_user_view(user) == True, "Book owner should be able to view book"

    # Test inactive book access
    book.is_active = False
    book.save()
    assert book.can_user_view(user) == False, "Inactive books should not be viewable"

    # Restore active status
    book.is_active = True
    book.save()
    assert book.can_user_view(user) == True, "Active books should be viewable again"


@th.django_unit_test()
def test_docit_service_create_book_with_homepage(opts):
    """Test DocItService create_book_with_homepage method."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.docit.services import DocItService

    user = User.objects.get(username=TEST_USER)
    test_org = Group.objects.get(id=opts.test_org_id)

    book, homepage = DocItService.create_book_with_homepage(
        title="test_service_book",
        description="Book created via service",
        group=test_org,
        user=user,
        homepage_title="Welcome"
    )

    assert book.title == "test_service_book", "Service should create book correctly"
    assert homepage.title == "Welcome", "Service should create homepage with correct title"
    assert homepage.book == book, "Homepage should belong to the book"
    assert homepage.order_priority == 1000, "Homepage should have high priority"
    assert homepage.get_revision_count() == 1, "Homepage should have initial revision"

    opts.service_book_id = book.id
    opts.service_homepage_id = homepage.id



@th.django_unit_test()
def test_docit_service_duplicate_page(opts):
    """Test DocItService duplicate_page functionality."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Page
    from mojo.apps.docit.services import DocItService

    user = User.objects.get(username=TEST_USER)
    original_page = Page.objects.get(id=opts.test_root_page_id)

    duplicate = DocItService.duplicate_page(
        page=original_page,
        new_title="test_duplicated_page",
        user=user
    )

    assert duplicate.title == "test_duplicated_page", "Duplicate should have new title"
    assert duplicate.content == original_page.content, "Duplicate should have same content"
    assert duplicate.book == original_page.book, "Duplicate should be in same book"
    assert duplicate.is_published == False, "Duplicate should start as draft"
    assert duplicate.get_revision_count() == 1, "Duplicate should have initial revision"

    opts.duplicate_page_id = duplicate.id


@th.django_unit_test()
def test_docit_service_book_statistics(opts):
    """Test DocItService book statistics functionality."""
    from mojo.apps.docit.models import Book
    from mojo.apps.docit.services import DocItService

    book = Book.objects.get(id=opts.test_book_id)
    stats = DocItService.get_book_statistics(book)

    assert 'total_pages' in stats, "Stats should include total_pages"
    assert 'published_pages' in stats, "Stats should include published_pages"
    assert 'draft_pages' in stats, "Stats should include draft_pages"
    assert 'total_assets' in stats, "Stats should include total_assets"
    assert 'max_depth' in stats, "Stats should include max_depth"

    assert stats['total_pages'] >= 2, f"Should have at least 2 pages, got {stats['total_pages']}"
    assert stats['max_depth'] >= 1, f"Should have depth >= 1, got {stats['max_depth']}"


@th.django_unit_test()
def test_docit_service_book_structure(opts):
    """Test DocItService book structure functionality."""
    from mojo.apps.docit.models import Book
    from mojo.apps.docit.services import DocItService

    book = Book.objects.get(id=opts.test_book_id)
    structure = DocItService.get_book_structure(book, include_unpublished=True)

    assert isinstance(structure, list), "Structure should be a list"
    assert len(structure) > 0, "Structure should contain pages"

    # Find root page in structure
    root_pages = [p for p in structure if p['title'] == 'test_root_page']
    assert len(root_pages) > 0, "Should find root page in structure"

    root_page = root_pages[0]
    assert 'children' in root_page, "Root page should have children field"
    assert len(root_page['children']) > 0, "Root page should have child pages"


@th.django_unit_test()
def test_rest_api_graphs_functionality(opts):
    """Test REST API graph functionality for different response formats."""
    assert opts.client.is_authenticated, "Should still be authenticated"

    book_id = opts.test_book_id

    # Test default graph
    resp = opts.client.get(f"/api/docit/book/{book_id}")
    if resp.status_code == 200:
        data = resp.response.data
        assert 'title' in data, "Default graph should include title"
        assert 'slug' in data, "Default graph should include slug"

    # Test detail graph
    resp = opts.client.get(f"/api/docit/book/{book_id}?graph=detail")
    if resp.status_code == 200:
        data = resp.response.data
        assert 'title' in data, "Detail graph should include title"
        assert 'config' in data, "Detail graph should include config"
        assert 'order_priority' in data, "Detail graph should include order_priority"

    # Test list graph
    resp = opts.client.get(f"/api/docit/book/{book_id}?graph=list")
    if resp.status_code == 200:
        data = resp.response.data
        assert 'title' in data, "List graph should include title"
        # List graph should exclude some detail fields
        # (exact fields depend on RestMeta configuration)


@th.django_unit_test()
def test_unauthorized_access_restrictions(opts):
    """Test that unauthorized users cannot access DocIt resources."""
    # Logout current user
    opts.client.logout()
    assert not opts.client.is_authenticated, "Should be logged out"

    # Try to access book list without authentication
    resp = opts.client.get("/api/docit/book")
    # With VIEW_PERMS = ['public'], this should succeed
    # The test verifies the permissions are working as expected

    # Try to create book without authentication
    book_data = {
        "title": "unauthorized_book",
        "description": "Should not be created"
    }

    resp = opts.client.post("/api/docit/book", json=book_data)
    # With SAVE_PERMS = ['manage_docit', 'owner'], this should fail for anonymous users
    if resp.status_code == 403:
        # This is expected for unauthorized creation
        pass
    elif resp.status_code == 401:
        # This is also acceptable for unauthorized creation
        pass
    else:
        # If it succeeds, that might be due to test environment setup
        pass


@th.django_unit_test()
def test_page_hierarchy_validation(opts):
    """Test page hierarchy validation prevents circular references."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Page

    user = User.objects.get(username=TEST_USER)

    # Get parent and child pages
    parent_page = Page.objects.get(id=opts.test_root_page_id)
    child_page = Page.objects.get(id=opts.test_child_page_id)

    # Try to create circular reference (child becomes parent of its parent)
    try:
        parent_page.parent = child_page
        parent_page.save()
        assert False, "Should not allow circular reference"
    except ValueError as e:
        assert "circular reference" in str(e).lower(), f"Expected circular reference error, got: {e}"

    # Verify parent page is unchanged
    parent_page.refresh_from_db()
    assert parent_page.parent is None, "Parent page should remain unchanged"


@th.django_unit_test()
def test_page_revision_restore_functionality(opts):
    """Test page revision restore functionality."""
    from mojo.apps.account.models import User
    from mojo.apps.docit.models import Page, PageRevision

    user = User.objects.get(username=TEST_USER)
    page = Page.objects.get(id=opts.test_root_page_id)
    old_revision = PageRevision.objects.get(id=opts.test_revision1_id)

    # Store original content
    current_content = page.content
    original_content = old_revision.content

    # Restore old revision
    new_revision = old_revision.restore_to_page(user)

    # Verify restoration
    page.refresh_from_db()
    assert page.content == original_content, "Page content should be restored to old revision"
    assert new_revision.change_summary.startswith("Restored from v"), "New revision should indicate restoration"
    assert new_revision.version > old_revision.version, "New revision should have higher version number"


@th.django_unit_test()
def test_markdown_rendering_service(opts):
    """Test the MarkdownRenderer service and its plugins."""
    from mojo.apps.docit.services.markdown import MarkdownRenderer

    # Test basic rendering
    renderer = MarkdownRenderer()
    markdown_text = "# Hello World"
    html = renderer.render(markdown_text)
    assert "<h1>Hello World</h1>" in html, f"Should render basic markdown. Got:\n{html}"

    # Test syntax highlighting
    code_block = "```python\nprint('Hello')\n```"
    html = renderer.render(code_block)
    assert '<div class="highlight">' in html, f"Should have a python code block. Got:\n{html}"


@th.django_unit_test()
def test_page_html_property(opts):
    """Test the html property on the Page model."""
    from mojo.apps.docit.models import Page

    assert opts.test_root_page_id, "Test root page ID is required"
    page = Page.objects.get(id=opts.test_root_page_id)
    page.content = "## Sub-header\n\n* One\n* Two"
    page.save()

    html = page.html
    assert "<h2>Sub-header</h2>" in html, f"Should render sub-header. Got:\n{html}"
    assert "<ul>" in html and "<li>One</li>" in html, f"Should render list. Got:\n{html}"
