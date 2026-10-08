"""Maestro item #7189 — an account's server-set facts cannot be written through REST.

`User.RestMeta.NO_SAVE_FIELDS` used to REPLACE the framework default
`["id", "pk", "created", "uuid"]`, so an ordinary user could post those names
to `/api/user/me` and have them stored, along with `date_joined`,
`last_login`, `onetime_code` and `modified`. `uuid` names the folder of a
user's personal file store and is the passkey user handle; an emptied
`last_login` makes the next password reset mark the email verified.

Each of these is now ignored with a normal 200, like every other no-save name.
"""
import datetime

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

IP = "127.0.0.1"
PREFIX = "usf_"
PWORD = "usf##mojo99Fields"
OLD = "2000-01-01T00:00:00Z"
PLANTED_UUID = "11111111-2222-3333-4444-555555555555"
STORED_CODE = "usf-stored-code"

SERVER_FIELDS = ("id", "uuid", "created", "date_joined", "last_login", "onetime_code", "modified")


def _mk_user(name, perms=None):
    from mojo.apps.account.models import User
    from mojo.helpers import dates

    User.objects.filter(username=name).delete()
    user = User(username=name, display_name=name, email=f"{name}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(PWORD)
    user.remove_all_permissions()
    if perms:
        user.add_permission(perms)
    User.objects.filter(pk=user.pk).update(onetime_code=STORED_CODE, last_login=dates.utcnow())
    return user.pk


@th.django_unit_setup()
def setup_user_server_fields(opts):
    from mojo.apps.account.models import User

    # delete-before-create — tests run against a long-lived DB
    User.objects.filter(username__startswith=PREFIX).delete()


def _login(opts, name):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    assert_true(opts.client.login(name, PWORD), f"{name} must be able to log in")


def _own(opts, tag):
    """A fresh account per test, signed in once.

    One user each: `created` is part of the key that encrypts a row's secrets,
    so on the unfixed code a test that backdates it would leave the account
    broken for the next test. Signing in stamps `last_login`, so it happens
    here, before the test reads the stored values.
    """
    name = f"{PREFIX}{tag}"
    pk = _mk_user(name)
    _login(opts, name)
    return pk


def _stored(pk):
    from mojo.apps.account.models import User
    row = User.objects.filter(pk=pk).values(*[k for k in SERVER_FIELDS]).first()
    assert_true(row is not None, f"user {pk} must still exist")
    return row


def _assert_ignored(opts, path, pk, body, label):
    before = _stored(pk)
    resp = opts.client.post(path, body)
    assert_eq(resp.status_code, 200, f"{label}: an ignored key must not fail the save, got {resp.status_code}")
    after = _stored(pk)
    for key in SERVER_FIELDS:
        assert_eq(after[key], before[key], f"{label}: `{key}` must be unchanged")


@th.django_unit_test("#7189: the owner cannot backdate `created`")
def test_owner_cannot_write_created(opts):
    pk = _own(opts, "created")
    _assert_ignored(opts, "/api/user/me", pk, {"created": OLD}, "created")


@th.django_unit_test("#7189: the owner cannot replace the account `uuid`")
def test_owner_cannot_write_uuid(opts):
    pk = _own(opts, "uuid")
    _assert_ignored(opts, "/api/user/me", pk, {"uuid": PLANTED_UUID}, "uuid")


@th.django_unit_test("#7189: the owner cannot backdate `date_joined`")
def test_owner_cannot_write_date_joined(opts):
    pk = _own(opts, "joined")
    _assert_ignored(opts, "/api/user/me", pk, {"date_joined": OLD}, "date_joined")


@th.django_unit_test("#7189: the owner cannot set or clear `last_login`")
def test_owner_cannot_write_last_login(opts):
    pk = _own(opts, "login")
    assert_true(_stored(pk)["last_login"] is not None,
                "the account must have a last_login to protect")
    _assert_ignored(opts, "/api/user/me", pk, {"last_login": OLD}, "last_login set")
    # An empty last_login makes the next password reset mark the email verified.
    _assert_ignored(opts, "/api/user/me", pk, {"last_login": ""}, "last_login cleared")


@th.django_unit_test("#7189: the owner cannot plant `onetime_code`")
def test_owner_cannot_write_onetime_code(opts):
    pk = _own(opts, "code")
    _assert_ignored(opts, "/api/user/me", pk, {"onetime_code": "usf-planted"}, "onetime_code")
    assert_eq(_stored(pk)["onetime_code"], STORED_CODE, "the stored code must be the server's")


@th.django_unit_test("#7189: the owner cannot backdate `modified`")
def test_owner_cannot_write_modified(opts):
    pk = _own(opts, "modified")
    _assert_ignored(opts, "/api/user/me", pk, {"modified": OLD}, "modified")


@th.django_unit_test("#7189: the owner cannot post another account's `id`")
def test_owner_cannot_write_id(opts):
    from mojo.apps.account.models import User

    other_name = f"{PREFIX}id_other"
    other_id = _mk_user(other_name)
    pk = _own(opts, "id")
    other_before = _stored(other_id)
    _assert_ignored(opts, "/api/user/me", pk,
                    {"id": other_id, "display_name": "usf renamed"}, "id")
    assert_eq(_stored(other_id), other_before, "the other account's server fields must be unchanged")
    assert_eq(User.objects.get(pk=other_id).display_name, other_name,
              "the other account must not take the caller's values")
    assert_eq(User.objects.get(pk=pk).display_name, "usf renamed",
              "the caller's own row must take the ordinary field posted beside `id`")


@th.django_unit_test("#7189: every server field in one body is ignored together")
def test_owner_cannot_write_them_together(opts):
    pk = _own(opts, "all")
    _assert_ignored(opts, "/api/user/me", pk, {
        "created": OLD, "uuid": PLANTED_UUID, "date_joined": OLD, "last_login": OLD,
        "onetime_code": "usf-planted", "modified": OLD}, "all together")
    stored = _stored(pk)
    assert_true(stored["created"] > datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                "`created` must not have been backdated")
    assert_true(str(stored["uuid"]) != PLANTED_UUID, "`uuid` must not be the posted one")


@th.django_unit_test("#7189: a manage_users admin cannot backdate another account's `created`")
def test_admin_cannot_write_created(opts):
    target_id = _mk_user(f"{PREFIX}admin_target")
    admin_name = f"{PREFIX}admin"
    _mk_user(admin_name, perms=["manage_users"])
    _login(opts, admin_name)
    _assert_ignored(opts, f"/api/user/{target_id}", target_id,
                    {"created": OLD, "uuid": PLANTED_UUID, "onetime_code": "usf-planted"}, "admin")


@th.django_unit_test("#7189: User lists its own server fields beside the framework names")
def test_user_effective_list(opts):
    from mojo.apps.account.models import User

    effective = User.get_no_save_fields()
    for key in ("id", "pk", "created", "uuid", "date_joined", "last_login", "onetime_code", "modified"):
        assert_true(key in effective, f"`{key}` must be protected on User")
    declared = User.get_rest_meta_prop("NO_SAVE_FIELDS", [])
    for key in ("date_joined", "last_login", "onetime_code", "modified"):
        assert_true(key in declared, f"`{key}` must be in User.RestMeta.NO_SAVE_FIELDS")
