"""Split out of tests/test_fileman/6_test_shortlink_urls.py (maestro #1839).

test_toggle_fm_true_wins patches the shared settings singleton
(mojo.helpers.settings.settings.get) around an IN-PROCESS call, which is
process-global and unsafe under the parallel default tier — so it runs here,
in the opt-in serial tier, with its own minimal fixtures.
"""
import os
import tempfile
import shutil as _shutil

from testit import helpers as th
from testit.helpers import assert_true


TEST_USER = "fileman_sl_user"


TEST_PWORD = "fileman##mojo99"


def _write_file(tmpdir, storage_file_path, data):
    full_path = os.path.join(tmpdir, storage_file_path.lstrip('/'))
    os.makedirs(os.path.dirname(full_path) or tmpdir, exist_ok=True)
    with open(full_path, 'wb') as fh:
        fh.write(data)


@th.django_unit_setup()
def setup_shortlink_urls_extended(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File
    from mojo.apps.shortlink.models import ShortLink
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    u1 = User.objects.filter(username=TEST_USER).last()
    if u1 is None:
        u1 = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        u1.save()
    u1.is_email_verified = True
    u1.save_password(TEST_PWORD)
    u1.add_permission(["view_fileman", "manage_files"])
    u1.save()
    opts.user = u1

    tmpdir = tempfile.mkdtemp(prefix="mojo_sl_test_")
    opts.tmpdir = tmpdir

    fm = FileManager.objects.filter(name="test_sl_fm", user=u1).first()
    if fm is None:
        fm = FileManager(
            name="test_sl_fm",
            backend_type="file",
            backend_url="file://",
            user=u1,
            is_active=True,
            is_default=False,
            is_public=False,  # private so resolver returns a *new* signed URL
        )
        fm.save()
    fm.backend_url = "file://"
    fm.is_public = False
    fm.is_active = True
    fm.save()
    fm.set_setting("base_path", tmpdir)
    # Default: do not force per-manager toggle. The test twiddles as needed.
    fm.set_setting("use_shortlinks", None)
    fm.set_setting("shortlink_track_clicks", None)
    fm.set_setting("shortlink_expire_days", None)
    fm.save(update_fields=["mojo_secrets", "modified"])
    opts.fm_id = fm.pk

    File.objects.filter(user=u1).delete()
    ShortLink.objects.filter(source__in=["fileman", "fileman-share"]).delete()


def _mk_file(opts, filename="tier1.txt", file_manager_id=None, content=b"hi"):
    from mojo.apps.fileman.models import FileManager, File
    fm = FileManager.objects.get(pk=file_manager_id or opts.fm_id)
    f = File(filename=filename, content_type="text/plain", category="text",
             file_size=len(content), file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()
    _write_file(opts.tmpdir, f.storage_file_path, content)
    f.mark_as_completed(commit=True)
    f.refresh_from_db()
    return f


@th.django_unit_test("Toggle: per-FileManager True overrides global False")
def test_toggle_fm_true_wins(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.shortlink.models import ShortLink
    from unittest import mock

    fm = FileManager.objects.get(pk=opts.fm_id)
    fm.set_setting("use_shortlinks", True)  # per-manager ON
    fm.save(update_fields=["mojo_secrets", "modified"])

    # Simulate global OFF via settings.get returning False. Patching the helper
    # module's settings import is simplest (and works because generate_download_url
    # is invoked in-process — no cross-process server isolation).
    from mojo.apps.fileman.models import file as file_module
    with mock.patch.object(file_module, "_shortlink_installed", return_value=True):
        with mock.patch("mojo.helpers.settings.settings.get") as mget:
            def _fake_get(k, default=None, **kwargs):
                if k == "FILEMAN_USE_SHORTLINKS":
                    return False
                return default
            mget.side_effect = _fake_get
            f = _mk_file(opts, "tier1_fm_wins.txt")
            url = f.generate_download_url()
    assert_true("/s/" in (url or ""),
                f"per-manager True should override global False; got {url!r}")
    assert_true(
        ShortLink.objects.filter(file=f, source="fileman").exists(),
        "shortlink row should be created when per-manager wins",
    )

    fm.set_setting("use_shortlinks", None)
    fm.save(update_fields=["mojo_secrets", "modified"])


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_shortlink_urls_extended(opts):
    from mojo.apps.fileman.models import FileManager, File
    from mojo.apps.shortlink.models import ShortLink

    File.objects.filter(user=opts.user).delete()
    FileManager.objects.filter(pk=opts.fm_id).delete()
    ShortLink.objects.filter(source__in=["fileman", "fileman-share"]).delete()

    if os.path.exists(opts.tmpdir):
        _shutil.rmtree(opts.tmpdir, ignore_errors=True)

