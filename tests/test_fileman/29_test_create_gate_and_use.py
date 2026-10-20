"""Storage is created for a group only for someone who may use it, and every
write path checks the upload rule first (#8661, with #7981's purpose check).

A request names a group, and with it a storage purpose (`use`). When that
group had no storage for the purpose, the framework created it, copying the
system default, before asking who was calling. Inline base64 fields, a
multipart update of an existing File and `create_from_file(request=...)` wrote
rows and bytes without the upload rule at all. And `use` went into the storage
path unchecked, so `../x` or `/x` moved the storage root.

Now `use` must be a plain short name, a request creates storage only for an
active member of the group (parents walked) or a holder of a global files
grant, and the existing upload rule runs before any row or byte is written.
Members keep every upload they had, public storage included.

Test-owned data only (prefix `fm_gate_`), filesystem managers under a
temporary folder, and a system default with a dedicated negative id, created
and removed inside the tests that need one (the device of
12_test_public_access_reconciliation.py). No patch of shared code.
"""

TESTIT_TIER = "framework"

import base64
import io
import tempfile
from contextlib import contextmanager

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PREFIX = "fm_gate_"
PASSWORD = "fm-gate-pass-99!"
SYSTEM_DEFAULT_PK = -8661
MEMBER = "fm_gate_member"          # plain member of A, no grants
OUTSIDER = "fm_gate_outsider"      # member of nothing
GADMIN = "fm_gate_gadmin"          # member of A with manage_group there, not of B
GLOBAL_GADMIN = "fm_gate_global"   # global manage_groups, member of nothing
LEFT = "fm_gate_left"              # owned a File in A, then left A
SECURITY = "fm_gate_security"      # global security grant only, member of nothing
FILES_ADMIN = "fm_gate_files"      # global files grant, member of nothing
GLOBAL_MANAGE_GROUP = "fm_gate_mgroup"  # global manage_group only, member of nothing
USERS = [MEMBER, OUTSIDER, GADMIN, GLOBAL_GADMIN, LEFT, SECURITY, FILES_ADMIN,
         GLOBAL_MANAGE_GROUP]
INLINE = "data:text/plain;base64," + base64.b64encode(b"fm gate inline").decode()
ORIGINAL_BYTES = b"fm gate original"


def _clear():
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.fileman.models import File, FileManager

    FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    Group.objects.filter(name__startswith=PREFIX).update(avatar=None)
    File.objects.filter(group__name__startswith=PREFIX).delete()
    File.objects.filter(user__username__in=USERS).delete()
    FileManager.objects.filter(group__name__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__in=USERS).delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_create_gate(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.fileman.models import File, FileManager
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

    users = {name: make_user(name) for name in USERS}
    users[GLOBAL_GADMIN].add_permission("manage_groups")
    users[GLOBAL_GADMIN].save()
    users[SECURITY].add_permission("security")
    users[SECURITY].save()
    users[FILES_ADMIN].add_permission("files")
    users[FILES_ADMIN].save()
    users[GLOBAL_MANAGE_GROUP].add_permission("manage_group")
    users[GLOBAL_MANAGE_GROUP].save()

    group_a = Group(name=f"{PREFIX}a")
    group_a.save()
    group_b = Group(name=f"{PREFIX}b")
    group_b.save()
    group_c = Group(name=f"{PREFIX}c")  # no storage at all: the key inline case
    group_c.save()
    group_d = Group(name=f"{PREFIX}d")  # no storage at all: a member creates the default
    group_d.save()
    group_d.add_member(users[MEMBER])
    group_a.add_member(users[MEMBER])
    gadmin = group_a.add_member(users[GADMIN])
    gadmin.add_permission("manage_group")
    gadmin.save()
    left = group_a.add_member(users[LEFT])

    opts.tmpdir = tempfile.mkdtemp(prefix=PREFIX)

    def make_manager(name, **scope):
        manager = FileManager(name=f"{PREFIX}{name}", backend_type="file",
                              backend_url=f"file:///{PREFIX}{name}",
                              **{"is_active": True, **scope})
        manager.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
        manager.save()
        return manager

    fm_a = make_manager("a", group=group_a, is_default=True, is_public=True)
    make_manager("a_existing", group=group_a, use="existing", is_public=False)

    def owned_file(user, label):
        handle = io.BytesIO(ORIGINAL_BYTES)
        handle.name = f"{label}.txt"
        handle.size = len(ORIGINAL_BYTES)
        handle.content_type = "text/plain"
        return File.create_from_file(handle, handle.name, user=user, group=group_a,
                                     file_manager=fm_a).id

    opts.left_file = owned_file(users[LEFT], "left")
    opts.member_file = owned_file(users[MEMBER], "member")
    left.is_active = False
    left.save()

    opts.group_ids = {"a": group_a.id, "b": group_b.id, "c": group_c.id, "d": group_d.id}
    opts.user_ids = {name: user.id for name, user in users.items()}
    ApiKey.create_for_group(group_b, f"{PREFIX}key_b")
    opts.key_c_id = ApiKey.create_for_group(group_c, f"{PREFIX}key_c")[0].id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@contextmanager
def _system_default(opts, is_public=False):
    """The system default every derived manager copies, for one test."""
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    system = FileManager(pk=SYSTEM_DEFAULT_PK, name=f"{PREFIX}system", backend_type="file",
                         backend_url="file://", is_active=True, is_default=True,
                         is_public=is_public)
    system.save()
    system.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
    system.save()
    try:
        yield system
    finally:
        FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
        system.delete()


def _group(opts, name):
    from mojo.apps.account.models import Group

    return Group.objects.get(pk=opts.group_ids[name])


def _user(opts, name):
    from mojo.apps.account.models import User

    return User.objects.get(pk=opts.user_ids[name])


def _managers(opts, name):
    from mojo.apps.fileman.models import FileManager

    return FileManager.objects.filter(group_id=opts.group_ids[name]).count()


def _files(opts, name):
    from mojo.apps.fileman.models import File

    return File.objects.filter(group_id=opts.group_ids[name]).count()


def _reset_b(opts):
    """B starts every test with no storage, no files and no uuid."""
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import File, FileManager

    File.objects.filter(group_id=opts.group_ids["b"]).delete()
    FileManager.objects.filter(group_id=opts.group_ids["b"]).delete()
    Group.objects.filter(pk=opts.group_ids["b"]).update(uuid=None)


def _uuid(opts, name):
    from mojo.apps.account.models import Group

    return Group.objects.get(pk=opts.group_ids[name]).uuid


def _drop_purpose(opts, group, use):
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(group_id=opts.group_ids[group], use=use).delete()


def _login(opts, username):
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _initiate(opts, **body):
    payload = {"filename": "gate.txt", "content_type": "text/plain", "file_size": 4}
    payload.update(body)
    return opts.client.post("/api/fileman/upload/initiate", payload)


def _request(opts, user=None, group=None, data=None, api_key=None):
    """A request as the dispatcher leaves it: `group` bound, DATA parsed."""
    from django.contrib.auth.models import AnonymousUser
    from objict import objict
    from testit.helpers import get_mock_request

    request = get_mock_request(user=user or AnonymousUser())
    if api_key is not None and user is None:
        # As the bearer-token path leaves an unlinked key (ApiKey.validate_*).
        api_key.is_authenticated = True
        api_key.username = f"apikey:{api_key.id}"
    request.user = user if user is not None else (api_key or AnonymousUser())
    request.group = _group(opts, group) if group else None
    request.DATA = objict(data or {})
    request.api_key = api_key
    request.group_token = None
    request.user_api_key = None
    return request


def _stored_bytes(file_id):
    from mojo.apps.fileman.models import File

    row = File.objects.get(pk=file_id)
    with row.file_manager.backend.open(row.storage_file_path) as handle:
        return row, handle.read()


# ---------------------------------------------------------------------------
# Step 1: the purpose name
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("a request's purpose cannot be a relative path")
def test_use_relative_path_refused(opts):
    _login(opts, MEMBER)
    with _system_default(opts):
        before = _managers(opts, "a")
        response = _initiate(opts, group=opts.group_ids["a"], use="../escape")
        after = _managers(opts, "a")
    assert_eq(response.status_code, 400, f"a path purpose must be refused: {response.response}")
    assert_eq(after, before, "a refused purpose must create no storage")


@th.django_unit_test("a request's purpose cannot be an absolute path")
def test_use_absolute_path_refused(opts):
    _login(opts, MEMBER)
    with _system_default(opts):
        before = _managers(opts, "a")
        response = _initiate(opts, group=opts.group_ids["a"], use="/abs")
        after = _managers(opts, "a")
    assert_eq(response.status_code, 400, f"an absolute purpose must be refused: {response.response}")
    assert_eq(after, before, "a refused purpose must create no storage")


@th.django_unit_test("server code cannot create storage with a path purpose either")
def test_get_for_group_refuses_path(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import FileManager

    with _system_default(opts):
        before = _managers(opts, "a")
        refused = False
        try:
            FileManager.get_for_group(_group(opts, "a"), use="../x")
        except me.ValueException:
            refused = True
        after = _managers(opts, "a")
    assert_true(refused, "get_for_group must refuse a path purpose")
    assert_eq(after, before, "a refused purpose must create no storage")


@th.django_unit_test("a purpose with a leading or trailing newline is refused")
def test_clean_use_refuses_newlines(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import FileManager

    accepted = []
    for value in ("x\n", "filevault\n", "a" * 64 + "\n", "\nx"):
        try:
            FileManager.clean_use(value, from_request=True)
            accepted.append(value)
        except me.ValueException:
            pass
    assert_eq(accepted, [], "clean_use must refuse every newline purpose with a 400")


@th.django_unit_test("a request's reserved purpose with a trailing newline is refused over HTTP")
def test_use_filevault_newline_refused(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    _drop_purpose(opts, "a", "filevault")
    with _system_default(opts):
        before, before_uuid = _managers(opts, "a"), _uuid(opts, "a")
        response = _initiate(opts, group=opts.group_ids["a"], use="filevault\n")
        after, after_uuid = _managers(opts, "a"), _uuid(opts, "a")
    FileManager.objects.filter(group_id=opts.group_ids["a"], use__startswith="filevault").delete()
    assert_eq(response.status_code, 400, f"a newline purpose must be refused: {response.response}")
    assert_eq(after, before, "a refused purpose must create no storage")
    assert_eq(after_uuid, before_uuid, "a refused purpose must not write the group's uuid")


@th.django_unit_test("control: a member still uses an ordinary purpose name")
def test_member_invoices_purpose(opts):
    _login(opts, MEMBER)
    _drop_purpose(opts, "a", "invoices")
    with _system_default(opts):
        response = _initiate(opts, group=opts.group_ids["a"], use="invoices")
    _drop_purpose(opts, "a", "invoices")
    assert_eq(response.status_code, 200, f"use=invoices must still work: {response.response}")


@th.django_unit_test("a request cannot select the vault's reserved purpose")
def test_use_filevault_reserved(opts):
    _login(opts, MEMBER)
    _drop_purpose(opts, "a", "filevault")
    with _system_default(opts):
        before = _managers(opts, "a")
        response = _initiate(opts, group=opts.group_ids["a"], use="filevault")
        after = _managers(opts, "a")
    _drop_purpose(opts, "a", "filevault")
    assert_eq(response.status_code, 403, f"the vault's purpose is reserved: {response.response}")
    assert_eq(after, before, "a reserved purpose must create no storage")


# ---------------------------------------------------------------------------
# Step 2: who may cause storage to be created
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("a non-member cannot create a purpose's storage in another group")
def test_outsider_cannot_create_purpose(opts):
    _reset_b(opts)
    _login(opts, OUTSIDER)
    with _system_default(opts):
        response = _initiate(opts, group=opts.group_ids["b"], use="newpurpose")
        after = _managers(opts, "b")
    assert_eq(response.status_code, 403, f"a non-member must be refused: {response.response}")
    assert_eq(after, 0, "a refused non-member must create no storage in B")


@th.django_unit_test("a non-member cannot create another group's default storage")
def test_outsider_cannot_create_default(opts):
    from mojo.apps.account.models import Group

    _reset_b(opts)
    _login(opts, OUTSIDER)
    with _system_default(opts):
        response = _initiate(opts, group=opts.group_ids["b"])
        after = _managers(opts, "b")
    assert_eq(response.status_code, 403, f"a non-member must be refused: {response.response}")
    assert_eq(after, 0, "a refused non-member must create no default storage in B")
    assert_true(not Group.objects.get(pk=opts.group_ids["b"]).uuid,
                "a refused non-member must not write B's uuid")


@th.django_unit_test("a key without files is refused before any storage is created")
def test_key_without_files_creates_nothing(opts):
    from mojo import errors as me
    from mojo.apps.account.models import ApiKey
    from mojo.apps.fileman.models import FileManager

    _reset_b(opts)
    key = ApiKey.objects.get(name=f"{PREFIX}key_b")
    request = _request(opts, group="b", api_key=key, data={"use": "newpurpose"})
    refused = False
    with _system_default(opts):
        try:
            FileManager.resolve_for_file_create(request)
        except me.PermissionDeniedException:
            refused = True
        after = _managers(opts, "b")
    assert_true(refused, "a key without files must be refused on the general file endpoint")
    assert_eq(after, 0, "the key's refusal must come before any storage row")


@th.django_unit_test("a global manage_group grant alone cannot create another group's storage over HTTP")
def test_global_manage_group_initiate_refused(opts):
    _reset_b(opts)
    _login(opts, GLOBAL_MANAGE_GROUP)
    with _system_default(opts):
        response = _initiate(opts, group=opts.group_ids["b"], use="newpurpose")
        after = _managers(opts, "b")
    assert_eq(response.status_code, 403, f"global manage_group alone must be refused: {response.response}")
    assert_eq(after, 0, "no storage may be created in B")
    assert_true(not _uuid(opts, "b"), "B's uuid must not be written")


@th.django_unit_test("a global manage_group grant alone cannot create storage through create_from_file")
def test_global_manage_group_create_from_file_refused(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import File

    _reset_b(opts)
    handle = io.BytesIO(b"x")
    handle.name = "x.txt"
    handle.size = 1
    handle.content_type = "text/plain"
    request = _request(opts, user=_user(opts, GLOBAL_MANAGE_GROUP), group="b")
    refused = False
    with _system_default(opts):
        try:
            File.create_from_file(handle, "x", request=request)
        except me.PermissionDeniedException:
            refused = True
        managers, files = _managers(opts, "b"), _files(opts, "b")
    assert_true(refused, "global manage_group alone must be refused")
    assert_eq(managers, 0, "no storage may be created in B")
    assert_eq(files, 0, "no File may be written in B")
    assert_true(not _uuid(opts, "b"), "B's uuid must not be written")


@th.django_unit_test("a global manage_group grant alone cannot create storage through an inline write")
def test_global_manage_group_inline_refused(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import File

    _reset_b(opts)
    request = _request(opts, user=_user(opts, GLOBAL_MANAGE_GROUP), group="b")
    refused = False
    with _system_default(opts):
        try:
            File._inline_rest_file(object(), "media", INLINE, request)
        except me.PermissionDeniedException:
            refused = True
        managers, files = _managers(opts, "b"), _files(opts, "b")
    assert_true(refused, "global manage_group alone must be refused")
    assert_eq(managers, 0, "no storage may be created in B")
    assert_eq(files, 0, "no File may be written in B")
    assert_true(not _uuid(opts, "b"), "B's uuid must not be written")


@th.django_unit_test("control: a plain member still creates their group's default storage")
def test_member_creates_default(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    FileManager.objects.filter(group_id=opts.group_ids["d"]).delete()
    with _system_default(opts):
        response = _initiate(opts, group=opts.group_ids["d"])
        created = FileManager.objects.filter(group_id=opts.group_ids["d"], use="", is_default=True).exists()
    FileManager.objects.filter(group_id=opts.group_ids["d"]).delete()
    assert_eq(response.status_code, 200, f"a member must still create the group default: {response.response}")
    assert_true(created, "the member's group default storage must exist")


@th.django_unit_test("control: a plain member still creates a new purpose for their group")
def test_member_creates_purpose(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    _drop_purpose(opts, "a", "newpurpose")
    with _system_default(opts):
        response = _initiate(opts, group=opts.group_ids["a"], use="newpurpose")
        created = FileManager.objects.filter(group_id=opts.group_ids["a"], use="newpurpose").exists()
    _drop_purpose(opts, "a", "newpurpose")
    assert_eq(response.status_code, 200, f"a member must still create a purpose: {response.response}")
    assert_true(created, "the member's purpose storage must exist")


@th.django_unit_test("control: a plain member still uploads to the group's public storage")
def test_member_public_default(opts):
    _login(opts, MEMBER)
    response = _initiate(opts, group=opts.group_ids["a"])
    assert_eq(response.status_code, 200, f"member public uploads are kept: {response.response}")


@th.django_unit_test("control: a plain member still uses an existing purpose")
def test_member_existing_purpose(opts):
    _login(opts, MEMBER)
    response = _initiate(opts, group=opts.group_ids["a"], use="existing")
    assert_eq(response.status_code, 200, f"an existing purpose must still work: {response.response}")


# ---------------------------------------------------------------------------
# Step 3: the upload rule before every write
# ---------------------------------------------------------------------------

@th.django_unit_test("a group admin cannot put an inline avatar into another group's storage")
def test_cross_group_inline_avatar_refused(opts):
    _reset_b(opts)
    _login(opts, GADMIN)
    with _system_default(opts):
        response = opts.client.post(f"/api/group/{opts.group_ids['a']}",
                                    {"group": opts.group_ids["b"], "avatar": INLINE})
        managers, files = _managers(opts, "b"), _files(opts, "b")
    assert_eq(response.status_code, 403, f"a cross-group inline write must be refused: {response.response}")
    assert_eq(managers, 0, "no storage may be created in B")
    assert_eq(files, 0, "no File may be written in B")


@th.django_unit_test("control: a group admin still sets their group's avatar inline")
def test_own_group_inline_avatar(opts):
    _login(opts, GADMIN)
    response = opts.client.post(f"/api/group/{opts.group_ids['a']}",
                                {"group": opts.group_ids["a"], "avatar": INLINE})
    assert_eq(response.status_code, 200, f"an own-group inline avatar must work: {response.response}")


@th.django_unit_test("control: a global group admin still sets any group's avatar inline")
def test_global_group_admin_inline_avatar(opts):
    _login(opts, GLOBAL_GADMIN)
    response = opts.client.post(f"/api/group/{opts.group_ids['a']}",
                                {"group": opts.group_ids["a"], "avatar": INLINE})
    assert_eq(response.status_code, 200, f"a global group admin keeps inline avatars: {response.response}")


@th.django_unit_test("control: a key without files still saves inline media into its group")
def test_key_inline_into_own_group(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.fileman.models import File, FileManager

    key = ApiKey.objects.get(pk=opts.key_c_id)
    FileManager.objects.filter(group_id=opts.group_ids["c"]).delete()
    request = _request(opts, group="c", api_key=key)
    with _system_default(opts):
        created = File._inline_rest_file(object(), "media", INLINE, request)
    assert_eq(created.group_id, opts.group_ids["c"], "the key's inline File must land in its group")


def _inline_media(opts, username):
    """Inline incident-style `media` into A, as the related-field path does."""
    from mojo.apps.fileman.models import File

    request = _request(opts, user=_user(opts, username), group="a")
    return File._inline_rest_file(object(), "media", INLINE, request)


@th.django_unit_test("a non-member with only a global security grant cannot save inline media into a group")
def test_security_only_inline_media_refused(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import File

    before = File.objects.filter(group_id=opts.group_ids["a"], user_id=opts.user_ids[SECURITY]).count()
    refused = False
    try:
        _inline_media(opts, SECURITY)
    except me.PermissionDeniedException:
        refused = True
    after = File.objects.filter(group_id=opts.group_ids["a"], user_id=opts.user_ids[SECURITY]).count()
    assert_true(refused, "a global security grant alone must not write inline media into A")
    assert_eq(after, before, "the refusal must create no File")


@th.django_unit_test("control: a non-member with a global files grant still saves inline media into a group")
def test_files_admin_inline_media(opts):
    created = _inline_media(opts, FILES_ADMIN)
    assert_eq(created.group_id, opts.group_ids["a"], "the files holder's inline File must land in A")


@th.django_unit_test("a File owner who left the group cannot replace the File's bytes")
def test_left_owner_cannot_replace_bytes(opts):
    _login(opts, LEFT)
    before, before_bytes = _stored_bytes(opts.left_file)
    response = opts.client.post(f"/api/fileman/file/{opts.left_file}", data={},
                                files={"file": ("swap.txt", b"REPLACED", "text/plain")})
    after, after_bytes = _stored_bytes(opts.left_file)
    assert_eq(response.status_code, 403, f"a former member must be refused: {response.response}")
    assert_eq(after.storage_filename, before.storage_filename, "the File must not move")
    assert_eq(after_bytes, ORIGINAL_BYTES, "the stored bytes must be unchanged")


@th.django_unit_test("control: a File owner still in the group replaces the File's bytes")
def test_member_owner_replaces_bytes(opts):
    _login(opts, MEMBER)
    response = opts.client.post(f"/api/fileman/file/{opts.member_file}", data={},
                                files={"file": ("swap.txt", b"REPLACED", "text/plain")})
    _, stored = _stored_bytes(opts.member_file)
    assert_eq(response.status_code, 200, f"a member owner must still replace bytes: {response.response}")
    assert_eq(stored, b"REPLACED", "the member's new bytes must be stored")


@th.django_unit_test("create_from_file with a non-member's request writes nothing")
def test_create_from_file_request_checked(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import File

    _reset_b(opts)
    handle = io.BytesIO(b"x")
    handle.name = "x.txt"
    handle.size = 1
    handle.content_type = "text/plain"
    request = _request(opts, user=_user(opts, OUTSIDER), group="b")
    refused = False
    with _system_default(opts):
        try:
            File.create_from_file(handle, "x", request=request)
        except me.PermissionDeniedException:
            refused = True
        managers, files = _managers(opts, "b"), _files(opts, "b")
    assert_true(refused, "a non-member's request must be refused")
    assert_eq(files, 0, "no File may be written in B")
    assert_eq(managers, 0, "no storage may be created in B")


@th.django_unit_test("control: server code still creates the vault's storage, inheriting public")
def test_server_side_vault_storage_unchanged(opts):
    from mojo.apps.fileman.models import FileManager

    _drop_purpose(opts, "a", "filevault")
    with _system_default(opts, is_public=True):
        manager = FileManager.get_for_group(_group(opts, "a"), use="filevault")
        created, public = manager is not None, bool(manager and manager.is_public)
    _drop_purpose(opts, "a", "filevault")
    assert_true(created, "server-side purpose storage must still be created")
    assert_true(public, "new purpose storage still inherits the default's is_public")
