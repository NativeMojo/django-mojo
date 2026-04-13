"""Tests for the export_data assistant tool."""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.fileman")
def setup_export(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event
    from mojo.apps.fileman.models import FileManager, File

    # Clean up test users
    User.objects.filter(email__in=["exptest_admin@test.com", "exptest_nopriv@test.com"]).delete()

    opts.admin = User.objects.create_user(
        username="exptest_admin@test.com", email="exptest_admin@test.com", password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("view_security")

    opts.nopriv = User.objects.create_user(
        username="exptest_nopriv@test.com", email="exptest_nopriv@test.com", password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    # Ensure a FileManager exists for the admin user
    # Clean up any previous test file managers
    FileManager.objects.filter(name="exptest_fm").delete()
    opts.fm = FileManager.objects.create(
        name="exptest_fm",
        backend_type="file",
        backend_url="filesystem:///tmp/exptest_files",
        is_default=True,
        is_active=True,
        user=opts.admin,
    )

    # Clean up test files from previous runs
    File.objects.filter(filename__startswith="export_incident_Event_").delete()

    # Create test events
    Event.objects.filter(title__startswith="exptest_").delete()
    for i in range(5):
        Event.objects.create(
            title=f"exptest_event_{i}",
            details=f"Export test event {i}",
            category="test",
            level=i + 1,
            scope="global",
        )


def _export(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_export_data
    return _tool_export_data(params, user)


# ---------------------------------------------------------------------------
# Basic export
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_export_creates_file(opts):
    result = _export({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "exptest_"},
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "url" in result, "Result should have url"
    assert "filename" in result, "Result should have filename"
    assert result["filename"].startswith("export_incident_Event_"), \
        f"Filename should start with export_incident_Event_, got: {result['filename']}"
    assert result["filename"].endswith(".csv"), \
        f"Filename should end with .csv, got: {result['filename']}"
    assert result["row_count"] == 5, f"Expected 5 rows, got: {result['row_count']}"
    assert result["size"] > 0, f"File size should be > 0, got: {result['size']}"
    assert result["model"] == "incident.Event", f"Model: {result.get('model')}"


@th.django_unit_test()
def test_export_file_has_metadata(opts):
    from mojo.apps.fileman.models import File

    result = _export({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "exptest_"},
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"

    # Find the file by filename pattern
    f = File.objects.filter(
        filename__startswith="export_incident_Event_",
        user=opts.admin,
    ).order_by("-created").first()
    assert f is not None, "File record should exist"
    assert f.metadata.get("source") == "assistant_export", \
        f"Expected source=assistant_export, got: {f.metadata.get('source')}"
    assert f.metadata.get("model") == "incident.Event", \
        f"Expected model=incident.Event, got: {f.metadata.get('model')}"
    assert "expires_at" in f.metadata, "File should have expires_at in metadata"
    assert f.metadata.get("row_count") == 5, \
        f"Expected row_count=5, got: {f.metadata.get('row_count')}"


@th.django_unit_test()
def test_export_returns_url_not_content(opts):
    result = _export({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "exptest_"},
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "url" in result, "Should have url"
    assert "content" not in result, "Should NOT have inline content"
    assert "expires_in" in result, "Should have expires_in"


@th.django_unit_test()
def test_export_has_expires_in(opts):
    result = _export({
        "app_name": "incident", "model_name": "Event",
        "filters": {"title__startswith": "exptest_"},
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "days" in result.get("expires_in", ""), \
        f"expires_in should contain 'days', got: {result.get('expires_in')}"


# ---------------------------------------------------------------------------
# Validation and permissions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_export_permission_denied(opts):
    result = _export({
        "app_name": "incident", "model_name": "Event",
    }, opts.nopriv)
    assert "error" in result, "Should be denied without view_security"
    assert "Permission denied" in result["error"], f"Error: {result['error']}"


@th.django_unit_test()
def test_export_bad_model(opts):
    result = _export({
        "app_name": "fake", "model_name": "FakeModel",
    }, opts.admin)
    assert "error" in result, "Should error for nonexistent model"


@th.django_unit_test()
def test_export_limit_enforcement(opts):
    from mojo.apps.assistant.services.tools.models import MAX_EXPORT_LIMIT
    # Just verify the constant is reasonable
    assert MAX_EXPORT_LIMIT == 50000, f"MAX_EXPORT_LIMIT should be 50000, got: {MAX_EXPORT_LIMIT}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_export_data_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "export_data" in registry, "export_data should be registered"
    entry = registry["export_data"]
    assert entry["permission"] == "view_admin", f"Permission: {entry['permission']}"
    assert entry["core"] is True, "Should be core tool"
    assert entry["domain"] == "models", f"Domain: {entry['domain']}"
    assert entry["mutates"] is True, "Should be marked as mutating"
