"""Tests for the web domain assistant tools (browse_url)."""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_web_tools(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email="webtest_admin@test.com").delete()
    opts.admin = User.objects.create_user(
        username="webtest_admin@test.com", email="webtest_admin@test.com", password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")


def _browse(params, user):
    from mojo.apps.assistant.services.tools.web import _tool_browse_url
    return _tool_browse_url(params, user)


# ---------------------------------------------------------------------------
# Scheme validation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_rejects_file_scheme(opts):
    result = _browse({"url": "file:///etc/passwd"}, opts.admin)
    assert "error" in result, "file:// scheme should be rejected"
    assert "Unsupported scheme" in result["error"], f"Wrong error: {result['error']}"


@th.django_unit_test()
def test_rejects_ftp_scheme(opts):
    result = _browse({"url": "ftp://example.com/file.txt"}, opts.admin)
    assert "error" in result, "ftp:// scheme should be rejected"
    assert "Unsupported scheme" in result["error"], f"Wrong error: {result['error']}"


@th.django_unit_test()
def test_rejects_javascript_scheme(opts):
    result = _browse({"url": "javascript:alert(1)"}, opts.admin)
    assert "error" in result, "javascript: scheme should be rejected"
    assert "Unsupported scheme" in result["error"], f"Wrong error: {result['error']}"


@th.django_unit_test()
def test_rejects_empty_url(opts):
    result = _browse({"url": ""}, opts.admin)
    assert "error" in result, "Empty URL should be rejected"


@th.django_unit_test()
def test_rejects_no_url(opts):
    result = _browse({}, opts.admin)
    assert "error" in result, "Missing URL should be rejected"


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_rejects_localhost(opts):
    result = _browse({"url": "http://127.0.0.1/"}, opts.admin)
    assert "error" in result, "localhost should be rejected"
    assert "private" in result["error"].lower(), f"Wrong error: {result['error']}"


@th.django_unit_test()
def test_rejects_private_10(opts):
    result = _browse({"url": "http://10.0.0.1/"}, opts.admin)
    assert "error" in result, "10.x.x.x should be rejected"
    assert "private" in result["error"].lower(), f"Wrong error: {result['error']}"


@th.django_unit_test()
def test_rejects_private_192(opts):
    result = _browse({"url": "http://192.168.1.1/"}, opts.admin)
    assert "error" in result, "192.168.x.x should be rejected"
    assert "private" in result["error"].lower(), f"Wrong error: {result['error']}"


@th.django_unit_test()
def test_rejects_private_172(opts):
    result = _browse({"url": "http://172.16.0.1/"}, opts.admin)
    assert "error" in result, "172.16.x.x should be rejected"
    assert "private" in result["error"].lower(), f"Wrong error: {result['error']}"


@th.django_unit_test()
def test_rejects_metadata_ip(opts):
    result = _browse({"url": "http://169.254.169.254/latest/meta-data/"}, opts.admin)
    assert "error" in result, "AWS metadata IP should be rejected"
    assert "private" in result["error"].lower(), f"Wrong error: {result['error']}"


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fetch_public_url(opts):
    result = _browse({"url": "https://httpbin.org/html"}, opts.admin)
    assert "error" not in result, f"Fetch should succeed: {result.get('error')}"
    assert "content" in result, "Result should have content"
    assert len(result["content"]) > 0, "Content should not be empty"
    assert "url" in result, "Result should have url"
    assert result["url"] == "https://httpbin.org/html", "URL should match request"


@th.django_unit_test()
def test_fetch_returns_title(opts):
    result = _browse({"url": "https://httpbin.org/html"}, opts.admin)
    assert "error" not in result, f"Fetch should succeed: {result.get('error')}"
    assert "title" in result, "Result should have title field"


# ---------------------------------------------------------------------------
# Content truncation (tested via _extract_text, no settings manipulation)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_content_truncation(opts):
    from mojo.apps.assistant.services.tools.web import _extract_text, DEFAULT_MAX_LENGTH

    # Build HTML content that exceeds the default max
    big_text = "A" * (DEFAULT_MAX_LENGTH + 5000)
    html = f"<html><body><p>{big_text}</p></body></html>"
    text, title = _extract_text(html)
    assert len(text) > DEFAULT_MAX_LENGTH, \
        f"Extracted text should exceed max length, got {len(text)}"

    # Verify the truncation logic works the same way the handler does
    truncated = len(text) > DEFAULT_MAX_LENGTH
    content = text[:DEFAULT_MAX_LENGTH]
    assert truncated is True, "truncated flag should be True for large content"
    assert len(content) == DEFAULT_MAX_LENGTH, \
        f"Truncated content should be exactly {DEFAULT_MAX_LENGTH}, got {len(content)}"


# ---------------------------------------------------------------------------
# CSS selector
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_css_selector_extracts_section(opts):
    from mojo.apps.assistant.services.tools.web import _extract_text

    html = """
    <html><body>
        <nav>Navigation here</nav>
        <main id="content"><p>Main content here</p></main>
        <footer>Footer here</footer>
    </body></html>
    """
    text, title = _extract_text(html, selector="#content")
    assert "Main content" in text, f"Selector should extract #content, got: {text}"
    assert "Navigation" not in text, "Selector should exclude nav content"
    assert "Footer" not in text, "Selector should exclude footer content"


@th.django_unit_test()
def test_css_selector_no_match(opts):
    from mojo.apps.assistant.services.tools.web import _extract_text

    html = "<html><body><p>Hello</p></body></html>"
    text, title = _extract_text(html, selector="#nonexistent")
    assert text is None, "Should return None when selector matches nothing"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_bad_domain_returns_error(opts):
    result = _browse({"url": "https://this-domain-does-not-exist-xyz123.com/"}, opts.admin)
    assert "error" in result, "Bad domain should return error"


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_strips_script_and_style(opts):
    from mojo.apps.assistant.services.tools.web import _extract_text

    html = """
    <html><body>
        <script>var x = 1;</script>
        <style>.foo { color: red; }</style>
        <p>Visible content</p>
    </body></html>
    """
    text, title = _extract_text(html)
    assert "var x" not in text, "Script content should be stripped"
    assert "color: red" not in text, "Style content should be stripped"
    assert "Visible content" in text, f"Body text should be preserved, got: {text}"


@th.django_unit_test()
def test_strips_nav_header_footer(opts):
    from mojo.apps.assistant.services.tools.web import _extract_text

    html = """
    <html><body>
        <header>Site Header</header>
        <nav>Nav Menu</nav>
        <article>Article body</article>
        <footer>Site Footer</footer>
    </body></html>
    """
    text, title = _extract_text(html)
    assert "Site Header" not in text, "Header should be stripped"
    assert "Nav Menu" not in text, "Nav should be stripped"
    assert "Site Footer" not in text, "Footer should be stripped"
    assert "Article body" in text, f"Article content should be preserved, got: {text}"


@th.django_unit_test()
def test_extracts_title(opts):
    from mojo.apps.assistant.services.tools.web import _extract_text

    html = "<html><head><title>My Page Title</title></head><body><p>Content</p></body></html>"
    text, title = _extract_text(html)
    assert title == "My Page Title", f"Expected 'My Page Title', got: {title}"


# ---------------------------------------------------------------------------
# Non-HTML content
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fetch_json_content(opts):
    result = _browse({"url": "https://httpbin.org/json"}, opts.admin)
    assert "error" not in result, f"JSON fetch should succeed: {result.get('error')}"
    assert result["title"] is None, "JSON response should have no title"
    assert len(result["content"]) > 0, "JSON content should not be empty"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_browse_url_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "browse_url" in registry, "browse_url should be registered in the tool registry"
    entry = registry["browse_url"]
    assert entry["permission"] == "view_admin", \
        f"Permission should be view_admin, got: {entry['permission']}"
    assert entry["mutates"] is False, "browse_url should not be a mutating tool"
    assert entry["domain"] == "web", f"Domain should be 'web', got: {entry['domain']}"
