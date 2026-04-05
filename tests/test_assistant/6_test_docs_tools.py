"""Tests for the docs domain assistant tools (read_docs)."""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_docs_tools(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email="docstest_admin@test.com").delete()
    opts.admin = User.objects.create_user(
        username="docstest_admin@test.com", email="docstest_admin@test.com", password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")


def _read_docs(params, user):
    from mojo.apps.assistant.services.tools.docs import _tool_read_docs
    return _tool_read_docs(params, user)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_rejects_no_params(opts):
    result = _read_docs({}, opts.admin)
    assert "error" in result, "Should error when no path or topic provided"


@th.django_unit_test()
def test_rejects_path_traversal(opts):
    result = _read_docs({"path": "../../../etc/passwd"}, opts.admin)
    assert "error" in result, "Path traversal should be rejected"
    assert ".." in result["error"], f"Error should mention traversal: {result['error']}"


@th.django_unit_test()
def test_strips_leading_docs(opts):
    """Path starting with 'docs/' should be normalized."""
    from mojo.apps.assistant.services.tools.docs import _normalize_path
    clean, err = _normalize_path("docs/django_developer/README.md")
    assert err is None, f"Should not error: {err}"
    assert clean == "django_developer/README.md", f"Should strip docs/ prefix, got: {clean}"


@th.django_unit_test()
def test_adds_readme_to_directory_path(opts):
    from mojo.apps.assistant.services.tools.docs import _normalize_path
    clean, err = _normalize_path("django_developer/account")
    assert err is None, f"Should not error: {err}"
    assert clean == "django_developer/account/README.md", f"Should append README.md, got: {clean}"


# ---------------------------------------------------------------------------
# Direct path fetch
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fetch_known_doc_path(opts):
    result = _read_docs({"path": "django_developer/assistant/README.md"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "content" in result, "Result should have content"
    assert len(result["content"]) > 100, f"Content should be substantial, got {len(result['content'])} chars"
    assert result["path"] == "django_developer/assistant/README.md", \
        f"Path should match, got: {result['path']}"


@th.django_unit_test()
def test_fetch_with_docs_prefix(opts):
    """Path starting with docs/ should still work."""
    result = _read_docs({"path": "docs/django_developer/assistant/README.md"}, opts.admin)
    assert "error" not in result, f"Should succeed with docs/ prefix: {result.get('error')}"
    assert result["path"] == "django_developer/assistant/README.md", \
        f"Path should be normalized, got: {result['path']}"


@th.django_unit_test()
def test_fetch_nonexistent_path(opts):
    result = _read_docs({"path": "django_developer/nonexistent_page.md"}, opts.admin)
    assert "error" in result, "Nonexistent path should return error"


# ---------------------------------------------------------------------------
# Topic-based lookup
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_topic_finds_matching_doc(opts):
    result = _read_docs({"topic": "push notifications"}, opts.admin)
    assert "error" not in result, f"Topic search should succeed: {result.get('error')}"
    assert "content" in result, "Result should have content"
    assert "path" in result, "Result should have resolved path"
    # "push notifications" appears in the account section description
    assert "account" in result["path"].lower(), \
        f"Topic 'push notifications' should resolve to account docs, got: {result['path']}"


@th.django_unit_test()
def test_topic_finds_jobs_doc(opts):
    result = _read_docs({"topic": "job queue"}, opts.admin)
    assert "error" not in result, f"Topic search should succeed: {result.get('error')}"
    assert "jobs" in result["path"].lower(), \
        f"Topic 'job queue' should resolve to a jobs doc, got: {result['path']}"


@th.django_unit_test()
def test_topic_not_found_returns_index(opts):
    result = _read_docs({"topic": "xyzzy_nonexistent_feature_12345"}, opts.admin)
    assert "error" not in result, f"Unknown topic should not hard-error: {result.get('error')}"
    assert "note" in result, "Should include a note about no match"
    assert result["path"] == "django_developer/README.md", \
        f"Should return the index, got: {result['path']}"


# ---------------------------------------------------------------------------
# Content truncation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_truncation_flag(opts):
    from mojo.apps.assistant.services.tools.docs import DEFAULT_MAX_LENGTH

    result = _read_docs({"path": "django_developer/README.md"}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "truncated" in result, "Result should have truncated flag"
    assert "content_length" in result, "Result should have content_length"
    # The README is small enough to not be truncated
    if result["content_length"] <= DEFAULT_MAX_LENGTH:
        assert result["truncated"] is False, "Small doc should not be truncated"


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_find_topic_in_index(opts):
    from mojo.apps.assistant.services.tools.docs import _find_topic_in_index

    index = """## Built-in Apps
| [account/](account/README.md) | User, Group, JWT authentication, permissions, push notifications |
| [jobs/](jobs/README.md) | Async job queue — publishing, scheduling, retries |
| [chat/](chat/README.md) | Real-time chat rooms, messages |
"""
    matches = _find_topic_in_index(index, "job queue")
    assert len(matches) > 0, "Should find at least one match for 'job queue'"
    assert any("jobs" in path for _, path in matches), \
        f"Should match jobs doc, got: {matches}"


@th.django_unit_test()
def test_find_topic_no_match(opts):
    from mojo.apps.assistant.services.tools.docs import _find_topic_in_index

    index = "| [account/](account/README.md) | User management |"
    matches = _find_topic_in_index(index, "xyzzy_nothing")
    assert len(matches) == 0, f"Should find no matches, got: {matches}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_read_docs_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "read_docs" in registry, "read_docs should be registered in the tool registry"
    entry = registry["read_docs"]
    assert entry["permission"] == "view_admin", \
        f"Permission should be view_admin, got: {entry['permission']}"
    assert entry["mutates"] is False, "read_docs should not be a mutating tool"
    assert entry["domain"] == "docs", f"Domain should be 'docs', got: {entry['domain']}"


# ---------------------------------------------------------------------------
# Security hardening
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_index_link_rejects_traversal(opts):
    """Links extracted from index content with .. should be filtered out."""
    from mojo.apps.assistant.services.tools.docs import _find_topic_in_index

    index = '| [evil](../../../etc/passwd.md) | Secret stuff with traversal |'
    matches = _find_topic_in_index(index, "traversal")
    assert len(matches) == 0, f"Traversal links should be filtered: {matches}"


@th.django_unit_test()
def test_index_link_rejects_protocol_relative(opts):
    """Links starting with // should be filtered out."""
    from mojo.apps.assistant.services.tools.docs import _find_topic_in_index

    index = '| [evil](//attacker.com/payload.md) | Evil protocol-relative link |'
    matches = _find_topic_in_index(index, "evil")
    assert len(matches) == 0, f"Protocol-relative links should be filtered: {matches}"


@th.django_unit_test()
def test_index_link_rejects_absolute(opts):
    """Links starting with / should be filtered out."""
    from mojo.apps.assistant.services.tools.docs import _find_topic_in_index

    index = '| [evil](/etc/passwd.md) | Absolute path link |'
    matches = _find_topic_in_index(index, "evil")
    assert len(matches) == 0, f"Absolute links should be filtered: {matches}"


@th.django_unit_test()
def test_404_error_no_url_leak(opts):
    """404 error should not expose the full base URL."""
    result = _read_docs({"path": "django_developer/nonexistent_page.md"}, opts.admin)
    assert "error" in result, "Should return error"
    assert "raw.githubusercontent" not in result["error"], \
        f"Error should not leak base URL: {result['error']}"


@th.django_unit_test()
def test_validate_base_url(opts):
    from mojo.apps.assistant.services.tools.docs import _validate_base_url
    assert _validate_base_url("https://raw.githubusercontent.com/foo/bar/") is True, \
        "Public https URL should be valid"
    assert _validate_base_url("http://raw.githubusercontent.com/foo/bar/") is False, \
        "http (not https) should be rejected"
    assert _validate_base_url("https://127.0.0.1/docs/") is False, \
        "Localhost should be rejected"
