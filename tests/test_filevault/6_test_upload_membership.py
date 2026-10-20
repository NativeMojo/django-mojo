"""Vault uploads and stores need membership of the group (#8661, D2).

`POST /api/filevault/file/upload` and `POST /api/filevault/data/store` had only
`requires_auth`, then used `request.group` — any active group a signed-in user
names. An outsider could create another group's vault storage, save a
VaultFile or VaultData there, and as its owner read it back.

Now a person must be an active member of the group (parents walked) or hold a
global vault or files grant. Members keep uploading as before.

Test-owned data only (prefix `fv_member_`), and a filesystem system default
with a dedicated negative id, created and removed inside the tests that need
one (the device of test_fileman/12_test_public_access_reconciliation.py).
"""

TESTIT_TIER = "framework"

import tempfile
from contextlib import contextmanager

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PREFIX = "fv_member_"
PASSWORD = "fv-member-pass-99!"
SYSTEM_DEFAULT_PK = -8662
MEMBER = "fv_member_member"      # member of B
OUTSIDER = "fv_member_outsider"  # member of nothing
USERS = [MEMBER, OUTSIDER]


def _clear():
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.filevault.models import VaultData, VaultFile

    FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    VaultFile.objects.filter(group__name__startswith=PREFIX).delete()
    VaultData.objects.filter(group__name__startswith=PREFIX).delete()
    FileManager.objects.filter(group__name__startswith=PREFIX).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__in=USERS).delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.filevault")
def setup_upload_membership(opts):
    from mojo.apps.account.models import Group, User
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
    group_b = Group(name=f"{PREFIX}b")
    group_b.save()
    group_b.add_member(users[MEMBER])
    opts.group_b = group_b.id
    opts.tmpdir = tempfile.mkdtemp(prefix=PREFIX)


@contextmanager
def _system_default(opts):
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    system = FileManager(pk=SYSTEM_DEFAULT_PK, name=f"{PREFIX}system", backend_type="file",
                         backend_url="file://", is_active=True, is_default=True)
    system.save()
    system.set_secrets({"base_path": opts.tmpdir, "use_shortlinks": False})
    system.save()
    try:
        yield system
    finally:
        FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
        system.delete()


def _reset_b(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.filevault.models import VaultData, VaultFile

    VaultFile.objects.filter(group_id=opts.group_b).delete()
    VaultData.objects.filter(group_id=opts.group_b).delete()
    FileManager.objects.filter(group_id=opts.group_b).delete()


def _login(opts, username):
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _upload(opts):
    return opts.client.post("/api/filevault/file/upload",
                            data={"group": opts.group_b, "name": "secret.txt"},
                            files={"file": ("secret.txt", b"vault secret", "text/plain")})


@th.django_unit_test("a non-member cannot upload into another group's vault")
def test_outsider_upload_refused(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.filevault.models import VaultFile

    _reset_b(opts)
    _login(opts, OUTSIDER)
    with _system_default(opts):
        response = _upload(opts)
        files = VaultFile.objects.filter(group_id=opts.group_b).count()
        managers = FileManager.objects.filter(group_id=opts.group_b).count()
    assert_eq(response.status_code, 403, f"a non-member upload must be refused: {response.response}")
    assert_eq(files, 0, "no VaultFile may be saved in B")
    assert_eq(managers, 0, "no vault storage may be created in B")


@th.django_unit_test("a non-member cannot store vault data in another group")
def test_outsider_store_refused(opts):
    from mojo.apps.filevault.models import VaultData

    _reset_b(opts)
    _login(opts, OUTSIDER)
    response = opts.client.post("/api/filevault/data/store",
                                {"group": opts.group_b, "name": "secret", "data": {"k": "v"}})
    stored = VaultData.objects.filter(group_id=opts.group_b).count()
    assert_eq(response.status_code, 403, f"a non-member store must be refused: {response.response}")
    assert_eq(stored, 0, "no VaultData may be saved in B")


@th.django_unit_test("control: a member still uploads into their group's vault")
def test_member_upload(opts):
    from mojo.apps.filevault.models import VaultFile

    _reset_b(opts)
    _login(opts, MEMBER)
    with _system_default(opts):
        response = _upload(opts)
        files = VaultFile.objects.filter(group_id=opts.group_b).count()
    _reset_b(opts)
    assert_eq(response.status_code, 200, f"a member upload must work: {response.response}")
    assert_eq(files, 1, "the member's VaultFile must be saved")
