"""Tests for the export_data assistant tool."""
import contextlib
import csv
import io

from testit import helpers as th

SKILL_SECRET = "EXPTEST-SECRET-MARKER"


@contextlib.contextmanager
def _skill_rest_meta(graphs=None, **attrs):
    """Temporarily ADD graphs / RestMeta attributes to assistant.Skill.

    Additive only: `default` and `detail` are left exactly as shipped, so the
    only code that can see a difference is the assistant's own graph selection,
    which runs in this package's thread. The handler is called in-process; the
    change never reaches the test server.
    """
    from mojo.apps.assistant.models import Skill

    original = Skill.RestMeta
    merged = dict(original.GRAPHS)
    merged.update(graphs or {})
    attrs["GRAPHS"] = merged
    Skill.RestMeta = type("RestMeta", (original,), attrs)
    try:
        yield Skill
    finally:
        Skill.RestMeta = original


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

    # Skills carrying a value the `default` graph does not expose.
    from mojo.apps.assistant.models import Skill
    Skill.objects.filter(name__startswith="exptest_skill_").delete()
    File.objects.filter(filename__startswith="export_assistant_Skill_").delete()
    for i in range(3):
        Skill.objects.create(
            user=opts.admin, tier="user", name=f"exptest_skill_{i}",
            description=f"export graph test {i}",
            steps=[{"note": SKILL_SECRET}],
            metadata={"marker": SKILL_SECRET},
        )


def _export(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_export_data
    return _tool_export_data(params, user)


def _export_skills(opts, **extra):
    params = {
        "app_name": "assistant", "model_name": "Skill",
        "filters": {"name__startswith": "exptest_skill_"},
        "ordering": "name",
    }
    params.update(extra)
    return _export(params, opts.admin)


def _skill_export_files(opts):
    from mojo.apps.fileman.models import File
    return File.objects.filter(filename__startswith="export_assistant_Skill_", user=opts.admin)


def _file_text(f):
    """A stored export's text, read back through its own file backend."""
    with f.file_manager.backend.open(f.storage_file_path) as fh:
        return fh.read().decode("utf-8")


def _read_export(opts):
    """Text and parsed rows of the newest Skill export."""
    f = _skill_export_files(opts).order_by("-pk").first()
    assert f is not None, "the export should have created a File row"
    text = _file_text(f)
    return text, list(csv.reader(io.StringIO(text)))


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


# ---------------------------------------------------------------------------
# Serialization graph — the ceiling for every exported column
# ---------------------------------------------------------------------------

@th.tier("bug")
@th.django_unit_test()
def test_export_refuses_caller_graph(opts):
    """A `graph` key is refused before any File row exists."""
    before = _skill_export_files(opts).count()
    for name in ("default", "detail", None):
        result = _export_skills(opts, graph=name)
        assert "error" in result, f"export_data must refuse graph={name!r}, got: {result}"
        assert "'graph' parameter is not supported" in result["error"], \
            f"Error should name the parameter: {result['error']}"
    assert _skill_export_files(opts).count() == before, "A refused export must not create a File row"


@th.tier("bug")
@th.django_unit_test()
def test_export_default_graph_columns_only(opts):
    """With no `fields`, the columns are exactly the selected graph's public keys."""
    result = _export_skills(opts)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["row_count"] == 3, f"Expected 3 rows, got: {result['row_count']}"
    text, rows = _read_export(opts)
    assert rows[0] == ["Id", "Tier", "Name", "Description", "Auto Execute", "Is Active",
                       "Created", "Modified", "User"], f"Unexpected header: {rows[0]}"
    assert len(rows) == 4, f"Expected a header and 3 rows, got {len(rows)} lines"
    assert SKILL_SECRET not in text, "A value outside the default graph reached the export"
    assert [r[2] for r in rows[1:]] == ["exptest_skill_0", "exptest_skill_1", "exptest_skill_2"], \
        f"Rows should hold the graph-serialized names in order, got: {[r[2] for r in rows[1:]]}"


@th.tier("bug")
@th.django_unit_test()
def test_export_fields_only_narrow(opts):
    """`fields` narrows and reorders the graph's columns."""
    result = _export_skills(opts, fields=["name", "id"])
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    text, rows = _read_export(opts)
    assert rows[0] == ["Name", "Id"], f"Header should be the two chosen columns in order, got: {rows[0]}"
    assert all(len(r) == 2 for r in rows), f"Every row should have two cells, got: {rows}"
    assert rows[1][0] == "exptest_skill_0", f"First cell should be the name, got: {rows[1]}"


@th.tier("bug")
@th.django_unit_test()
def test_export_fields_cannot_widen(opts):
    """Outside names, duplicates, an empty list and junk are refused with no File row."""
    before = _skill_export_files(opts).count()
    cases = {
        "outside the graph (model field)": ["name", "metadata"],
        "outside the graph (detail-only)": ["steps"],
        "relation traversal": ["user.email"],
        "duplicate": ["name", "id", "name"],
        "empty list": [],
        "not a list": "name",
        "not strings": [["name"]],
    }
    for label, fields in cases.items():
        result = _export_skills(opts, fields=fields)
        assert "error" in result, f"fields {label} must be refused, got: {result}"
        assert "url" not in result, f"fields {label} must not produce a file"
    assert _skill_export_files(opts).count() == before, "A refused export must not create a File row"


@th.tier("bug")
@th.django_unit_test()
def test_export_header_reflects_graph_serialized_rows(opts):
    """An `ai` graph with an exclusion, an aliased extra and a nested graph drives the CSV."""
    ai = {
        "fields": ["id", "name", "metadata", "description"],
        "exclude": ["metadata"],
        "extra": [("get_model_string", "model_ref")],
        "graphs": {"user": "basic"},
    }
    with _skill_rest_meta(graphs={"ai": ai}):
        result = _export_skills(opts)
        narrowed = _export_skills(opts, fields=["model_ref", "name"])
        refused = _export_skills(opts, fields=["metadata"])
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert "error" not in narrowed, f"Narrowing to an alias should succeed: {narrowed.get('error')}"
    assert "error" in refused, f"An excluded field must not be selectable, got: {refused}"

    files = list(_skill_export_files(opts).order_by("-pk")[:2])
    texts = [_file_text(f) for f in reversed(files)]
    full = list(csv.reader(io.StringIO(texts[0])))
    assert full[0] == ["Id", "Name", "Description", "Model Ref", "User"], \
        f"Header should be the ai graph's public keys only, got: {full[0]}"
    assert full[1][3] == "assistant.Skill", f"The aliased extra should fill its column, got: {full[1]}"
    assert str(opts.admin.pk) in full[1][4] and "{" in full[1][4], \
        f"The nested graph should occupy one column, got: {full[1][4]!r}"
    assert SKILL_SECRET not in texts[0], "The excluded field's value reached the export"
    slim = list(csv.reader(io.StringIO(texts[1])))
    assert slim[0] == ["Model Ref", "Name"], f"Narrowed header should use the alias, got: {slim[0]}"


@th.tier("bug")
@th.django_unit_test()
def test_export_zero_rows_keeps_graph_header(opts):
    """An empty result still writes the selected columns, and only those."""
    result = _export(
        {"app_name": "assistant", "model_name": "Skill",
         "filters": {"name": "exptest_skill_absent"}, "fields": ["name", "id"]}, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    assert result["row_count"] == 0, f"Expected 0 rows, got: {result['row_count']}"
    text, rows = _read_export(opts)
    assert rows == [["Name", "Id"]], f"Expected a header-only file, got: {rows}"
