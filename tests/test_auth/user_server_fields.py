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
OWNER = "usf_owner"
ADMIN = "usf_admin"
TARGET = "usf_target"
OTHER = "usf_other"
PWORD = "usf##mojo99Fields"
OLD = "2000-01-01T00:00:00Z"
PLANTED_UUID = "11111111-2222-3333-4444-555555555555"
STORED_CODE = "usf-stored-code"

SERVER_FIELDS = ("id", "uuid", "created", "date_joined", "last_login", "onetime_code", "modified")


@th.django_unit_setup()
def setup_user_server_fields(opts):
    from mojo.apps.account.models import User
    from mojo.helpers import dates

    User.objects.filter(username__in=[OWNER, ADMIN, TARGET, OTHER]).delete()
    for name in (OWNER, ADMIN, TARGET, OTHER):
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PWORD)
        user.remove_all_permissions()
        if name == ADMIN:
            user.add_permission(["manage_users"])
        setattr(opts, f"{name}_id", user.pk)
    User.objects.filter(pk__in=[opts.usf_owner_id, opts.usf_target_id]).update(
        onetime_code=STORED_CODE, last_login=dates.utcnow())


def _login(opts, name):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    assert_true(opts.client.login(name, PWORD), f"{name} must be able to log in")


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
    _login(opts, OWNER)
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {"created": OLD}, "created")


@th.django_unit_test("#7189: the owner cannot replace the account `uuid`")
def test_owner_cannot_write_uuid(opts):
    _login(opts, OWNER)
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {"uuid": PLANTED_UUID}, "uuid")


@th.django_unit_test("#7189: the owner cannot backdate `date_joined`")
def test_owner_cannot_write_date_joined(opts):
    _login(opts, OWNER)
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {"date_joined": OLD}, "date_joined")


@th.django_unit_test("#7189: the owner cannot set or clear `last_login`")
def test_owner_cannot_write_last_login(opts):
    _login(opts, OWNER)
    assert_true(_stored(opts.usf_owner_id)["last_login"] is not None,
                "the account must have a last_login to protect")
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {"last_login": OLD}, "last_login set")
    # An empty last_login makes the next password reset mark the email verified.
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {"last_login": ""}, "last_login cleared")


@th.django_unit_test("#7189: the owner cannot plant `onetime_code`")
def test_owner_cannot_write_onetime_code(opts):
    _login(opts, OWNER)
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {"onetime_code": "usf-planted"}, "onetime_code")
    assert_eq(_stored(opts.usf_owner_id)["onetime_code"], STORED_CODE, "the stored code must be the server's")


@th.django_unit_test("#7189: the owner cannot backdate `modified`")
def test_owner_cannot_write_modified(opts):
    _login(opts, OWNER)
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {"modified": OLD}, "modified")


@th.django_unit_test("#7189: the owner cannot post another account's `id`")
def test_owner_cannot_write_id(opts):
    from mojo.apps.account.models import User

    _login(opts, OWNER)
    other_before = _stored(opts.usf_other_id)
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id,
                    {"id": opts.usf_other_id, "display_name": "usf renamed"}, "id")
    assert_eq(_stored(opts.usf_other_id), other_before, "the other account's server fields must be unchanged")
    assert_eq(User.objects.get(pk=opts.usf_other_id).display_name, OTHER,
              "the other account must not take the caller's values")
    assert_eq(User.objects.get(pk=opts.usf_owner_id).display_name, "usf renamed",
              "the caller's own row must take the ordinary field posted beside `id`")


@th.django_unit_test("#7189: every server field in one body is ignored together")
def test_owner_cannot_write_them_together(opts):
    _login(opts, OWNER)
    _assert_ignored(opts, "/api/user/me", opts.usf_owner_id, {
        "created": OLD, "uuid": PLANTED_UUID, "date_joined": OLD, "last_login": OLD,
        "onetime_code": "usf-planted", "modified": OLD}, "all together")
    stored = _stored(opts.usf_owner_id)
    assert_true(stored["created"] > datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                "`created` must not have been backdated")
    assert_true(str(stored["uuid"]) != PLANTED_UUID, "`uuid` must not be the posted one")


@th.django_unit_test("#7189: a manage_users admin cannot backdate another account's `created`")
def test_admin_cannot_write_created(opts):
    _login(opts, ADMIN)
    _assert_ignored(opts, f"/api/user/{opts.usf_target_id}", opts.usf_target_id,
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
