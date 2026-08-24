"""Regression tests for ITEM-033 — the uploading user (owner) can complete and
FK-attach their own fileman File without manage_files/files perms.

`POST /api/fileman/upload/initiate` requires an explicitly authorized manager;
these users share an active group-scoped manager and the File is stamped
`user=request.user`.
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

TESTIT_TIER = "bug"
import os
import tempfile
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

OWNER_USER = "fm_owner_up_owner"
OTHER_USER = "fm_owner_up_other"
ADMIN_USER = "fm_owner_up_admin"
PWORD = "fmowner##mojo99"
INLINE_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
        "group": opts.group_id,
    })
    assert_eq(resp.status_code, 200,
              f"initiate should be 200 for a plain member, got "
              f"{resp.status_code}: {resp.response.data}")
    return File.objects.get(pk=resp.response.data.id)


def _make_avatar(opts, owner, **overrides):
    from mojo.apps.fileman.models import File

    values = {
        "filename": f"avatar-{owner.id}.png",
        "content_type": "image/png",
        "category": "image",
        "file_size": 68,
        "upload_status": File.COMPLETED,
        "is_active": True,
        "file_manager_id": opts.fm_id,
        "user": owner,
        "group": None,
    }
    values.update(overrides)
    return File.objects.create(**values)


@th.django_unit_setup()
def setup_owner_upload(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import FileManager, File
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Tests share a long-lived db — clear leftovers before creating.
    GroupMember.objects.filter(group__name="fm_owner_up_group").delete()
    Group.objects.filter(name="fm_owner_up_group").delete()
    User.objects.filter(username__in=[OWNER_USER, OTHER_USER, ADMIN_USER]).delete()
    FileManager.objects.filter(name__startswith="fm_owner_up_").delete()

    def _member(username):
        u = User(username=username, email=f"{username}@example.com")
        u.save()
        u.is_email_verified = True
        u.save_password(PWORD)
        u.save()  # deliberately NO fileman perms — a plain group member
        return u

    opts.owner = _member(OWNER_USER)
    opts.other = _member(OTHER_USER)
    opts.admin = _member(ADMIN_USER)
    opts.admin.add_permission("users")
    opts.admin.add_permission("manage_files")
    opts.admin.save()
    group = Group(name="fm_owner_up_group")
    group.save()
    group.add_member(opts.owner)
    group.add_member(opts.other)
    opts.group_id = group.id

    # temp dir for a local-backend FileManager shared by both active members.
    tmpdir = tempfile.mkdtemp(prefix="mojo_owner_up_")
    opts.tmpdir = tmpdir

    system_fm = FileManager(
        name="fm_owner_up_system_fm",
        backend_type="file",
        backend_url="file://",
        is_active=True,
        is_default=True,
    )
    system_fm.save()
    system_fm.set_setting("base_path", tmpdir)
    system_fm.save(update_fields=["mojo_secrets", "modified"])

    fm = FileManager(
        name="fm_owner_up_fm",
        backend_type="file",
        backend_url="file://",
        is_active=True,
        group=group,
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


@th.tier("core")
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

    opts.client.login(OWNER_USER, PWORD)
    f = _make_avatar(opts, opts.owner)

    # Owner saves their own user record with avatar -> their own File.
    resp = opts.client.post("/api/user/me", {"avatar": f.id})
    assert_eq(resp.status_code, 200,
              f"owner self-save should be 200, got "
              f"{resp.status_code}: {resp.response.data}")

    owner = User.objects.get(pk=opts.owner.pk)
    assert_eq(owner.avatar_id, f.id,
              f"owner's own File should attach as avatar, got avatar_id={owner.avatar_id}")
    assert_eq(resp.response.data.avatar.id, f.id,
              "save response must authoritatively serialize the attached avatar")


@th.tier("core")
@th.django_unit_test("fileman relation: foreign File FK-attach fails explicitly")
def test_foreign_fk_attach_dropped(opts):
    from mojo.apps.account.models import User

    f = _make_avatar(opts, opts.owner)

    # A different member saves THEIR OWN user record (owner/self -> allowed),
    # but points avatar at OWNER's File — the attach must fail explicitly.
    opts.client.login(OTHER_USER, PWORD)
    resp = opts.client.post("/api/user/me", {"avatar": f.id})
    assert_eq(resp.status_code, 403,
              f"non-viewable File attach must fail explicitly, got "
              f"{resp.status_code}: {resp.response}")

    other = User.objects.get(pk=opts.other.pk)
    assert_true(other.avatar_id is None,
                f"foreign File must not attach as avatar, "
                f"got avatar_id={other.avatar_id}")


@th.django_unit_test("fileman relation: explicit null clears a nullable File FK")
def test_file_relation_explicit_null_clears(opts):
    from mojo.apps.account.models import User

    f = _initiate(opts, OWNER_USER)
    User.objects.filter(pk=opts.owner.pk).update(avatar=f)

    resp = opts.client.post("/api/user/me", {"avatar": None})
    assert_eq(resp.status_code, 200,
              f"explicit null clear should succeed, got {resp.status_code}: {resp.response}")
    owner = User.objects.get(pk=opts.owner.pk)
    assert_true(owner.avatar_id is None,
                f"explicit null must detach the File relation, got avatar_id={owner.avatar_id}")
    assert_true(resp.response.data.avatar is None,
                "clear response must authoritatively serialize avatar=null")


@th.django_unit_test("fileman relation: File id classification is strict and bounded")
def test_file_relation_strict_value_classification(opts):
    from mojo.apps.account.models import User

    _login_values = [True, False, 0, -1, "123", " 123 ", {}, [], "not base64!"]
    opts.client.login(OWNER_USER, PWORD)
    for value in _login_values:
        User.objects.filter(pk=opts.owner.pk).update(avatar=None)
        resp = opts.client.post("/api/user/me", {"avatar": value})
        assert_eq(resp.status_code, 400,
                  f"ambiguous File relation value {value!r} must return 400: {resp.response}")
        assert_true(User.objects.get(pk=opts.owner.pk).avatar_id is None,
                    f"invalid value {value!r} must leave the relation unchanged")


@th.django_unit_test("fileman relation: missing and non-viewable ids share one response")
def test_file_relation_missing_and_denied_are_uniform(opts):
    from mojo.apps.account.models import User

    hidden = _make_avatar(opts, opts.owner)
    opts.client.login(OTHER_USER, PWORD)
    denied = opts.client.post("/api/user/me", {"avatar": hidden.id})
    missing = opts.client.post("/api/user/me", {"avatar": 2147483647})
    assert_eq(denied.status_code, 403, f"hidden File must be unavailable: {denied.response}")
    assert_eq(missing.status_code, 403, f"missing File must be unavailable: {missing.response}")
    assert_eq(denied.response.error, "File unavailable",
              "hidden File error must be non-oracular")
    assert_eq(missing.response.error, denied.response.error,
              "missing and hidden File errors must be identical")
    assert_true(User.objects.get(pk=opts.other.pk).avatar_id is None,
                "denied relation must remain unchanged")


@th.django_unit_test("fileman avatar: lifecycle, image, scope, and ownership are required")
def test_avatar_candidate_validation(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import File

    opts.client.login(OWNER_USER, PWORD)
    cases = [
        _make_avatar(opts, opts.owner, upload_status=File.UPLOADING),
        _make_avatar(opts, opts.owner, content_type="text/plain", category="document"),
        _make_avatar(opts, opts.owner, is_active=False),
        _make_avatar(opts, opts.owner, group_id=opts.group_id),
    ]
    for candidate in cases:
        User.objects.filter(pk=opts.owner.pk).update(avatar=None)
        resp = opts.client.post("/api/user/me", {"avatar": candidate.id})
        assert_eq(resp.status_code, 400,
                  f"invalid avatar candidate {candidate.id} must return 400: {resp.response}")
        assert_true(User.objects.get(pk=opts.owner.pk).avatar_id is None,
                    f"invalid avatar candidate {candidate.id} must not attach")


@th.django_unit_test("fileman avatar: admin-on-behalf retains uploader ownership")
def test_admin_avatar_on_behalf_ownership(opts):
    from mojo.apps.account.models import User

    admin_file = _make_avatar(opts, opts.admin)
    target_file = _make_avatar(opts, opts.owner)
    opts.client.login(ADMIN_USER, PWORD)

    allowed = opts.client.post(f"/api/user/{opts.owner.id}", {"avatar": admin_file.id})
    assert_eq(allowed.status_code, 200,
              f"admin may attach their own upload on behalf of a target: {allowed.response}")
    owner = User.objects.get(pk=opts.owner.pk)
    assert_eq(owner.avatar_id, admin_file.id, "admin-owned avatar must attach to target")
    admin_file.refresh_from_db()
    assert_eq(admin_file.user_id, opts.admin.id,
              "admin-on-behalf attach must retain uploader ownership")

    denied = opts.client.post(f"/api/user/{opts.owner.id}", {"avatar": target_file.id})
    assert_eq(denied.status_code, 403,
              f"admin must not attach a target-owned File: {denied.response}")
    assert_eq(User.objects.get(pk=opts.owner.pk).avatar_id, admin_file.id,
              "denied replacement must leave the authoritative relation unchanged")

    cleared = opts.client.post(f"/api/user/{opts.owner.id}", {"avatar": None})
    assert_eq(cleared.status_code, 200, f"admin may clear target avatar: {cleared.response}")
    assert_true(User.objects.get(pk=opts.owner.pk).avatar_id is None,
                "admin clear must detach without deleting the File")
    assert_true(type(admin_file).objects.filter(pk=admin_file.pk).exists(),
                "clearing an avatar must not delete the detached File")


@th.django_unit_test("fileman avatar: non-admin cannot replace another user's avatar")
def test_nonadmin_avatar_on_behalf_denied(opts):
    own_file = _make_avatar(opts, opts.other)
    opts.client.login(OTHER_USER, PWORD)
    resp = opts.client.post(f"/api/user/{opts.owner.id}", {"avatar": own_file.id})
    assert_eq(resp.status_code, 403,
              f"non-admin must not update another user's avatar: {resp.response}")


@th.django_unit_test("fileman avatar: inline uploads use the real actor and personal scope")
def test_inline_avatar_actor_scope(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import File

    User.objects.filter(pk=opts.owner.pk).update(avatar=None)
    opts.client.login(OWNER_USER, PWORD)
    resp = opts.client.post("/api/user/me", {
        "group": opts.group_id,
        "avatar": INLINE_PNG,
    })
    assert_eq(resp.status_code, 200, f"inline avatar must succeed: {resp.response}")
    owner = User.objects.get(pk=opts.owner.pk)
    inline = File.objects.get(pk=owner.avatar_id)
    assert_eq(inline.user_id, opts.owner.id, "inline File must be owned by the real actor")
    assert_true(inline.group_id is None, "User avatar scope must override ambient group to None")
    assert_eq(inline.upload_status, File.COMPLETED, "inline avatar must be completed before attach")


@th.django_unit_test("fileman avatar: inline asset survives a later parent validation failure")
def test_inline_avatar_survives_parent_failure(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import File

    User.objects.filter(pk=opts.owner.pk).update(avatar=None)
    before = File.objects.filter(user_id=opts.owner.id).count()
    opts.client.login(OWNER_USER, PWORD)
    resp = opts.client.post("/api/user/me", {
        "avatar": INLINE_PNG,
        "username": "replacement-name",
    })
    assert_eq(resp.status_code, 400,
              f"unrelated parent validation must fail after inline creation: {resp.response}")
    assert_eq(File.objects.filter(user_id=opts.owner.id).count(), before + 1,
              "inline File must remain an independently owned reusable asset")
    newest = File.objects.filter(user_id=opts.owner.id).order_by("-id").first()
    assert_true(newest.group_id is None and newest.upload_status == File.COMPLETED,
                "surviving inline asset must retain personal completed scope")
    assert_true(User.objects.get(pk=opts.owner.pk).avatar_id is None,
                "failed parent save must not persist the relation")


@th.django_unit_test("fileman relation: existing id resolves once and restores request group")
def test_file_relation_single_resolution_and_group_restore(opts):
    import objict
    from mojo.apps.account.models import Group, User
    from mojo.apps.fileman.models import File
    from mojo import errors as merrors

    owner = User.objects.get(pk=opts.owner.pk)
    candidate = _make_avatar(opts, owner)
    request = objict.objict(
        user=owner, acting_user=None, group=None, api_key=None, group_token=None,
        DATA=objict.objict(), method="POST", path="/test/file-relation", META={})
    field = owner._meta.get_field("avatar")
    with mock.patch.object(File.objects, "get", wraps=File.objects.get) as file_get:
        owner.on_rest_save_related_field(field, candidate.id, request)
    assert_eq(file_get.call_count, 1,
              "existing File candidate must resolve exactly once")

    ambient = Group.objects.get(pk=opts.group_id)
    admin = User.objects.get(pk=opts.admin.pk)
    request.user = admin
    request.group = ambient
    target_owned = _make_avatar(opts, owner)
    try:
        owner.on_rest_save_related_field(field, target_owned.id, request)
        assert_true(False, "target-owned candidate must be denied for admin-on-behalf attach")
    except merrors.PermissionDeniedException:
        pass
    assert_eq(request.group, ambient,
              "File permission/validator failure must restore the caller's group context")


@th.django_unit_test("fileman relation: non-positive id is rejected before lookup")
def test_nonpositive_id_attach_is_gated(opts):
    """The FK-attach gate must cover the EXACT values File.on_rest_related_save's
    int branch attaches. A `> 0` guard on the gate let id 0 / False (bool is an
    int subclass, coerced to pk 0) skip the check yet still reach the ungated
    fetch-and-attach. Construct a File at pk 0 owned by OWNER and confirm a
    non-owner cannot attach it via {"avatar": 0}."""
    from mojo.apps.fileman.models import File
    from mojo.apps.account.models import User

    # Clean baseline: OTHER owns no avatar; a File exists at pk 0 owned by OWNER.
    User.objects.filter(pk=opts.other.pk).update(avatar=None)
    File.objects.filter(pk=0).delete()
    f0 = File(id=0, filename="zero.txt", content_type="text/plain",
              file_size=4, file_manager_id=opts.fm_id, user=opts.owner)
    f0.save(force_insert=True)
    try:
        opts.client.login(OTHER_USER, PWORD)
        resp = opts.client.post("/api/user/me", {"avatar": 0})
        assert_eq(resp.status_code, 400,
                  f"File id 0 must be rejected, got {resp.status_code}: {resp.response}")

        other = User.objects.get(pk=opts.other.pk)
        assert_true(other.avatar_id is None,
                    f"non-owner must not attach the pk-0 file — the gate must cover "
                    f"id 0 / False, not just positive ids; got avatar_id={other.avatar_id}")
    finally:
        User.objects.filter(pk=opts.other.pk).update(avatar=None)
        File.objects.filter(pk=0).delete()


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
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import FileManager, File, UploadInitiation

    UploadInitiation.objects.filter(file__user__in=[opts.owner, opts.other, opts.admin]).delete()
    File.objects.filter(user__in=[opts.owner, opts.other, opts.admin]).delete()
    FileManager.objects.filter(name__startswith="fm_owner_up_").delete()
    GroupMember.objects.filter(group_id=opts.group_id).delete()
    Group.objects.filter(pk=opts.group_id).delete()
    User.objects.filter(username__in=[OWNER_USER, OTHER_USER, ADMIN_USER]).delete()

    if os.path.exists(opts.tmpdir):
        shutil.rmtree(opts.tmpdir, ignore_errors=True)
