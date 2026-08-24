"""Regression tests for FileManager CORS allowed-origin resolution.

`fix_cors` resolved allowed origins only from the action payload and global
Django settings — it ignored the manager's own `allowed_origins` field, even
though `check_cors` consults it (`on_action_check_cors` passes
`self.allowed_origins`). A manager configured with `allowed_origins` would
therefore fail `fix_cors` with "No allowed origins provided" while passing
`check_cors`.

When no origin is configured, direct-upload CORS falls back to ``*``. This is
the legacy django-mojo behavior: CORS permits browsers to use an upload
capability, while the presigned URL remains the authorization boundary.

These tests exercise the resolver directly — no S3 connectivity required.
"""

TESTIT_TIER = "bug"
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_true


@th.django_unit_setup()
def setup_fm_cors(opts):
    from mojo.apps.fileman.models import FileManager

    # Tests share a long-lived db — clear leftovers before creating.
    FileManager.objects.filter(name__startswith="fm_cors_").delete()

    # Direct ORM create (bypasses on_rest_pre_save's system-scope guard).
    fm = FileManager(
        name="fm_cors_test",
        backend_type="file",
        backend_url="file://",
        is_active=True,
    )
    fm.save()
    opts.fm_id = fm.pk


def _fm(opts):
    """Fresh FileManager instance per test — each test mutates allowed_origins."""
    from mojo.apps.fileman.models import FileManager
    return FileManager.objects.get(pk=opts.fm_id)


@th.django_unit_test("CORS: manager allowed_origins (list) is resolved by fix_cors path")
def test_resolve_uses_manager_allowed_origins_list(opts):
    fm = _fm(opts)
    fm.set_allowed_origins(["https://app.example.com", "https://admin.example.com"])
    fm.save()

    resolved = fm._resolve_allowed_origins_from_value_or_settings({})
    assert_true(
        "https://app.example.com" in resolved,
        f"manager allowed_origins should be resolved by fix_cors, got {resolved}",
    )
    assert_true(
        "https://admin.example.com" in resolved,
        f"manager allowed_origins should be resolved by fix_cors, got {resolved}",
    )


@th.django_unit_test("CORS: comma-separated manager allowed_origins string is split")
def test_resolve_manager_allowed_origins_string(opts):
    fm = _fm(opts)
    fm.set_allowed_origins("https://a.example.com, https://b.example.com")
    fm.save()

    resolved = fm._resolve_allowed_origins_from_value_or_settings({})
    assert_true(
        "https://a.example.com" in resolved,
        f"comma-separated origins should be split, got {resolved}",
    )
    assert_true(
        "https://b.example.com" in resolved,
        f"comma-separated origins should be split, got {resolved}",
    )


@th.django_unit_test("CORS: explicit action payload origins still resolve")
def test_resolve_payload_origins(opts):
    fm = _fm(opts)
    fm.set_allowed_origins([])  # clear manager origins — payload must stand alone
    fm.save()

    resolved = fm._resolve_allowed_origins_from_value_or_settings(
        {"origins": "https://payload.example.com"}
    )
    assert_true(
        "https://payload.example.com" in resolved,
        f"action payload origins should still resolve, got {resolved}",
    )


@th.django_unit_test("CORS: missing origins fall back to wildcard")
def test_resolve_defaults_to_wildcard(opts):
    fm = _fm(opts)
    fm.set_allowed_origins([])
    fm.save()

    def configured_url(name):
        return "https://api.example.com" if name == "BASE_URL" else None

    with mock.patch("mojo.apps.fileman.models.manager.settings.get", side_effect=configured_url):
        resolved = fm._resolve_allowed_origins_from_value_or_settings({})

    assert_true(
        resolved == ["*"],
        f"BASE_URL must not narrow the wildcard direct-upload default, got {resolved}",
    )


@th.django_unit_setup()
def cleanup_fm_cors(opts):
    from mojo.apps.fileman.models import FileManager
    FileManager.objects.filter(name__startswith="fm_cors_").delete()
