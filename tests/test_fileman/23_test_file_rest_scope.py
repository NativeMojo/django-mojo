"""A File's storage over the general file endpoint stays the caller's own.

`POST /api/fileman/file` used to take the storage (`file_manager`), the stored
object's path, the upload token and the upload state from the request body.
A member of one group could point a record at another group's storage or at
any object under a shared folder, get a link to it, and delete or overwrite
it through that record. The storage is now resolved by the upload rule for
user sessions and by the framework's permission check for API keys and group
tokens; everything else that names a stored object is set by the server.

Test-owned data only: groups `file_scope_a`, `file_scope_a1` (a child of A)
and `file_scope_b`, filesystem managers under one temporary folder, and keys
made here. No patch of production code.
"""

TESTIT_TIER = "core"

import os
import tempfile

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PREFIX = "file_scope_"
PASSWORD = "file-scope-pass-99!"
MEMBER = "file_scope_member"      # `files` in A
OTHER = "file_scope_other"        # plain member of A
VIEWER = "file_scope_viewer"      # `files` in A, `view_fileman` in B
LINKED = "file_scope_linked"      # `files` in A and in B; linked to the override key
ADMIN = "file_scope_admin"        # global `manage_files`
SERVICE = "file_scope_service"    # global `files`, uses a per-user key
USERS = [MEMBER, OTHER, VIEWER, LINKED, ADMIN, SERVICE]

B_NAME = "secret_b.txt"
B_PATH = f"file_scope_b/{B_NAME}"
B_BYTES = b"B-SECRET"


def _clear():
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.account.models.user_api_key import UserAPIKey
    from mojo.apps.fileman.models import File, FileManager

    File.objects.filter(file_manager__name__startswith=PREFIX).delete()
    File.objects.filter(filename__startswith=PREFIX).delete()
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    UserAPIKey.objects.filter(user__username__in=USERS).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name=f"{PREFIX}a1").delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__in=USERS).delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_file_rest_scope(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.account.models.user_api_key import UserAPIKey
    from mojo.apps.account.services import group_token
    from mojo.apps.fileman.models import FileManager
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    _clear()

    def make_user(username):
        user = User(username=username, email=f"{username}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PASSWORD)
        user.save()
        return user

    def grant(group, user, *perms):
        member = group.add_member(user)
        for perm in perms:
            member.add_permission(perm)
        member.save()
        return member

    users = {name: make_user(name) for name in USERS}
    users[ADMIN].add_permission("manage_files")
    users[ADMIN].save()
    users[SERVICE].add_permission("files")
    users[SERVICE].save()

    group_a = Group(name=f"{PREFIX}a")
    group_a.save()
    group_b = Group(name=f"{PREFIX}b")
    group_b.save()
    group_a1 = Group(name=f"{PREFIX}a1", parent=group_a)
    group_a1.save()
    grant(group_a, users[MEMBER], "files")
    grant(group_a, users[OTHER])
    grant(group_a, users[VIEWER], "files")
    grant(group_b, users[VIEWER], "view_fileman")
    grant(group_a, users[LINKED], "files")
    grant(group_b, users[LINKED], "files")

    opts.tmpdir = tempfile.mkdtemp(prefix=PREFIX)

    def make_manager(name, root, **scope):
        manager = FileManager(
            name=f"{PREFIX}{name}", backend_type="file",
            backend_url=f"file:///{root}", is_active=True, **scope)
        manager.save()
        manager.set_setting("base_path", opts.tmpdir)
        manager.set_setting("use_shortlinks", False)
        manager.save(update_fields=["mojo_secrets", "modified"])
        return manager.id

    opts.fm_a = make_manager("a", "file_scope_a", group=group_a, is_default=True)
    opts.fm_a_fw = make_manager("a_fw", "file_scope_a_fw", group=group_a, use="firmware")
    opts.fm_a1 = make_manager("a1", "file_scope_a1", group=group_a1, is_default=True)
    opts.fm_b = make_manager("b", "file_scope_b", group=group_b, is_default=True)
    opts.fm_user = make_manager("user", "file_scope_user", user=users[MEMBER])
    opts.fm_system = make_manager("system", "file_scope_system", is_default=False)

    opts.group_a = group_a.id
    opts.group_a1 = group_a1.id
    opts.group_b = group_b.id
    opts.user_ids = {name: user.id for name, user in users.items()}

    _, ka = ApiKey.create_for_group(group_a, f"{PREFIX}ka", permissions={"files": True})
    _, ko = ApiKey.create_for_group(
        group_a, f"{PREFIX}ko", user=users[LINKED], override_user=True)
    opts.ka = {"Authorization": f"apikey {ka}"}
    opts.ko = {"Authorization": f"apikey {ko}"}
    opts.token_a = {"Authorization": f"grouptoken {group_token.mint(users[MEMBER], group_a)}"}
    service_key = UserAPIKey.create_for_user(users[SERVICE], expire_days=1, label=PREFIX)
    opts.service = {"Authorization": f"Bearer {service_key.token}"}


def _login(opts, username):
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _b_object(opts):
    """Write group B's stored object afresh and return its full path."""
    full = os.path.join(opts.tmpdir, B_PATH)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as handle:
        handle.write(B_BYTES)
    return full


def _b_intact(opts):
    full = os.path.join(opts.tmpdir, B_PATH)
    if not os.path.exists(full):
        return False
    with open(full, "rb") as handle:
        return handle.read() == B_BYTES


def _create(opts, body, headers=None, files=None):
    payload = {"graph": "detailed"}
    payload.update(body)
    if files:
        return opts.client.post("/api/fileman/file", data=payload, files=files, headers=headers)
    return opts.client.post("/api/fileman/file", payload, headers=headers)


def _multipart(name="upload.txt"):
    return {"file": (name, b"data", "text/plain")}


def _row(response):
    from mojo.apps.fileman.models import File

    data = getattr(response.response, "data", None) or {}
    pk = data.get("id") if hasattr(data, "get") else None
    return File.objects.filter(pk=pk).first() if pk else None


def _assert_refused(response, label):
    from mojo.apps.fileman.models import File

    assert_eq(response.status_code, 403, f"{label}: must be refused: {response.response}")
    assert_true(not File.objects.filter(file_manager__name__startswith=PREFIX,
                                        filename__startswith=label).exists(),
                f"{label}: a refused create must leave no record")


def _assert_created_in(response, manager_id, group_id, label):
    assert_eq(response.status_code, 200, f"{label}: must be created: {response.response}")
    row = _row(response)
    assert_true(row is not None, f"{label}: the record must exist: {response.response}")
    assert_eq(row.file_manager_id, manager_id, f"{label}: wrong storage")
    assert_eq(row.group_id, group_id, f"{label}: wrong group")
    return row


def _own_file(opts, username, filename):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import File, FileManager

    manager = FileManager.objects.get(pk=opts.fm_a)
    row = File(filename=filename, content_type="text/plain", file_manager=manager,
               group_id=opts.group_a, user=User.objects.get(username=username))
    row.on_rest_pre_save({}, True)
    row.upload_status = File.COMPLETED
    row.save()
    return row


@th.django_unit_test("a member naming another group's storage on create is refused")
def test_create_with_foreign_manager_is_refused(opts):
    _b_object(opts)
    _login(opts, MEMBER)
    for alias in ("file_manager", "manager"):
        label = f"{PREFIX}foreign_{alias}"
        response = _create(opts, {
            "group": opts.group_a, alias: opts.fm_b, "filename": f"{label}.txt",
            "storage_filename": B_NAME, "storage_file_path": B_PATH})
        _assert_refused(response, label)
        assert_true("url" not in str(response.response) or B_PATH not in str(response.response),
                    f"{alias}: no link to B's object may be returned")


@th.django_unit_test("a supplied stored-object path is ignored on create")
def test_create_ignores_supplied_storage_path(opts):
    from mojo.apps.fileman.models import File

    _b_object(opts)
    _login(opts, MEMBER)
    b_url = File(file_manager_id=opts.fm_b, storage_file_path=B_PATH,
                 upload_status=File.COMPLETED).file_manager.backend.get_url(B_PATH)
    for index, path in enumerate((B_PATH, f" {B_PATH}", b_url)):
        label = f"{PREFIX}path_{index}"
        response = _create(opts, {
            "group": opts.group_a, "filename": f"{label}.txt",
            "storage_filename": B_NAME, "storage_file_path": path})
        row = _assert_created_in(response, opts.fm_a, opts.group_a, label)
        assert_true(row.storage_file_path.startswith("file_scope_a/"),
                    f"{label}: the path must be generated under A's root: {row.storage_file_path}")
        assert_true(B_NAME != row.storage_filename, f"{label}: the storage name must be generated")
        assert_true(B_PATH not in str(response.response),
                    f"{label}: the response must not point at B's object")


@th.django_unit_test("a supplied upload token and state are ignored; B's object cannot be overwritten")
def test_create_ignores_token_and_status(opts):
    from mojo.apps.fileman.models import File

    _b_object(opts)
    _login(opts, MEMBER)
    token = "f1e5c09e7971f1e5c09e7971f1e5c09e"
    label = f"{PREFIX}token"
    response = _create(opts, {
        "group": opts.group_a, "filename": f"{label}.txt",
        "storage_filename": B_NAME, "storage_file_path": B_PATH,
        "upload_token": token, "upload_status": File.UPLOADING})
    row = _assert_created_in(response, opts.fm_a, opts.group_a, label)
    assert_true(row.upload_token != token, "a supplied upload token must not be stored")
    assert_eq(row.upload_status, File.PENDING, "a supplied upload state must not be stored")
    upload = opts.client.post(f"/api/fileman/upload/{token}",
                              files={"file": (B_NAME, b"OVERWRITE", "text/plain")})
    assert_eq(upload.status_code, 404, f"the supplied token must not be accepted: {upload.response}")
    assert_true(_b_intact(opts), "B's stored object must be unchanged")


@th.django_unit_test("a save cannot repoint a record to another group's storage")
def test_save_cannot_repoint_manager(opts):
    _login(opts, VIEWER)
    row = _own_file(opts, VIEWER, f"{PREFIX}repoint_manager.txt")
    response = opts.client.post(f"/api/fileman/file/{row.id}", {"file_manager": opts.fm_b})
    row.refresh_from_db()
    assert_eq(row.file_manager_id, opts.fm_a,
              f"the storage must not change on save: {response.response}")


@th.django_unit_test("a save cannot change the stored path, upload state or owner")
def test_save_cannot_repoint_storage_or_owner(opts):
    _login(opts, MEMBER)
    row = _own_file(opts, MEMBER, f"{PREFIX}repoint_storage.txt")
    before = (row.storage_filename, row.storage_file_path, row.upload_token,
              row.upload_status, row.user_id, row.download_url)
    response = opts.client.post(f"/api/fileman/file/{row.id}", {
        "storage_filename": B_NAME, "storage_file_path": B_PATH,
        "upload_token": "f1e5c09e7971f1e5c09e7971f1e5c09f", "upload_status": "uploading",
        "download_url": "https://example.com/elsewhere",
        "user": opts.user_ids[OTHER]})
    row.refresh_from_db()
    after = (row.storage_filename, row.storage_file_path, row.upload_token,
             row.upload_status, row.user_id, row.download_url)
    assert_eq(after, before, f"server-owned values must not change on save: {response.response}")


@th.django_unit_test("deleting one's own record never removes another group's object")
def test_delete_never_removes_foreign_object(opts):
    from mojo.apps.fileman.models import File

    _b_object(opts)
    _login(opts, MEMBER)
    _create(opts, {"group": opts.group_a, "file_manager": opts.fm_b,
                   "filename": f"{PREFIX}del_foreign.txt",
                   "storage_filename": B_NAME, "storage_file_path": B_PATH})
    _create(opts, {"group": opts.group_a, "filename": f"{PREFIX}del_path.txt",
                   "storage_filename": B_NAME, "storage_file_path": B_PATH})
    own = File.objects.filter(filename__startswith=f"{PREFIX}del_",
                              user_id=opts.user_ids[MEMBER])
    for row in own:
        response = opts.client.delete(f"/api/fileman/file/{row.id}")
        assert_eq(response.status_code, 200, f"own record must delete: {response.response}")
    assert_true(_b_intact(opts), "B's stored object must still be on disk")


@th.django_unit_test("a member's full upload cycle in their own group still works")
def test_member_full_cycle_in_own_group(opts):
    from mojo.apps.fileman.models import File

    _login(opts, MEMBER)
    initiated = opts.client.post("/api/fileman/upload/initiate", {
        "filename": f"{PREFIX}cycle.txt", "content_type": "text/plain", "file_size": 4,
        "file_manager": opts.fm_a, "group": opts.group_a})
    assert_eq(initiated.status_code, 200, f"initiate must succeed: {initiated.response}")
    data = initiated.response.data
    transfer = opts.client.put(data.upload_url, data=b"test",
                               headers={"Content-Type": "text/plain", "Content-Length": "4"})
    assert_eq(transfer.status_code, 200, f"transfer must succeed: {transfer.response}")
    done = opts.client.post(f"/api/fileman/file/{data.id}", {"action": "mark_as_completed"})
    assert_eq(done.status_code, 200, f"completion must succeed: {done.response}")
    row = File.objects.get(pk=data.id)
    assert_eq(row.upload_status, File.COMPLETED, "the file must be completed")
    read = opts.client.get(f"/api/fileman/file/{data.id}", params={"graph": "detailed"})
    assert_eq(read.status_code, 200, f"read must succeed: {read.response}")
    assert_true(read.response.data.get("url"), f"the link must be returned: {read.response}")
    gone = opts.client.delete(f"/api/fileman/file/{data.id}")
    assert_eq(gone.status_code, 200, f"delete must succeed: {gone.response}")


@th.django_unit_test("a member's generic create in their own group still works")
def test_member_generic_create_in_own_group(opts):
    _login(opts, MEMBER)
    label = f"{PREFIX}generic"
    response = _create(opts, {"group": opts.group_a, "filename": f"{label}.txt"},
                       files=_multipart(f"{label}.txt"))
    _assert_created_in(response, opts.fm_a, opts.group_a, label)
    label = f"{PREFIX}generic_use"
    response = _create(opts, {"group": opts.group_a, "use": "firmware",
                              "filename": f"{label}.txt"},
                       files=_multipart(f"{label}.txt"))
    _assert_created_in(response, opts.fm_a_fw, opts.group_a, label)


@th.django_unit_test("a multipart create records the uploaded size")
def test_multipart_create_records_size(opts):
    _login(opts, MEMBER)
    label = f"{PREFIX}size"
    response = _create(opts, {"group": opts.group_a, "filename": f"{label}.txt"},
                       files=_multipart(f"{label}.txt"))
    row = _assert_created_in(response, opts.fm_a, opts.group_a, label)
    assert_eq(row.file_size, 4, "the multipart create must record the size")


@th.django_unit_test("a global file admin may still name any manager")
def test_admin_create_names_any_manager(opts):
    _login(opts, ADMIN)
    label = f"{PREFIX}admin"
    response = _create(opts, {"file_manager": opts.fm_b, "filename": f"{label}.txt"})
    assert_eq(response.status_code, 200, f"{label}: must be created: {response.response}")
    assert_eq(_row(response).file_manager_id, opts.fm_b, "wrong storage")


@th.django_unit_test("a group key creates, saves, reads and deletes in its own group")
def test_group_key_creates_in_own_group(opts):
    label = f"{PREFIX}ka_own"
    response = _create(opts, {"filename": f"{label}.txt"}, headers=opts.ka,
                       files=_multipart(f"{label}.txt"))
    row = _assert_created_in(response, opts.fm_a, opts.group_a, label)
    assert_true(row.storage_file_path.startswith("file_scope_a/"),
                f"the path must be under A's root: {row.storage_file_path}")
    saved = opts.client.post(f"/api/fileman/file/{row.id}",
                             {"filename": f"{label}_renamed.txt"}, headers=opts.ka)
    assert_eq(saved.status_code, 200, f"the key must save its record: {saved.response}")
    read = opts.client.get(f"/api/fileman/file/{row.id}", params={"graph": "detailed"},
                           headers=opts.ka)
    assert_eq(read.status_code, 200, f"the key must read its record: {read.response}")
    gone = opts.client.delete(f"/api/fileman/file/{row.id}", headers=opts.ka)
    assert_eq(gone.status_code, 200, f"the key must delete its record: {gone.response}")


@th.django_unit_test("a group key may name its own group's storage")
def test_group_key_creates_with_own_manager_named(opts):
    label = f"{PREFIX}ka_named"
    response = _create(opts, {"file_manager": opts.fm_a, "filename": f"{label}.txt"},
                       headers=opts.ka)
    _assert_created_in(response, opts.fm_a, opts.group_a, label)


@th.django_unit_test("a group key may select its own storage by use")
def test_group_key_creates_with_use(opts):
    label = f"{PREFIX}ka_use"
    response = _create(opts, {"use": "firmware", "filename": f"{label}.txt"},
                       headers=opts.ka)
    _assert_created_in(response, opts.fm_a_fw, opts.group_a, label)


@th.django_unit_test("a parent group key may use a child group's storage")
def test_group_key_creates_in_descendant(opts):
    label = f"{PREFIX}ka_child"
    response = _create(opts, {"file_manager": opts.fm_a1, "filename": f"{label}.txt"},
                       headers=opts.ka)
    assert_eq(response.status_code, 200, f"{label}: must be created: {response.response}")
    assert_eq(_row(response).file_manager_id, opts.fm_a1, "wrong storage")
    label = f"{PREFIX}ka_child_group"
    response = _create(opts, {"file_manager": opts.fm_a1, "group": opts.group_a1,
                              "filename": f"{label}.txt"}, headers=opts.ka)
    _assert_created_in(response, opts.fm_a1, opts.group_a1, label)


@th.django_unit_test("an override key creates in its own group as its linked member")
def test_override_key_creates_in_own_group(opts):
    label = f"{PREFIX}ko_own"
    response = _create(opts, {"filename": f"{label}.txt"}, headers=opts.ko)
    row = _assert_created_in(response, opts.fm_a, opts.group_a, label)
    assert_eq(row.user_id, opts.user_ids[LINKED], "the record must name the linked member")


@th.django_unit_test("a per-user key with the global files permission may use any storage")
def test_service_user_key_creates_in_system_manager(opts):
    label = f"{PREFIX}service_system"
    response = _create(opts, {"file_manager": opts.fm_system, "filename": f"{label}.txt"},
                       headers=opts.service)
    assert_eq(response.status_code, 200, f"{label}: must be created: {response.response}")
    assert_eq(_row(response).file_manager_id, opts.fm_system, "wrong storage")
    label = f"{PREFIX}service_b"
    response = _create(opts, {"file_manager": opts.fm_b, "filename": f"{label}.txt"},
                       headers=opts.service)
    assert_eq(response.status_code, 200, f"{label}: must be created: {response.response}")
    assert_eq(_row(response).file_manager_id, opts.fm_b, "wrong storage")


@th.django_unit_test("server code that sets the storage first is not checked or changed")
def test_server_code_preset_manager_is_untouched(opts):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from mojo.apps.account.models import Group, User
    from mojo.apps.fileman.models import File, FileManager

    foreign = FileManager.objects.get(pk=opts.fm_b)
    row = File(filename=f"{PREFIX}server.txt", content_type="text/plain",
               file_manager=foreign, group_id=opts.group_a)
    row.on_rest_pre_save({}, True)
    row.save()
    row.refresh_from_db()
    assert_eq((row.file_manager_id, row.group_id), (opts.fm_b, opts.group_a),
              "a preset manager and group must stay as set")

    created = File.create_from_file(
        SimpleUploadedFile(f"{PREFIX}server_upload.txt", b"data", content_type="text/plain"),
        "file", user=User.objects.get(pk=opts.user_ids[MEMBER]),
        group=Group.objects.get(pk=opts.group_a), file_manager=foreign)
    created.refresh_from_db()
    assert_eq((created.file_manager_id, created.group_id), (opts.fm_b, opts.group_a),
              "create_from_file must keep the manager and group it was given")


@th.django_unit_test("a group key naming another group's storage is refused")
def test_group_key_foreign_manager_is_refused(opts):
    for alias in ("file_manager", "manager"):
        label = f"{PREFIX}ka_foreign_{alias}"
        response = _create(opts, {alias: opts.fm_b, "filename": f"{label}.txt"},
                           headers=opts.ka)
        _assert_refused(response, label)


@th.django_unit_test("a group key's supplied path is ignored and its delete leaves B's object")
def test_group_key_storage_path_is_ignored(opts):
    _b_object(opts)
    label = f"{PREFIX}ka_path"
    response = _create(opts, {"filename": f"{label}.txt", "storage_filename": B_NAME,
                              "storage_file_path": B_PATH}, headers=opts.ka)
    assert_eq(response.status_code, 200, f"{label}: must be created: {response.response}")
    row = _row(response)
    assert_true(row.storage_file_path != B_PATH, "the supplied path must not be stored")
    gone = opts.client.delete(f"/api/fileman/file/{row.id}", headers=opts.ka)
    assert_eq(gone.status_code, 200, f"the key must delete its record: {gone.response}")
    assert_true(_b_intact(opts), "B's stored object must still be on disk")


@th.django_unit_test("a group key naming personal storage is refused")
def test_group_key_user_manager_is_refused(opts):
    label = f"{PREFIX}ka_user_fm"
    response = _create(opts, {"file_manager": opts.fm_user, "filename": f"{label}.txt"},
                       headers=opts.ka)
    _assert_refused(response, label)


@th.django_unit_test("a group key naming platform storage is refused")
def test_group_key_system_manager_is_refused(opts):
    label = f"{PREFIX}ka_system_fm"
    response = _create(opts, {"file_manager": opts.fm_system, "filename": f"{label}.txt"},
                       headers=opts.ka)
    _assert_refused(response, label)


@th.django_unit_test("an override key cannot follow its member into another tenant")
def test_override_key_cannot_follow_member_to_other_tenant(opts):
    label = f"{PREFIX}ko_foreign"
    response = _create(opts, {"file_manager": opts.fm_b, "filename": f"{label}.txt"},
                       headers=opts.ko)
    _assert_refused(response, label)


@th.django_unit_test("a group token creates in its own group only")
def test_group_token_stays_in_its_group(opts):
    label = f"{PREFIX}token_own"
    response = _create(opts, {"filename": f"{label}.txt"}, headers=opts.token_a)
    _assert_created_in(response, opts.fm_a, opts.group_a, label)
    label = f"{PREFIX}token_foreign"
    response = _create(opts, {"file_manager": opts.fm_b, "filename": f"{label}.txt"},
                       headers=opts.token_a)
    _assert_refused(response, label)
