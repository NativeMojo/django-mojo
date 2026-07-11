"""Regression tests for ITEM-033 — the uploading user (owner) can complete and
FK-attach their own fileman File without manage_files/files perms.

`POST /api/fileman/upload/initiate` is auth-only (`@md.requires_auth()`), so any
group member can start an upload and the File is stamped `user=request.user`.
Before the fix, `File.RestMeta.{VIEW,SAVE}_PERMS` omitted the `"owner"` token, so:

  * the documented completion step (`POST /api/fileman/file/<id>`
    `{"action": "mark_as_completed"}`) 403'd with `group_member_permission_denied`
    — the uploader could never finalize their own upload; and
  * any FK to their own File (e.g. `User.avatar`, `note.media`) was **silently
    dropped** by the generic FK view-gate, since the member held none of
    `File.VIEW_PERMS`.

Adding `"owner"` to `File.RestMeta.VIEW_PERMS`/`SAVE_PERMS`/`DELETE_PERMS` closes
both gaps (evaluator, list-filter, and FK-gate already honor the token) while
keeping non-owners fail-closed. These tests exercise the full path over the REST
client with two permissionless members and a local `file://` backend.
"""
import os
import tempfile
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

OWNER_USER = "fm_owner_up_owner"
OTHER_USER = "fm_owner_up_other"
PWORD = "fmowner##mojo99"


def _write_dummy_file(tmpdir, storage_file_path):
    """Write real bytes so backend.exists() returns True for mark_as_completed."""
    full_path = os.path.join(tmpdir, storage_file_path.lstrip('/'))
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, 'w') as fh:
        fh.write("test")


def _initiate(opts, username):
    """Log in as `username` (a plain member) and initiate an upload against the
    test manager. Returns a freshly-fetched File row owned by that user."""
    from mojo.apps.fileman.models import File

    opts.client.login(username, PWORD)
    assert_true(opts.client.is_authenticated, f"{username} login should succeed")
    resp = opts.client.post("/api/fileman/upload/initiate", {
        "filename": "owner_upload.txt",
        "content_type": "text/plain",
        "file_size": 4,
        "file_manager": opts.fm_id,
    })
    assert_eq(resp.status_code, 200,
              f"initiate should be 200 for a plain member, got "
              f"{resp.status_code}: {resp.response.data}")
    return File.objects.get(pk=resp.response.data.id)


@th.django_unit_setup()
def setup_owner_upload(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Tests share a long-lived db — clear leftovers before creating.
    User.objects.filter(username__in=[OWNER_USER, OTHER_USER]).delete()
    FileManager.objects.filter(name="fm_owner_up_fm").delete()

    def _member(username):
        u = User(username=username, email=f"{username}@example.com")
        u.save()
        u.is_email_verified = True
        u.save_password(PWORD)
        u.save()  # deliberately NO fileman perms — a plain group member
        return u

    opts.owner = _member(OWNER_USER)
    opts.other = _member(OTHER_USER)

    # temp dir for a local-backend FileManager (no user/group scope; both
    # members reference it explicitly by id on initiate, so resolution needs
    # no perms and File.group ends up None).
    tmpdir = tempfile.mkdtemp(prefix="mojo_owner_up_")
    opts.tmpdir = tmpdir

    fm = FileManager(
        name="fm_owner_up_fm",
        backend_type="file",
        backend_url="file://",
        is_active=True,
    )
    fm.save()
    fm.set_setting("base_path", tmpdir)
    fm.save(update_fields=["mojo_secrets", "modified"])
    opts.fm_id = fm.pk

    File.objects.filter(user__in=[opts.owner, opts.other]).delete()


# ---------------------------------------------------------------------------
# Completion — owner may, non-owner may not.
# ---------------------------------------------------------------------------

@th.django_unit_test("fileman owner: uploader completes own upload without manage_files (regression)")
def test_owner_completes_own_upload(opts):
    from mojo.apps.fileman.models import File

    f = _initiate(opts, OWNER_USER)
    _write_dummy_file(opts.tmpdir, f.storage_file_path)

    # Same session (owner) posts the documented completion action.
    resp = opts.client.post(f"/api/fileman/file/{f.id}", {"action": "mark_as_completed"})
    assert_eq(resp.status_code, 200,
              f"owner should complete own upload, got "
              f"{resp.status_code}: {resp.response.data}")

    f.refresh_from_db()
    assert_eq(f.upload_status, File.COMPLETED,
              f"file should be COMPLETED after owner mark_as_completed, "
              f"got {f.upload_status}")


@th.django_unit_test("fileman owner: non-owner cannot complete another member's upload")
def test_non_owner_cannot_complete(opts):
    from mojo.apps.fileman.models import File

    f = _initiate(opts, OWNER_USER)  # owned by OWNER, left UPLOADING
    assert_eq(f.upload_status, File.UPLOADING,
              f"freshly initiated file should be UPLOADING, got {f.upload_status}")

    # A different permissionless member (not the owner) attempts completion.
    opts.client.login(OTHER_USER, PWORD)
    resp = opts.client.post(f"/api/fileman/file/{f.id}", {"action": "mark_as_completed"})
    assert_eq(resp.status_code, 403,
              f"non-owner without manage_files must be denied, got {resp.status_code}")

    f.refresh_from_db()
    assert_eq(f.upload_status, File.UPLOADING,
              f"denied completion must not change status, got {f.upload_status}")


# ---------------------------------------------------------------------------
# FK attach — owner's own File attaches; a foreign File is silently dropped.
# ---------------------------------------------------------------------------

@th.django_unit_test("fileman owner: uploader can FK-attach own File as avatar (regression)")
def test_owner_can_fk_attach_own_file(opts):
    from mojo.apps.account.models import User

    f = _initiate(opts, OWNER_USER)  # owned by OWNER; session is OWNER

    # Owner saves their own user record with avatar -> their own File.
    resp = opts.client.post("/api/user/me", {"avatar": f.id})
    assert_eq(resp.status_code, 200,
              f"owner self-save should be 200, got "
              f"{resp.status_code}: {resp.response.data}")

    owner = User.objects.get(pk=opts.owner.pk)
    assert_eq(owner.avatar_id, f.id,
              f"owner's own File should attach as avatar, got avatar_id={owner.avatar_id}")


@th.django_unit_test("fileman owner: foreign File FK-attach is silently dropped for non-owner")
def test_foreign_fk_attach_dropped(opts):
    from mojo.apps.account.models import User

    f = _initiate(opts, OWNER_USER)  # owned by OWNER

    # A different member saves THEIR OWN user record (owner/self -> allowed),
    # but points avatar at OWNER's File — the FK must be dropped, save still 200.
    opts.client.login(OTHER_USER, PWORD)
    resp = opts.client.post("/api/user/me", {"avatar": f.id})
    assert_eq(resp.status_code, 200,
              f"non-owner's own self-save should still be 200, got "
              f"{resp.status_code}: {resp.response.data}")

    other = User.objects.get(pk=opts.other.pk)
    assert_true(other.avatar_id is None,
                f"foreign File must not attach as avatar (silent drop), "
                f"got avatar_id={other.avatar_id}")


# ---------------------------------------------------------------------------
# List — a permissionless owner sees only their own files.
# ---------------------------------------------------------------------------

@th.django_unit_test("fileman owner: permissionless owner lists only own files")
def test_owner_list_scoped_to_owner(opts):
    owner_file = _initiate(opts, OWNER_USER)   # owned by OWNER
    other_file = _initiate(opts, OTHER_USER)   # owned by OTHER; session is OTHER

    # As OTHER (no perms), the list auto-filters to OTHER's own rows.
    resp = opts.client.get("/api/fileman/file")
    assert_eq(resp.status_code, 200, f"list should be 200, got {resp.status_code}")

    ids = [row.id for row in resp.response.data]
    assert_true(other_file.id in ids,
                f"owner should see own file {other_file.id} in list, got {ids}")
    assert_true(owner_file.id not in ids,
                f"owner must not see another member's file {owner_file.id}, got {ids}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_owner_upload(opts):
    import shutil
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File

    File.objects.filter(user__in=[opts.owner, opts.other]).delete()
    FileManager.objects.filter(name="fm_owner_up_fm").delete()
    User.objects.filter(username__in=[OWNER_USER, OTHER_USER]).delete()

    if os.path.exists(opts.tmpdir):
        shutil.rmtree(opts.tmpdir, ignore_errors=True)
