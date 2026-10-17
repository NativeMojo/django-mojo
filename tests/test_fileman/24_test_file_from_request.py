"""A storage id named in a request is honoured only for a caller who may use it (#7980).

`FileManager.get_from_request` backs `File.create_from_file(request=...)` and
`File.on_rest_save_file`. It used to return any manager whose id the body
named, with no check, so a custom upload endpoint built the documented way let
a signed-in caller write into another group's storage.

Now a named id is checked: a person needs what upload initiation requires
(membership of the manager's group, or the global file grant); an API key or
group token may name storage in the groups it already covers. A request that
names no storage is unchanged.

Test-owned data only (prefix `file_from_req_`), filesystem managers under a
temporary folder, mock requests carrying real credential objects. No patch of
shared code.
"""

TESTIT_TIER = "core"

import tempfile

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


PREFIX = "file_from_req_"
PASSWORD = "file-from-req-pass-99!"
MEMBER = "file_from_req_member"      # plain member of A
OUTSIDER = "file_from_req_outsider"  # member of A only, never of B
ADMIN = "file_from_req_admin"        # global manage_files
USERS = [MEMBER, OUTSIDER, ADMIN]
ALIASES = ["fileman", "filemanager", "file_manager", "manager", "file_manager_id"]


def _clear():
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.account.models.user_api_key import UserAPIKey
    from mojo.apps.fileman.models import File, FileManager

    File.objects.filter(file_manager__name__startswith=PREFIX).delete()
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    UserAPIKey.objects.filter(user__username__in=USERS).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name=f"{PREFIX}a1").delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__in=USERS).delete()


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_file_from_request(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.account.models.user_api_key import UserAPIKey
    from mojo.apps.fileman.models import FileManager

    _clear()

    def make_user(username):
        user = User(username=username, email=f"{username}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PASSWORD)
        user.save()
        return user

    users = {name: make_user(name) for name in USERS}
    users[ADMIN].add_permission("manage_files")
    users[ADMIN].save()

    group_a = Group(name=f"{PREFIX}a")
    group_a.save()
    group_a1 = Group(name=f"{PREFIX}a1", parent=group_a)
    group_a1.save()
    group_b = Group(name=f"{PREFIX}b")
    group_b.save()
    group_a.add_member(users[MEMBER]).save()
    group_a.add_member(users[OUTSIDER]).save()

    tmpdir = tempfile.mkdtemp(prefix=PREFIX)

    def make_manager(name, **scope):
        manager = FileManager(name=f"{PREFIX}{name}", backend_type="file",
                              backend_url=f"file:///{PREFIX}{name}", is_public=False,
                              **{"is_active": True, **scope})
        manager.set_secrets({"base_path": tmpdir, "use_shortlinks": False})
        manager.save()
        return manager.id

    opts.fm_a = make_manager("a", group=group_a, is_default=True)
    opts.fm_a1 = make_manager("a1", group=group_a1, is_default=True)
    opts.fm_b = make_manager("b", group=group_b, is_default=True)
    opts.fm_personal = make_manager("personal", user=users[MEMBER])
    opts.fm_root = make_manager("root", is_default=False)
    opts.fm_inactive = make_manager("inactive", group=group_a, is_active=False)

    opts.group_ids = {"a": group_a.id, "a1": group_a1.id, "b": group_b.id}
    opts.user_ids = {name: user.id for name, user in users.items()}
    opts.key_ref, _ = ApiKey.create_for_group(group_a, f"{PREFIX}ref")
    opts.key_ref_linked, _ = ApiKey.create_for_group(
        group_a, f"{PREFIX}ref_linked", user=users[MEMBER])
    opts.key_override, _ = ApiKey.create_for_group(
        group_a, f"{PREFIX}override", user=users[MEMBER], override_user=True)
    opts.user_api_key = UserAPIKey.create_for_user(users[MEMBER], expire_days=1, label=PREFIX)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _group(opts, name):
    from mojo.apps.account.models import Group

    return Group.objects.get(pk=opts.group_ids[name]) if name else None


def _user(opts, name):
    from mojo.apps.account.models import User

    return User.objects.get(pk=opts.user_ids[name])


def _request(opts, user=None, group=None, data=None, api_key=None, token=None, user_api_key=None):
    """A request as the dispatcher leaves it: `group` bound, DATA parsed."""
    from django.contrib.auth.models import AnonymousUser
    from objict import objict
    from testit.helpers import get_mock_request

    request = get_mock_request(user=user or AnonymousUser())
    request.user = user if user is not None else AnonymousUser()
    request.group = _group(opts, group)
    request.DATA = objict(data or {})
    request.api_key = api_key
    request.group_token = token
    request.user_api_key = user_api_key
    return request


def _person(opts, name, group=None, data=None, **creds):
    return _request(opts, user=_user(opts, name), group=group, data=data, **creds)


def _key(opts, key, group="a", data=None):
    from mojo.apps.account.models import ApiKey

    key = ApiKey.objects.get(pk=key.pk)
    user = key.user if key.override_user else key
    return _request(opts, user=user, group=group, data=data, api_key=key)


def _token(opts, group="a", data=None):
    from mojo.apps.account.services.group_token import GroupScopedToken

    token = GroupScopedToken(_user(opts, MEMBER), _group(opts, "a"))
    return _request(opts, user=token.user, group=group, data=data, token=token)


def _refused(request, label):
    from mojo import errors as me
    from mojo.apps.fileman.models import FileManager

    try:
        got = FileManager.get_from_request(request)
    except me.PermissionDeniedException:
        return
    except Exception as exc:  # noqa: BLE001 - the old code raised DoesNotExist/ValueError
        assert_true(False, f"{label}: must be a 403, raised {type(exc).__name__}: {exc}")
    assert_true(False, f"{label}: must be refused, got manager {got and got.pk}")


def _allowed(request, manager_id, label):
    from mojo.apps.fileman.models import FileManager

    got = FileManager.get_from_request(request)
    assert_eq(got and got.pk, manager_id, f"{label}: must resolve the named storage")


def _upload(name):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, b"data", content_type="text/plain")


# ---------------------------------------------------------------------------
# Red first: refused.
# ---------------------------------------------------------------------------

@th.django_unit_test("1. a member of A names B's storage by each alias: refused")
def test_member_names_foreign_storage(opts):
    for alias in ALIASES:
        _refused(_person(opts, MEMBER, group="a", data={alias: opts.fm_b, "group": opts.group_ids["a"]}),
                 f"member of A, {alias}=B")


@th.django_unit_test("1b. create_from_file(request=) with B's storage named: refused, nothing stored")
def test_create_from_file_refused(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import File

    request = _person(opts, MEMBER, group="a", data={"file_manager": opts.fm_b})
    err = None
    try:
        File.create_from_file(_upload(f"{PREFIX}1b.txt"), "file", request=request)
    except me.PermissionDeniedException as exc:
        err = exc
    assert_true(err is not None, "naming B's storage through create_from_file must be refused")
    assert_true(not File.objects.filter(file_manager_id=opts.fm_b).exists(),
                "a refused create must leave no record in B's storage")


@th.django_unit_test("2. a non-member of B names B's storage with group=B: refused")
def test_outsider_with_group(opts):
    _refused(_person(opts, OUTSIDER, group="b",
                     data={"file_manager": opts.fm_b, "group": opts.group_ids["b"]}),
             "outsider, group=B")


@th.django_unit_test("3. a non-member of B names B's storage with no group: refused")
def test_outsider_no_group(opts):
    _refused(_person(opts, OUTSIDER, data={"file_manager": opts.fm_b}), "outsider, no group")


@th.django_unit_test("4. anonymous names A's storage: refused")
def test_anonymous(opts):
    _refused(_request(opts, data={"file_manager": opts.fm_a}), "anonymous")


@th.django_unit_test("5. an API key of A names B's storage: refused")
def test_key_names_foreign(opts):
    _refused(_key(opts, opts.key_ref, data={"file_manager": opts.fm_b}), "key of A, B named")
    _refused(_key(opts, opts.key_override, data={"file_manager": opts.fm_b}), "override key of A, B named")


@th.django_unit_test("6. a group token for A names A1's and B's storage: refused")
def test_token_names_outside(opts):
    _refused(_token(opts, data={"file_manager": opts.fm_a1}), "group token, A1 named")
    _refused(_token(opts, data={"file_manager": opts.fm_b}), "group token, B named")


@th.django_unit_test("7. a reference-mode key names its member's personal storage: refused")
def test_reference_key_personal(opts):
    _refused(_key(opts, opts.key_ref_linked, data={"file_manager": opts.fm_personal}),
             "reference-mode key, personal named")


@th.django_unit_test("8. keys and tokens name the root storage: refused")
def test_keys_root(opts):
    for key in (opts.key_ref, opts.key_override):
        _refused(_key(opts, key, data={"file_manager": opts.fm_root}), f"{key.name}, root named")
    _refused(_token(opts, data={"file_manager": opts.fm_root}), "group token, root named")


@th.django_unit_test("9. unknown and malformed ids: 403, not 500")
def test_bad_ids(opts):
    for value in ("99999999", "abc"):
        _refused(_person(opts, MEMBER, group="a", data={"file_manager": value}), f"id {value!r}")


@th.django_unit_test("10. an inactive storage named by a member: refused")
def test_inactive(opts):
    _refused(_person(opts, MEMBER, group="a", data={"file_manager": opts.fm_inactive}),
             "inactive storage")


@th.django_unit_test("11. on_rest_save_file under a request naming B's storage: refused")
def test_on_rest_save_file(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import File
    from mojo.models.rest import ACTIVE_REQUEST

    request = _person(opts, MEMBER, group="a", data={"file_manager": opts.fm_b})
    token = ACTIVE_REQUEST.set(request)
    err = None
    try:
        File(filename=f"{PREFIX}11.txt").on_rest_save_file("file", _upload(f"{PREFIX}11.txt"))
    except me.PermissionDeniedException as exc:
        err = exc
    finally:
        ACTIVE_REQUEST.reset(token)
    assert_true(err is not None, "the on_rest_save_file fallback must be checked too")
    assert_true(not File.objects.filter(file_manager_id=opts.fm_b).exists(),
                "nothing may land in B's storage")


# ---------------------------------------------------------------------------
# Positive controls: pass before and after.
# ---------------------------------------------------------------------------

@th.django_unit_test("control: a member of A names A's storage, with and without group=A")
def test_member_own_storage(opts):
    from mojo.apps.fileman.models import File

    _allowed(_person(opts, MEMBER, group="a",
                     data={"file_manager": opts.fm_a, "group": opts.group_ids["a"]}),
             opts.fm_a, "member, group=A")
    _allowed(_person(opts, MEMBER, data={"file_manager": opts.fm_a}), opts.fm_a, "member, no group")
    row = File.create_from_file(_upload(f"{PREFIX}own.txt"), "file",
                                request=_person(opts, MEMBER, data={"file_manager": opts.fm_a}))
    assert_eq(row.file_manager_id, opts.fm_a, "the file must be stored in A's storage")


@th.django_unit_test("control: the same member through a personal API key")
def test_member_user_api_key(opts):
    _allowed(_person(opts, MEMBER, data={"file_manager": opts.fm_a}, user_api_key=opts.user_api_key),
             opts.fm_a, "member's UserAPIKey")


@th.django_unit_test("control: a global file admin names B's storage with group=B")
def test_admin_foreign(opts):
    _allowed(_person(opts, ADMIN, group="b",
                     data={"file_manager": opts.fm_b, "group": opts.group_ids["b"]}),
             opts.fm_b, "global manage_files")


@th.django_unit_test("control: a key of A with no grants names A's and A1's storage")
def test_key_own_groups(opts):
    _allowed(_key(opts, opts.key_ref, data={"file_manager": opts.fm_a}), opts.fm_a, "key, A")
    _allowed(_key(opts, opts.key_ref, data={"file_manager": opts.fm_a1}), opts.fm_a1, "key, A1")


@th.django_unit_test("control: an override key names A's storage and its member's personal storage")
def test_override_key(opts):
    _allowed(_key(opts, opts.key_override, data={"file_manager": opts.fm_a}), opts.fm_a, "override, A")
    _allowed(_key(opts, opts.key_override, data={"file_manager": opts.fm_personal}),
             opts.fm_personal, "override, personal")


@th.django_unit_test("control: a group token for A names A's storage")
def test_token_own(opts):
    _allowed(_token(opts, data={"file_manager": opts.fm_a}), opts.fm_a, "group token, A")


@th.django_unit_test("unchanged: with no storage named, the same manager as get_for_user_group")
def test_no_id_unchanged(opts):
    from mojo.apps.fileman.models import FileManager

    for label, request in (
            ("person", _person(opts, MEMBER, group="a")),
            ("key without grants", _key(opts, opts.key_ref)),
            ("group token", _token(opts))):
        expected = FileManager.get_for_user_group(user=request.user, group=request.group, use="")
        got = FileManager.get_from_request(request)
        assert_eq(got and got.pk, expected and expected.pk, f"{label}: the no-id path is unchanged")
