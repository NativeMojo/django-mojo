"""
Tests for the assistant file domain tools — query_files, get_file, analyze_image.
"""
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL_FILES = 'assistant-files-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.fileman")
def setup_file_tools(opts):
    from mojo.apps.account.models import User

    # Clean up prior test data
    User.objects.filter(email=TEST_EMAIL_FILES).delete()

    opts.file_user = User.objects.create_user(
        username=TEST_EMAIL_FILES, email=TEST_EMAIL_FILES, password=TEST_PASSWORD,
    )
    opts.file_user.is_email_verified = True
    opts.file_user.save()
    for perm in ["view_admin", "view_fileman", "manage_files"]:
        opts.file_user.add_permission(perm)


@th.django_unit_test()
def test_file_tools_registered(opts):
    """File tools should be registered in the assistant tool registry."""
    from mojo.apps.assistant import get_registry
    registry = get_registry()

    for name in ["query_files", "get_file", "analyze_image"]:
        assert_true(name in registry, f"Expected tool '{name}' in registry")

    # Verify domain
    assert_eq(registry["query_files"]["domain"], "files",
              "query_files should be in 'files' domain")
    assert_eq(registry["get_file"]["domain"], "files",
              "get_file should be in 'files' domain")
    assert_eq(registry["analyze_image"]["domain"], "files",
              "analyze_image should be in 'files' domain")


@th.django_unit_test()
def test_file_tools_permission(opts):
    """File tools should require view_fileman permission."""
    from mojo.apps.assistant import get_registry
    registry = get_registry()

    for name in ["query_files", "get_file", "analyze_image"]:
        assert_eq(registry[name]["permission"], "view_fileman",
                  f"{name} should require view_fileman permission")


@th.django_unit_test()
def test_file_tools_visible_to_permitted_user(opts):
    """User with view_fileman should see file tools."""
    from mojo.apps.assistant import get_tools_for_user
    tools = get_tools_for_user(opts.file_user)
    tool_names = [t["name"] for t in tools]

    assert_true("query_files" in tool_names,
                "File user should see query_files")
    assert_true("get_file" in tool_names,
                "File user should see get_file")
    assert_true("analyze_image" in tool_names,
                "File user should see analyze_image")


@th.django_unit_test()
def test_query_files_returns_bounded_list(opts):
    """query_files should return a bounded list."""
    from mojo.apps.assistant.services.tools.files import _tool_query_files

    result = _tool_query_files({"limit": 5}, opts.file_user)
    assert_true(isinstance(result, list), f"Expected list, got {type(result).__name__}")
    assert_true(len(result) <= 5, f"Expected at most 5 results, got {len(result)}")


@th.django_unit_test()
def test_query_files_filters_by_category(opts):
    """query_files with category filter should not raise."""
    from mojo.apps.assistant.services.tools.files import _tool_query_files

    result = _tool_query_files({"category": "image", "limit": 3}, opts.file_user)
    assert_true(isinstance(result, list), f"Expected list, got {type(result).__name__}")
    for item in result:
        assert_eq(item["category"], "image",
                  f"Expected category 'image', got '{item['category']}'")


@th.django_unit_test()
def test_get_file_not_found(opts):
    """get_file with invalid ID should return error."""
    from mojo.apps.assistant.services.tools.files import _tool_get_file

    result = _tool_get_file({"file_id": 999999}, opts.file_user)
    assert_true("error" in result, f"Expected error for missing file, got {result}")


@th.django_unit_test()
def test_analyze_image_not_found(opts):
    """analyze_image with invalid ID should return error."""
    from mojo.apps.assistant.services.tools.files import _tool_analyze_image

    result = _tool_analyze_image({"file_id": 999999}, opts.file_user)
    assert_true("error" in result, f"Expected error for missing file, got {result}")


@th.django_unit_test()
def test_analyze_image_rejects_non_image(opts):
    """analyze_image should reject files that are not images."""
    from mojo.apps.assistant.services.tools.files import _tool_analyze_image
    from mojo.apps.fileman.models import File

    # Find a non-image file, or mock one
    non_image = File.objects.filter(category="document", upload_status="completed").first()
    if non_image:
        result = _tool_analyze_image({"file_id": non_image.pk}, opts.file_user)
        assert_true("error" in result, "Should reject non-image files")
        assert_true("not an image" in result["error"],
                    f"Error should mention 'not an image', got: {result['error']}")
    else:
        # Create a mock file record to test the category check
        mock_file = mock.MagicMock()
        mock_file.category = "document"
        mock_file.upload_status = "completed"
        mock_file.pk = -1
        mock_file.file_size = 100

        with mock.patch(
            "mojo.apps.fileman.models.File.objects.select_related"
        ) as mock_sr:
            mock_sr.return_value.get.return_value = mock_file
            result = _tool_analyze_image({"file_id": -1}, opts.file_user)
            assert_true("error" in result, "Should reject non-image files")
            assert_true("not an image" in result["error"],
                        f"Error should mention 'not an image', got: {result['error']}")


@th.django_unit_test()
def test_analyze_image_rejects_oversized(opts):
    """analyze_image should reject images over the size limit."""
    from mojo.apps.assistant.services.tools.files import _tool_analyze_image, MAX_IMAGE_BYTES

    mock_file = mock.MagicMock()
    mock_file.category = "image"
    mock_file.upload_status = "completed"
    mock_file.pk = -1
    mock_file.file_size = MAX_IMAGE_BYTES + 1

    with mock.patch(
        "mojo.apps.fileman.models.File.objects.select_related"
    ) as mock_sr:
        mock_sr.return_value.get.return_value = mock_file
        result = _tool_analyze_image({"file_id": -1}, opts.file_user)
        assert_true("error" in result, "Should reject oversized images")
        assert_true("too large" in result["error"].lower(),
                    f"Error should mention size, got: {result['error']}")


@th.django_unit_test()
def test_analyze_image_calls_llm_with_vision(opts):
    """analyze_image should call llm.call with image content block."""
    from mojo.apps.assistant.services.tools.files import _tool_analyze_image

    mock_file = mock.MagicMock()
    mock_file.category = "image"
    mock_file.upload_status = "completed"
    mock_file.pk = 42
    mock_file.filename = "test.png"
    mock_file.content_type = "image/png"
    mock_file.file_size = 1000
    mock_file.storage_file_path = "test/test.png"

    # Mock the file handle returned by backend.open
    fake_bytes = b"\x89PNG fake image data"
    mock_fh = mock.MagicMock()
    mock_fh.read.return_value = fake_bytes
    mock_file.file_manager.backend.open.return_value = mock_fh

    # Mock llm.call
    fake_response = {
        "content": [{"type": "text", "text": "This is a test image showing a logo."}]
    }

    with mock.patch(
        "mojo.apps.fileman.models.File.objects.select_related"
    ) as mock_sr:
        mock_sr.return_value.get.return_value = mock_file
        with mock.patch("mojo.helpers.llm.call", return_value=fake_response) as mock_call:
            result = _tool_analyze_image(
                {"file_id": 42, "prompt": "What is in this image?"},
                opts.file_user,
            )

            assert_true("analysis" in result, f"Expected 'analysis' key, got {result}")
            assert_eq(result["file_id"], 42, "Should return the file ID")
            assert_eq(result["filename"], "test.png", "Should return the filename")
            assert_true("logo" in result["analysis"],
                        f"Analysis should contain mocked response, got: {result['analysis']}")

            # Verify llm.call was called with image content block
            assert_true(mock_call.called, "llm.call should have been called")
            call_args = mock_call.call_args
            messages = call_args[0][0]
            assert_eq(len(messages), 1, "Should send 1 message")
            content = messages[0]["content"]
            assert_eq(len(content), 2, "Message should have 2 content blocks")
            assert_eq(content[0]["type"], "image",
                      "First content block should be image type")
            assert_eq(content[0]["source"]["type"], "base64",
                      "Image source should be base64")
            assert_eq(content[0]["source"]["media_type"], "image/png",
                      "Media type should match file content_type")
            assert_eq(content[1]["type"], "text",
                      "Second content block should be text")
            assert_true("What is in this image" in content[1]["text"],
                        "Text block should contain the prompt")
