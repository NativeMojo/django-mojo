"""Maestro item #7189 — the framework's own no-save names apply to every model.

`on_rest_save` used to read `RestMeta.NO_SAVE_FIELDS` INSTEAD of the framework
default `["id", "pk", "created", "uuid"]`, so a model that declared its own
list silently dropped the default. A posted `id` was then assigned to the
loaded row and the save updated the row WITH THAT ID: a signed-in user saving
their own push device with another user's device id took the other row over.
The permission check had run on the caller's own row.

The four names are now always protected and a declared list adds to them.
`RestMeta.ALLOW_SAVE_FIELDS` can hand back `created` and `uuid` only; Group
declares `uuid` (commit fc07d333).
"""
import uuid as _uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

IP = "127.0.0.1"
PWORD = "nsd##mojo99Defaults"
PREFIX = "nsd_"
DEFAULTS = ("id", "pk", "created", "uuid")


def _mk_user(name):
    from mojo.apps.account.models import User
    user = User(username=name, display_name=name, email=f"{name}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(PWORD)
    user.remove_all_permissions()
    return user


def _login(opts, name):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=IP, key="login")
    assert_true(opts.client.login(name, PWORD), f"{name} must be able to log in")


def _mk_device(user, tag):
    from mojo.apps.account.models import RegisteredDevice
    return RegisteredDevice.objects.create(
        user=user, device_id=f"{PREFIX}dev_{tag}", device_token=f"{PREFIX}tok_{tag}",
        platform="ios", device_name=f"{PREFIX}{tag}")


def _device(pk):
    from mojo.apps.account.models import RegisteredDevice
    return RegisteredDevice.objects.filter(pk=pk).first()


@th.django_unit_setup()
def setup_no_save_defaults(opts):
    from mojo.apps.account.models import User, Group, RegisteredDevice
    from mojo.apps.account.models.user_api_key import UserAPIKey

    # delete-before-create — tests run against a long-lived DB
    RegisteredDevice.objects.filter(device_id__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()

    opts.tag = _uuid.uuid4().hex[:8]
    opts.name_a = f"{PREFIX}a_{opts.tag}"
    opts.name_b = f"{PREFIX}b_{opts.tag}"
    opts.name_admin = f"{PREFIX}admin_{opts.tag}"
    opts.name_mgr = f"{PREFIX}mgr_{opts.tag}"
    user_a = _mk_user(opts.name_a)
    user_b = _mk_user(opts.name_b)
    admin = _mk_user(opts.name_admin)
    admin.add_permission(["manage_groups"])
    # An ordinary user cannot create a device through the model endpoint (403,
    # they register instead), so the create case needs a holder of SAVE_PERMS.
    mgr = _mk_user(opts.name_mgr)
    mgr.add_permission(["manage_devices"])
    opts.user_mgr_id = mgr.pk
    opts.user_a_id = user_a.pk
    opts.user_b_id = user_b.pk

    opts.dev_a_id = _mk_device(user_a, f"a_{opts.tag}").pk
    opts.dev_b_id = _mk_device(user_b, f"b_{opts.tag}").pk

    opts.key_a_id = UserAPIKey.create_for_user(user_a, label=f"{PREFIX}a").id
    opts.key_b_id = UserAPIKey.create_for_user(user_b, label=f"{PREFIX}b").id

    grp = Group.objects.create(name=f"{PREFIX}grp_{opts.tag}", kind="organization", is_active=True)
    opts.grp_id = grp.pk


@th.django_unit_test("#7189: saving my own row with another row's id leaves the other row alone")
def test_update_with_another_rows_id(opts):
    tag = opts.tag
    _login(opts, opts.name_a)
    resp = opts.client.post(f"/api/account/devices/push/{opts.dev_a_id}", {
        "id": opts.dev_b_id,
        "device_id": f"{PREFIX}dev_a2_{tag}",
        "device_token": f"{PREFIX}tok_a2_{tag}",
    })
    assert_eq(resp.status_code, 200, f"an ignored `id` must not fail the save, got {resp.status_code}")

    other = _device(opts.dev_b_id)
    assert_true(other is not None, "the other user's device row must still exist")
    assert_eq(other.user_id, opts.user_b_id, "the other user's device must still belong to them")
    assert_eq(other.device_id, f"{PREFIX}dev_b_{tag}", "the other user's device_id must be unchanged")
    assert_eq(other.device_token, f"{PREFIX}tok_b_{tag}", "the other user's device_token must be unchanged")

    mine = _device(opts.dev_a_id)
    assert_true(mine is not None, "the caller's own device row must still exist")
    assert_eq(mine.user_id, opts.user_a_id, "the caller's device must still belong to the caller")
    assert_eq(mine.device_id, f"{PREFIX}dev_a2_{tag}", "the caller's own row must take the posted device_id")
    assert_eq(mine.device_token, f"{PREFIX}tok_a2_{tag}", "the caller's own row must take the posted device_token")


@th.django_unit_test("#7189: creating a row with another row's id creates a new row")
def test_create_with_another_rows_id(opts):
    from mojo.apps.account.models import RegisteredDevice

    tag = opts.tag
    _login(opts, opts.name_mgr)
    resp = opts.client.post("/api/account/devices/push", {
        "id": opts.dev_b_id,
        "device_id": f"{PREFIX}dev_a3_{tag}",
        "device_token": f"{PREFIX}tok_a3_{tag}",
        "platform": "android",
    })
    assert_eq(resp.status_code, 200, f"an ignored `id` must not fail the create, got {resp.status_code}")

    other = _device(opts.dev_b_id)
    assert_true(other is not None, "the other user's device row must still exist")
    assert_eq(other.user_id, opts.user_b_id, "the other user's device must still belong to them")
    assert_eq(other.device_id, f"{PREFIX}dev_b_{tag}", "the other user's device_id must be unchanged")
    assert_eq(other.device_token, f"{PREFIX}tok_b_{tag}", "the other user's device_token must be unchanged")
    assert_eq(other.platform, "ios", "the other user's platform must be unchanged")

    made = RegisteredDevice.objects.filter(device_id=f"{PREFIX}dev_a3_{tag}").first()
    assert_true(made is not None, "the create must have made a new row")
    assert_true(made.pk != opts.dev_b_id, "the new row must have its own id")
    assert_eq(made.user_id, opts.user_mgr_id, "the new row must belong to the caller")


@th.django_unit_test("#7189: a body of another row's id plus an action runs the action on my row only")
def test_id_plus_action_does_not_save(opts):
    from mojo.apps.account.models.user_api_key import UserAPIKey

    before = UserAPIKey.objects.get(pk=opts.key_b_id)
    mine_before = UserAPIKey.objects.get(pk=opts.key_a_id)
    _login(opts, opts.name_a)
    resp = opts.client.post(f"/api/account/api_keys/{opts.key_a_id}", {
        "id": opts.key_b_id, "revoke": True})
    assert_eq(resp.status_code, 200, f"an ignored `id` must not fail the action, got {resp.status_code}")

    other = UserAPIKey.objects.get(pk=opts.key_b_id)
    assert_eq(other.user_id, opts.user_b_id, "the other user's key must still belong to them")
    assert_true(other.is_active, "the other user's key must not be revoked")
    assert_eq(other.jti, before.jti, "the other user's key id must be unchanged")
    assert_true(other.mojo_secrets == before.mojo_secrets, "the other user's signing secret must be unchanged")

    mine = UserAPIKey.objects.get(pk=opts.key_a_id)
    assert_true(not mine.is_active, "the action must have revoked the caller's own key")
    assert_eq(mine.jti, mine_before.jti, "the caller's key id must be unchanged")
    assert_eq(mine.label, mine_before.label, "an action-only body must not rewrite the caller's row")


@th.django_unit_test("#7189: every mojo.apps model that declares NO_SAVE_FIELDS keeps the framework names")
def test_every_declared_list_keeps_the_defaults(opts):
    from django.apps import apps
    from mojo.models.rest import DEFAULT_NO_SAVE_FIELDS

    assert_eq(tuple(DEFAULT_NO_SAVE_FIELDS), DEFAULTS, "the framework's always-protected names")

    checked = 0
    exceptions = {}
    for model in apps.get_models():
        if not model.__module__.startswith("mojo.apps."):
            continue
        rest_meta = getattr(model, "RestMeta", None)
        if rest_meta is None or getattr(rest_meta, "NO_SAVE_FIELDS", None) is None:
            continue
        checked += 1
        effective = model.get_no_save_fields()
        allowed = set(getattr(rest_meta, "ALLOW_SAVE_FIELDS", None) or [])
        if allowed:
            exceptions[model.__name__] = sorted(allowed)
        for key in ("id", "pk"):
            assert_true(key in effective, f"`{key}` must be protected on {model.__name__}")
        for key in ("created", "uuid"):
            if key in allowed:
                continue
            assert_true(key in effective, f"`{key}` must be protected on {model.__name__}")
        for key in rest_meta.NO_SAVE_FIELDS:
            assert_true(key in effective, f"{model.__name__}'s own `{key}` must stay protected")
    assert_true(checked >= 20, f"expected to find the models that declare a list, found {checked}")
    assert_eq(exceptions, {"Group": ["uuid"]}, "only Group declares an exception, and only for `uuid`")


@th.django_unit_test("#7189: a model with no declared list, or an empty one, protects the four names")
def test_default_and_empty_lists(opts):
    from mojo.models import MojoModel

    class Bare(MojoModel):
        pass

    class NoList(MojoModel):
        class RestMeta:
            pass

    class Empty(MojoModel):
        class RestMeta:
            NO_SAVE_FIELDS = []

    for probe in (Bare, NoList, Empty):
        assert_eq(list(probe.get_no_save_fields()), list(DEFAULTS),
                  f"{probe.__name__} must protect exactly the framework names")


@th.django_unit_test("#7189: ALLOW_SAVE_FIELDS hands back `created` and `uuid` only")
def test_allow_save_fields_is_limited(opts):
    from mojo.models import MojoModel

    class Greedy(MojoModel):
        class RestMeta:
            NO_SAVE_FIELDS = ["owner", "id"]
            ALLOW_SAVE_FIELDS = ["id", "pk", "created", "uuid", "owner"]

    effective = Greedy.get_no_save_fields()
    for key in ("id", "pk", "owner"):
        assert_true(key in effective, f"`{key}` must stay protected whatever ALLOW_SAVE_FIELDS says")
    for key in ("created", "uuid"):
        assert_true(key not in effective, f"`{key}` is allowed back by ALLOW_SAVE_FIELDS")
    assert_eq(len(effective), len(set(effective)), "no name is listed twice")

    class IdOnly(MojoModel):
        class RestMeta:
            ALLOW_SAVE_FIELDS = ["id"]

    assert_eq(list(IdOnly.get_no_save_fields()), list(DEFAULTS),
              "ALLOW_SAVE_FIELDS = ['id'] must not make `id` writable")

    # a name the model itself declares stays protected even when it is also allowed
    for name, other in (("created", "uuid"), ("uuid", "created")):
        class Overlap(MojoModel):
            class RestMeta:
                NO_SAVE_FIELDS = [name]
                ALLOW_SAVE_FIELDS = ["created", "uuid"]

        effective = Overlap.get_no_save_fields()
        assert_true(name in effective, f"`{name}` is declared and allowed: the declaration wins")
        assert_true(other not in effective, f"`{other}` is only allowed, so it is handed back")
        assert_eq(len(effective), len(set(effective)), "no name is listed twice")


@th.django_unit_test("#7189: a groups admin can still change a group's uuid, and nothing else of the four")
def test_group_uuid_stays_writable(opts):
    from mojo.apps.account.models import Group

    before = Group.objects.get(pk=opts.grp_id)
    new_uuid = _uuid.uuid4().hex
    assert_true(str(before.uuid) != new_uuid, "the test needs a different uuid to post")
    _login(opts, opts.name_admin)
    resp = opts.client.post(f"/api/group/{opts.grp_id}", {
        "uuid": new_uuid, "created": "2000-01-01T00:00:00Z"})
    assert_eq(resp.status_code, 200, f"the group save must succeed, got {resp.status_code}")

    after = Group.objects.get(pk=opts.grp_id)
    assert_eq(str(after.uuid).replace("-", ""), new_uuid, "a groups admin must still be able to set a group's uuid")
    assert_eq(after.created, before.created, "a group's `created` must stay protected")
    assert_true("uuid" not in Group.get_no_save_fields(), "Group declares `uuid` writable")


@th.django_unit_test("#7189: the permissions report and the assistant's audit list show the effective list")
def test_other_readers_use_the_effective_list(opts):
    from mojo.apps.account.models import RegisteredDevice
    from mojo.apps.assistant.services.tools.models import _changed_field_names
    from mojo.rest.model_permissions import extract_model_info

    info = extract_model_info(RegisteredDevice, "account", verbose=True)
    assert_eq(list(info["no_save_fields"]), list(RegisteredDevice.get_no_save_fields()),
              "the permissions report must show what the save enforces")
    for key in ("id", "pk", "created", "uuid", "user"):
        assert_true(key in info["no_save_fields"], f"`{key}` must be in the reported list")

    audited = _changed_field_names(RegisteredDevice, {
        "id": 1, "created": "2000-01-01", "user": 2, "device_name": "phone"})
    assert_eq(audited, ["device_name"], "the audit list must leave out every name the save ignores")
