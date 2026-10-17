"""A manager on borrowed keys stays inside its group's storage folder (#7990).

A member with the group-level `files` grant can create and edit file
managers. A manager with no S3 key of its own runs on the platform's keys,
and one with a parent runs on the top parent's keys and settings. Before this
change nothing checked where such a manager pointed, so a member could aim one
at another group's folder, write there and make it public.

The rule: unless the caller holds the platform-wide file grant, a manager on
borrowed keys must point inside one of its group's anchors (a group manager
whose parent is system-scoped, the shape `get_for_group` provisions). A
manager on its own S3 key may point anywhere its key reaches. People, API keys
and group tokens holding the same grant get the same answer.

Test-owned data only: groups `fm_scope_a` and `fm_scope_b`, two non-default
system-scoped managers (filesystem under a temporary folder; S3 at a closed
local endpoint, never reached by a refused save) and their anchors. No patch
of production code; the system default manager is never touched.
"""

TESTIT_TIER = "core"

import os
import tempfile

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PREFIX = "fm_scope_"
PASSWORD = "fm-scope-pass-99!"
MEMBER = "fm_scope_member"    # `files` in A; plain member with `view_fileman` in B
STAFF = "fm_scope_staff"      # `manage_files` on the user record
SUPER = "fm_scope_super"
USERS = [MEMBER, STAFF, SUPER]

PLATFORM_KEY = "AKIAFMSCOPEPLATFORM"
PLATFORM_SECRET = "fm-scope-platform-secret-value"
B_OWN_KEY = "AKIAFMSCOPEBOWNKEY"
B_OWN_SECRET = "fm-scope-b-own-secret"
# Connection refused at once: a save that got as far as S3 fails fast.
CLOSED_ENDPOINT = "http://127.0.0.1:9"
BUCKET = "fm-scope-bucket"


def _clear():
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.fileman.models import File, FileManager

    File.objects.filter(file_manager__name__startswith=PREFIX).delete()
    File.objects.filter(filename__startswith=PREFIX).delete()
    # Children first: parent is CASCADE, but keep the order explicit.
    FileManager.objects.filter(name__startswith=f"Clone of {PREFIX}").delete()
    FileManager.objects.filter(group__name__startswith=PREFIX).delete()
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__in=USERS).delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_manager_storage_scope(opts):
    from mojo.apps.account.models import ApiKey, Group, User
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

    users = {name: make_user(name) for name in USERS}
    users[STAFF].add_permission("manage_files")
    users[STAFF].save()
    users[SUPER].is_superuser = True
    users[SUPER].save()

    group_a = Group(name=f"{PREFIX}a")
    group_a.save()
    group_b = Group(name=f"{PREFIX}b")
    group_b.save()
    member = group_a.add_member(users[MEMBER])
    member.add_permission("files")
    member.save()
    member_b = group_b.add_member(users[MEMBER])
    member_b.add_permission("view_fileman")
    member_b.save()

    opts.tmpdir = tempfile.mkdtemp(prefix=PREFIX)
    opts.uuid_a = group_a.get_uuid()
    opts.uuid_b = group_b.get_uuid()

    def make(name, backend_url, secrets=None, **fields):
        manager = FileManager(
            name=f"{PREFIX}{name}", backend_url=backend_url,
            backend_type=backend_url.split(":")[0], is_active=True,
            is_public=False, **fields)
        if secrets:
            manager.set_secrets(secrets)
        manager.save()
        return manager

    platform = {"aws_key": PLATFORM_KEY, "aws_secret": PLATFORM_SECRET}
    sys_fs = make("sys_fs", "file:///fm_scope",
                  {**platform, "base_path": opts.tmpdir, "use_shortlinks": False})
    sys_s3 = make("sys_s3", f"s3://{BUCKET}/tenants",
                  {**platform, "endpoint_url": CLOSED_ENDPOINT})

    # Anchors, shaped as get_for_group provisions them: the system parent and
    # a copy of its secrets.
    def anchor(name, system, group, uuid):
        return make(name, f"{system.backend_url}/{uuid}", dict(system.secrets),
                    group=group, parent=system)

    opts.anchor_a_fs = anchor("anchor_a_fs", sys_fs, group_a, opts.uuid_a)
    opts.anchor_b_fs = anchor("anchor_b_fs", sys_fs, group_b, opts.uuid_b)
    opts.anchor_a_s3 = anchor("anchor_a_s3", sys_s3, group_a, opts.uuid_a)
    opts.anchor_b_s3 = anchor("anchor_b_s3", sys_s3, group_b, opts.uuid_b)
    opts.b_own = make("b_own", "s3://fm-scope-b-own/files",
                      {"aws_key": B_OWN_KEY, "aws_secret": B_OWN_SECRET,
                       "endpoint_url": CLOSED_ENDPOINT}, group=group_b)

    opts.a_root = f"file:///fm_scope/{opts.uuid_a}"
    opts.b_root = f"file:///fm_scope/{opts.uuid_b}"
    opts.b_folder = os.path.join(opts.tmpdir, "fm_scope", opts.uuid_b)
    os.makedirs(opts.b_folder, exist_ok=True)
    with open(os.path.join(opts.b_folder, "b.txt"), "wb") as handle:
        handle.write(b"B")

    opts.group_a = group_a.id
    opts.group_b = group_b.id
    _, key_a = ApiKey.create_for_group(group_a, f"{PREFIX}ka", permissions={"files": True})
    opts.ka = {"Authorization": f"apikey {key_a}"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _login(opts, username):
    opts.client.logout()
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _as(opts, who):
    """`who` is a username (a session) or "key" (A's reference-mode key)."""
    if who == "key":
        opts.client.logout()
        return opts.ka
    _login(opts, who)
    return None


def _create(opts, name, body, headers=None):
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name=f"{PREFIX}{name}").delete()
    payload = {"name": f"{PREFIX}{name}", "group": opts.group_a, "is_public": False}
    payload.update(body)
    return opts.client.post("/api/fileman/manager", payload, headers=headers)


def _row(name):
    from mojo.apps.fileman.models import FileManager

    return FileManager.objects.filter(name=f"{PREFIX}{name}").first()


def _snapshot(pk):
    from mojo.apps.fileman.models import FileManager

    fm = FileManager.objects.get(pk=pk)
    return (fm.backend_type, fm.backend_url, fm.parent_id, fm.group_id, fm.is_public,
            dict(fm.secrets))


def _orm_manager(opts, name, backend_url, parent=None, group_id=None, secrets=None):
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name=f"{PREFIX}{name}").delete()
    fm = FileManager(name=f"{PREFIX}{name}", backend_url=backend_url,
                     backend_type=backend_url.split(":")[0], is_active=True,
                     is_public=False, parent=parent, group_id=group_id or opts.group_a)
    if secrets:
        fm.set_secrets(secrets)
    fm.save()
    return fm


def _b_intact(opts):
    names = sorted(os.listdir(opts.b_folder))
    return names == ["b.txt"]


def _refused_create(opts, name, body, who):
    headers = _as(opts, who)
    resp = _create(opts, name, body, headers)
    created = _row(name)
    if created is not None:
        created.delete()
    assert_eq(resp.status_code, 403,
              f"{who}: {name} must be refused, got HTTP {resp.status_code}: {resp.response}")
    assert_true(created is None, f"{who}: a refused create ({name}) must leave no row")
    return resp


# ---------------------------------------------------------------------------
# Red first: refused on borrowed keys outside the group's folder.
# ---------------------------------------------------------------------------

def _case_fs_into_b(opts, who):
    _refused_create(opts, f"r1_{who}", {
        "backend_type": "file", "backend_url": opts.b_root,
        "parent": opts.anchor_a_fs.id}, who)
    assert_true(_b_intact(opts), f"{who}: B's folder must be unchanged")


@th.django_unit_test("1. member creates a filesystem manager at B's folder on A's parent: refused")
def test_member_create_fs_into_b(opts):
    _case_fs_into_b(opts, MEMBER)


@th.django_unit_test("2. member creates a public S3 manager at B's prefix on platform keys: refused")
def test_member_create_s3_public_into_b(opts):
    _refused_create(opts, "r2", {
        "backend_type": "s3", "backend_url": f"s3://{BUCKET}/tenants/{opts.uuid_b}",
        "parent": opts.anchor_a_s3.id, "is_public": True}, MEMBER)


def _case_edit_anchor_into_b(opts, who):
    from mojo.apps.fileman.models import FileManager

    # A second, disposable anchor of A, so a wrong answer cannot widen A's
    # folder for the other tests.
    fm = _orm_manager(opts, f"r3_anchor_{who}", f"{opts.a_root}/r3",
                      parent=FileManager.objects.get(name=f"{PREFIX}sys_fs"),
                      secrets=dict(opts.anchor_a_fs.secrets))
    try:
        before = _snapshot(fm.pk)
        headers = _as(opts, who)
        resp = opts.client.post(f"/api/fileman/manager/{fm.pk}",
                                {"backend_url": opts.b_root}, headers=headers)
        assert_eq(resp.status_code, 403,
                  f"{who}: moving A's anchor into B must be refused, got HTTP "
                  f"{resp.status_code}: {resp.response}")
        assert_eq(_snapshot(fm.pk), before, f"{who}: the refused edit must store nothing")
    finally:
        FileManager.objects.filter(pk=fm.pk).delete()


@th.django_unit_test("3. member moves an anchor of A into B's folder: refused, row unchanged")
def test_member_edit_anchor_into_b(opts):
    _case_edit_anchor_into_b(opts, MEMBER)


@th.django_unit_test("4. member sets base_path on a parentless filesystem manager: refused")
def test_member_sets_base_path(opts):
    fm = _orm_manager(opts, "r4", f"{opts.a_root}/r4",
                      secrets={"base_path": opts.tmpdir})
    before = _snapshot(fm.pk)
    _login(opts, MEMBER)
    other = tempfile.mkdtemp(prefix=f"{PREFIX}other_")
    resp = opts.client.post(f"/api/fileman/manager/{fm.pk}",
                            {"settings": {"base_path": other}})
    assert_eq(resp.status_code, 403,
              f"a new base_path outside A's folder must be refused, got HTTP "
              f"{resp.status_code}: {resp.response}")
    assert_eq(_snapshot(fm.pk), before, "the refused edit must store nothing")


@th.django_unit_test("5. member climbs out of A's folder with '..': refused")
def test_member_dotdot(opts):
    _refused_create(opts, "r5", {
        "backend_type": "file", "backend_url": f"{opts.a_root}/../{opts.uuid_b}",
        "parent": opts.anchor_a_fs.id}, MEMBER)


@th.django_unit_test("6. member attaches B's own-key manager as parent: refused")
def test_member_parent_in_other_group(opts):
    _refused_create(opts, "r6", {
        "backend_type": "s3", "backend_url": "s3://fm-scope-b-own/files/r6",
        "parent": opts.b_own.id}, MEMBER)


def _case_group_b(opts, who):
    _refused_create(opts, f"r7_{who}", {
        "group": opts.group_b, "backend_type": "file",
        "backend_url": f"{opts.b_root}/r7", "parent": opts.anchor_b_fs.id}, who)


@th.django_unit_test("7. member creates in B, where they are only a plain member: refused")
def test_member_create_in_group_b(opts):
    _case_group_b(opts, MEMBER)


@th.django_unit_test("8. member makes an existing out-of-folder row public: refused")
def test_member_makes_outside_row_public(opts):
    fm = _orm_manager(opts, "r8", f"{opts.b_root}/r8", parent=opts.anchor_a_fs)
    before = _snapshot(fm.pk)
    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{fm.pk}", {"is_public": True})
    assert_eq(resp.status_code, 403,
              f"making an out-of-folder row public must be refused, got HTTP "
              f"{resp.status_code}: {resp.response}")
    assert_eq(_snapshot(fm.pk), before, "the refused edit must store nothing")


@th.django_unit_test("9. A's key with `files` gets the member's answers for 1, 3 and 7")
def test_key_same_answers(opts):
    _case_fs_into_b(opts, "key")
    _case_edit_anchor_into_b(opts, "key")
    _case_group_b(opts, "key")


# ---------------------------------------------------------------------------
# Credentials: reported by kind, never by value.
# ---------------------------------------------------------------------------

def _mask(value):
    return "*" * (len(value) - 4) + value[-4:]


@th.django_unit_test("credentials: a member-made manager in A's folder reads 'platform'; no secret leaks")
def test_credentials_field(opts):
    _login(opts, MEMBER)
    resp = _create(opts, "cred", {"backend_type": "file",
                                  "backend_url": f"{opts.a_root}/cred",
                                  "parent": opts.anchor_a_fs.id})
    assert_eq(resp.status_code, 200, f"the in-folder create must pass: {resp.response}")
    created = _row("cred")
    read = opts.client.get(f"/api/fileman/manager/{created.id}")
    assert_eq(read.status_code, 200, f"the member must read it: {read.response}")
    data = read.response.data
    assert_eq(data.get("credentials"), "platform",
              f"a manager under a system parent runs on platform keys: {data}")

    read = opts.client.get(f"/api/fileman/manager/{opts.anchor_a_fs.id}")
    data = read.response.data
    assert_eq(data.get("credentials"), "platform", f"the anchor runs on platform keys: {data}")
    assert_eq(data.get("aws_secret_masked"), _mask(PLATFORM_SECRET), "the mask is unchanged")
    assert_true(PLATFORM_SECRET not in str(read.response),
                "no response may carry the platform secret")

    _login(opts, SUPER)
    read = opts.client.get(f"/api/fileman/manager/{opts.b_own.id}")
    assert_eq(read.response.data.get("credentials"), "own",
              f"a manager on its own S3 key reads 'own': {read.response.data}")
    child = _orm_manager(opts, "cred_child", "s3://fm-scope-b-own/files/child",
                         parent=opts.b_own, group_id=opts.group_b)
    read = opts.client.get(f"/api/fileman/manager/{child.id}")
    assert_eq(read.response.data.get("credentials"), "parent",
              f"a child of an own-key manager reads 'parent': {read.response.data}")
    assert_true(B_OWN_SECRET not in str(read.response), "no response may carry a secret")
    listed = opts.client.get("/api/fileman/manager", params={"group": opts.group_b})
    assert_true("credentials" not in str(listed.response.data[0] if listed.response.data else {}),
                "credentials is on the default graph only, never the list graph")


# ---------------------------------------------------------------------------
# Positive controls: pass before and after.
# ---------------------------------------------------------------------------

@th.django_unit_test("control: member creates a manager inside A's folder and uploads to it")
def test_member_inside_own_folder(opts):
    _login(opts, MEMBER)
    resp = _create(opts, "reports", {"backend_type": "file",
                                     "backend_url": f"{opts.a_root}/reports",
                                     "parent": opts.anchor_a_fs.id})
    assert_eq(resp.status_code, 200, f"an in-folder create must pass: {resp.response}")
    created = _row("reports")
    upload = opts.client.post(
        "/api/fileman/file",
        data={"file_manager": created.id, "group": opts.group_a,
              "filename": f"{PREFIX}report.txt"},
        files={"file": (f"{PREFIX}report.txt", b"data", "text/plain")})
    assert_eq(upload.status_code, 200, f"the member must upload there: {upload.response}")
    from mojo.apps.fileman.models import File
    row = File.objects.filter(file_manager=created).first()
    assert_true(row is not None and row.storage_file_path.startswith(f"fm_scope/{opts.uuid_a}/reports/"),
                f"the file must land in A's folder: {row and row.storage_file_path}")


@th.django_unit_test("control: member renames an out-of-folder row")
def test_member_renames_outside_row(opts):
    fm = _orm_manager(opts, "rename", f"{opts.b_root}/rename", parent=opts.anchor_a_fs)
    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{fm.pk}",
                            {"description": "renamed by the member"})
    assert_eq(resp.status_code, 200, f"an untouched location keeps working: {resp.response}")


@th.django_unit_test("control: member on its own S3 key may point anywhere it reaches")
def test_member_own_key_anywhere(opts):
    fm = _orm_manager(opts, "own_key", f"s3://{BUCKET}/tenants/{opts.uuid_a}/own",
                      secrets={"aws_key": "AKIAFMSCOPEAOWN", "aws_secret": "a-own-secret",
                               "endpoint_url": CLOSED_ENDPOINT})
    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{fm.pk}",
                            {"backend_url": "s3://elsewhere-bucket/a"})
    assert_eq(resp.status_code, 200, f"an own-key manager is the group's business: {resp.response}")


@th.django_unit_test("control: staff and superuser create managers outside A's folder")
def test_staff_and_superuser(opts):
    for who in (STAFF, SUPER):
        _login(opts, who)
        resp = _create(opts, f"outside_{who}", {"backend_type": "file",
                                               "backend_url": f"{opts.b_root}/{who}",
                                               "parent": opts.anchor_a_fs.id})
        assert_eq(resp.status_code, 200, f"{who} holds the platform grant: {resp.response}")


@th.django_unit_test("control: get_for_group still provisions for a fresh group")
def test_get_for_group_provisions(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import FileManager

    fresh = Group(name=f"{PREFIX}fresh")
    fresh.save()
    default = FileManager.objects.filter(user=None, group=None, is_default=True,
                                         is_active=True).first()
    got = FileManager.get_for_group(group=fresh)
    if default is None:
        assert_true(got is None, "with no system default nothing is provisioned, as before")
    else:
        assert_true(got is not None and got.parent_id == default.id,
                    f"a fresh group gets a manager under the system default: {got}")


@th.django_unit_test("control: A's key creates a File on A's anchor")
def test_key_file_create_on_anchor(opts):
    opts.client.logout()
    resp = opts.client.post(
        "/api/fileman/file",
        data={"file_manager": opts.anchor_a_fs.id, "filename": f"{PREFIX}key.txt"},
        files={"file": (f"{PREFIX}key.txt", b"data", "text/plain")}, headers=opts.ka)
    assert_eq(resp.status_code, 200, f"the key's file create must pass: {resp.response}")


@th.django_unit_test("control: member clones A's anchor; the clone keeps its location")
def test_member_clone_keeps_location(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{opts.anchor_a_fs.id}", {"clone": 1})
    assert_eq(resp.status_code, 200, f"the clone must pass: {resp.response}")
    clone = FileManager.objects.filter(name=f"Clone of {PREFIX}anchor_a_fs").first()
    assert_true(clone is not None, "the clone must exist")
    assert_eq((clone.backend_url, clone.group_id),
              (opts.anchor_a_fs.backend_url, opts.group_a), "the clone keeps the location")


# ---------------------------------------------------------------------------
# Review 113553 corrections (Rick's decision 113556).
# C1: the own-key parent exemption covers S3 children only.
# C2: a save may not give an existing descendant new reach.
# C3: a changed temp_upload_path stays inside the group's folder.
# ---------------------------------------------------------------------------

A_OWN = {"aws_key": "AKIAFMSCOPEAOWN", "aws_secret": "a-own-secret",
         "endpoint_url": CLOSED_ENDPOINT}


def _own_parent(opts, name, backend_url, **settings):
    return _orm_manager(opts, name, backend_url, secrets={**A_OWN, **settings})


def _case_fs_child_of_own_key(opts, who):
    """R1: a file:// child of an own-key S3 parent never uses that key."""
    from mojo.apps.fileman.models import File

    parent = _own_parent(opts, f"r1p_{who}", f"s3://{BUCKET}/tenants/{opts.uuid_a}/p",
                         base_path=opts.tmpdir)
    headers = _as(opts, who)
    resp = _create(opts, f"r1_{who}", {"backend_type": "file", "backend_url": opts.b_root,
                                      "parent": parent.id}, headers)
    child = _row(f"r1_{who}")
    if child is not None:
        opts.client.post(
            "/api/fileman/file",
            data={"file_manager": child.id, "group": opts.group_a,
                  "filename": f"{PREFIX}r1.txt"},
            files={"file": (f"{PREFIX}r1.txt", b"R1", "text/plain")}, headers=headers)
        intact = _b_intact(opts)
        File.objects.filter(file_manager=child).delete()
        for name in os.listdir(opts.b_folder):
            if name != "b.txt":
                path = os.path.join(opts.b_folder, name)
                if os.path.isfile(path):
                    os.remove(path)
        child.delete()
    else:
        intact = _b_intact(opts)
    assert_eq(resp.status_code, 403,
              f"{who}: a file:// child of an own-key S3 parent at B's folder must be "
              f"refused, got HTTP {resp.status_code}: {resp.response}")
    assert_true(child is None, f"{who}: a refused create must leave no row")
    assert_true(intact, f"{who}: B's folder must be unchanged")


@th.django_unit_test("R1. member creates a file:// child of an own-key S3 parent at B's folder: refused")
def test_r1_fs_child_of_own_key(opts):
    _case_fs_child_of_own_key(opts, MEMBER)


@th.django_unit_test("R1b. member moves an own-key parent's base_path so its file:// child lands in B: refused")
def test_r1b_parent_base_path_moves_child(opts):
    parent = _own_parent(opts, "r1bp", f"s3://{BUCKET}/tenants/{opts.uuid_a}/p",
                         base_path=opts.tmpdir)
    _orm_manager(opts, "r1b_child", f"{opts.a_root}/r1b", parent=parent)
    before = _snapshot(parent.pk)
    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{parent.pk}",
                            {"settings": {"base_path": opts.b_folder}})
    assert_eq(resp.status_code, 403,
              f"moving the child into B through its parent must be refused, got HTTP "
              f"{resp.status_code}: {resp.response}")
    assert_eq(_snapshot(parent.pk), before, "the refused edit must store nothing")


def _s3_parent_with_child_in_b(opts, name, parent_url, **settings):
    parent = _own_parent(opts, f"{name}p", parent_url, **settings)
    child = _orm_manager(opts, f"{name}_child", f"s3://{BUCKET}/tenants/{opts.uuid_b}/c",
                         parent=parent)
    assert_eq(child.credentials, "parent", "the child starts on its parent's own key")
    return parent, child


def _refused_parent_edit(opts, who, parent, child, body, label):
    from mojo.apps.fileman.models import FileManager

    before = _snapshot(parent.pk)
    headers = _as(opts, who)
    resp = opts.client.post(f"/api/fileman/manager/{parent.pk}", body, headers=headers)
    assert_eq(resp.status_code, 403,
              f"{who}: {label} must be refused, got HTTP {resp.status_code}: {resp.response}")
    assert_eq(_snapshot(parent.pk), before, f"{who}: the refused edit must store nothing")
    assert_eq(FileManager.objects.get(pk=child.pk).credentials, "parent",
              f"{who}: the child must still run on the parent's own key")


def _case_reparent_onto_platform(opts, who):
    """R2: Brenda's sequence: re-parenting P puts its child on platform keys in B."""
    parent, child = _s3_parent_with_child_in_b(
        opts, f"r2_{who}", f"s3://{BUCKET}/tenants/{opts.uuid_a}/p")
    _refused_parent_edit(opts, who, parent, child, {
        "parent": opts.anchor_a_s3.id,
        "backend_url": f"s3://{BUCKET}/tenants/{opts.uuid_a}/converted"},
        "re-parenting onto A's platform anchor")


@th.django_unit_test("R2. member re-parents an own-key parent onto A's anchor; its child in B goes platform: refused")
def test_r2_reparent_onto_platform(opts):
    _case_reparent_onto_platform(opts, MEMBER)


@th.django_unit_test("R2b. member clears an own-key parent's keys; its child in B goes platform: refused")
def test_r2b_clear_parent_keys(opts):
    parent, child = _s3_parent_with_child_in_b(
        opts, "r2b", f"s3://{BUCKET}/tenants/{opts.uuid_a}/p")
    _refused_parent_edit(opts, MEMBER, parent, child,
                         {"settings": {"aws_key": "", "aws_secret": ""}}, "clearing the keys")


@th.django_unit_test("R2c. member turns an own-key parent into file://; its child in B goes platform: refused")
def test_r2c_parent_to_filesystem(opts):
    # P's own path sits inside A's filesystem folder, so P itself passes.
    parent, child = _s3_parent_with_child_in_b(
        opts, "r2c", f"s3://{BUCKET}/fm_scope/{opts.uuid_a}/p", base_path=opts.tmpdir)
    _refused_parent_edit(opts, MEMBER, parent, child, {"backend_type": "file"},
                         "switching the parent to file://")


def _case_temp_outside(opts, who):
    """R3: the filesystem backend creates and prunes folders at temp_upload_path."""
    fm = _orm_manager(opts, f"r3t_{who}", f"{opts.a_root}/r3t",
                      secrets={"base_path": opts.tmpdir})
    before = _snapshot(fm.pk)
    outside = os.path.join(tempfile.mkdtemp(prefix=f"{PREFIX}outside_"), "chosen")
    headers = _as(opts, who)
    resp = opts.client.post(f"/api/fileman/manager/{fm.pk}",
                            {"settings": {"temp_upload_path": outside}}, headers=headers)
    assert_eq(resp.status_code, 403,
              f"{who}: temp_upload_path outside A's folder must be refused, got HTTP "
              f"{resp.status_code}: {resp.response}")
    assert_eq(_snapshot(fm.pk), before, f"{who}: the refused edit must store nothing")


@th.django_unit_test("R3. member sets temp_upload_path outside A's folder: refused")
def test_r3_temp_upload_path_outside(opts):
    _case_temp_outside(opts, MEMBER)


@th.django_unit_test("R9. A's key with `files` gets the member's answers for R1, R2 and R3")
def test_r9_key_same_answers(opts):
    _case_fs_child_of_own_key(opts, "key")
    _case_reparent_onto_platform(opts, "key")
    _case_temp_outside(opts, "key")


@th.django_unit_test("control: member creates an S3 child of an own-key S3 parent at any prefix")
def test_s3_child_of_own_key_anywhere(opts):
    parent = _own_parent(opts, "own_s3p", f"s3://{BUCKET}/tenants/{opts.uuid_a}/p")
    _login(opts, MEMBER)
    resp = _create(opts, "own_s3_child", {"backend_type": "s3",
                                          "backend_url": f"s3://{BUCKET}/tenants/{opts.uuid_b}/c",
                                          "parent": parent.id})
    # The closed endpoint may fail the save after the check; only the
    # containment refusal is wrong here.
    assert_true(resp.status_code != 403,
                f"an S3 child on its parent's own key is not contained, got HTTP "
                f"{resp.status_code}: {resp.response}")


@th.django_unit_test("control: member renames an own-key parent whose child points into B")
def test_parent_rename_with_child_in_b(opts):
    parent, _ = _s3_parent_with_child_in_b(opts, "rename_p", f"s3://{BUCKET}/tenants/{opts.uuid_a}/p")
    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{parent.pk}", {"name": f"{PREFIX}rename_p2"})
    assert_eq(resp.status_code, 200, f"a rename changes no reach: {resp.response}")


@th.django_unit_test("control: member swaps an own-key parent's key pair; its child stays on the parent")
def test_parent_key_swap(opts):
    from mojo.apps.fileman.models import FileManager

    parent, child = _s3_parent_with_child_in_b(opts, "swap", f"s3://{BUCKET}/tenants/{opts.uuid_a}/p")
    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{parent.pk}",
                            {"settings": {"aws_key": "AKIAFMSCOPEASWAP",
                                          "aws_secret": "a-swap-secret"}})
    assert_eq(resp.status_code, 200, f"another own key leaves the child's reach alone: {resp.response}")
    assert_eq(FileManager.objects.get(pk=child.pk).credentials, "parent",
              "the child still runs on its parent's own key")


@th.django_unit_test("control: member sets temp_upload_path inside A's folder")
def test_temp_upload_path_inside(opts):
    fm = _orm_manager(opts, "temp_in", f"{opts.a_root}/temp_in",
                      secrets={"base_path": opts.tmpdir})
    inside = os.path.join(opts.tmpdir, "fm_scope", opts.uuid_a, "uploads")
    _login(opts, MEMBER)
    resp = opts.client.post(f"/api/fileman/manager/{fm.pk}",
                            {"settings": {"temp_upload_path": inside}})
    assert_eq(resp.status_code, 200, f"a temp folder inside A's folder is fine: {resp.response}")
