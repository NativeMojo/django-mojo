"""FileManager role settings: one rule, whichever key carries them (#7586).

A store can act under an AWS role. The role is assumed with the store's own
AWS key when it has one, and with the platform's identity when it has none or
holds a copy of the platform's key. The REST save dispatches `set_<key>` for
any posted key, so the role values and the key can arrive as flat fields or
inside `secrets` / `settings`; all three write the same store.

The rule, checked on the store as the request leaves it: a non-superuser may
not leave a store with a role while it runs on platform credentials. On a
store with its own key the role is the group's own business.

These tests post over the real endpoint and read the database. No AWS call is
made: updates here change neither the backend nor `is_public`, and the one
created store uses the `file` backend.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


ADMIN = "fm_rolegen_admin"      # holds the global `files` permission
MEMBER = "fm_rolegen_member"    # holds `files` in one group only
SUPER = "fm_rolegen_super"
PASSWORD = "rolegen-pass-99!"
GROUP = "fm_rolegen_group"

FIXTURE_ARN = "arn:aws:iam::210987654321:role/tenant-fileman"
POSTED_ARN = "arn:aws:iam::111111111111:role/x"
FIXTURE_EXTERNAL_ID = "fixture-external-id"
POSTED_EXTERNAL_ID = "posted-external-id"

OWN_KEY = "AKIAROLEGENOWNKEY"
OWN_SECRET = "rolegen-own-secret"
# Held by a test-owned system-scoped store, which is what makes it a
# platform key to the rule.
PLATFORM_KEY = "AKIAROLEGENPLATFORM"
PLATFORM_SECRET = "rolegen-platform-secret"

ROLE_VALUES = {
    "assume_role_arn": POSTED_ARN,
    "external_id": POSTED_EXTERNAL_ID,
    "role_session_name": "rolegen-session",
    "assume_role_duration": 1800,
}
SYSTEM_DEFAULT_PK = -7586


def _forms(values):
    """The same values as flat fields, inside `secrets`, and inside `settings`."""
    return [("flat", dict(values)), ("secrets", {"secrets": dict(values)}),
            ("settings", {"settings": dict(values)})]


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_role_generic_keys(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    FileManager.objects.filter(name__startswith="fm_rolegen_").delete()
    FileManager.objects.filter(name__startswith="Clone of fm_rolegen_").delete()
    FileManager.objects.filter(group__name=GROUP).delete()
    GroupMember.objects.filter(group__name=GROUP).delete()
    Group.objects.filter(name=GROUP).delete()
    User.objects.filter(username__in=[ADMIN, MEMBER, SUPER]).delete()

    def make_user(username):
        user = User(username=username, email=f"{username}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PASSWORD)
        user.save()
        return user

    admin = make_user(ADMIN)
    admin.add_permission("files")
    admin.save()
    member_user = make_user(MEMBER)
    superuser = make_user(SUPER)
    superuser.is_superuser = True
    superuser.save()

    group = Group(name=GROUP)
    group.save()
    member = group.add_member(member_user)
    member.add_permission("files")
    member.save()

    # Not a default, so get_for_user/get_for_group never derive from it.
    system = FileManager(
        name="fm_rolegen_system",
        backend_type="file",
        backend_url="file://",
        is_active=True,
        is_default=False,
    )
    system.set_secrets({"aws_key": PLATFORM_KEY, "aws_secret": PLATFORM_SECRET})
    system.save()

    opts.group_id = group.id
    opts.member_user_id = member_user.id


def _login(opts, username):
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _new_manager(opts, name, key="own", role=False):
    """A fresh group S3 store, written by direct ORM use.

    key: "own" (the group's own key), "platform" (a copy of the platform's
    key, as created and derived stores hold) or None (no key of its own).
    """
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name=f"fm_rolegen_{name}").delete()
    fm = FileManager(
        name=f"fm_rolegen_{name}",
        backend_type="s3",
        backend_url="s3://rolegen-bucket/fileman/prefix",
        is_active=True,
        group_id=opts.group_id,
    )
    secrets = {}
    if key == "own":
        secrets.update(aws_key=OWN_KEY, aws_secret=OWN_SECRET)
    elif key == "platform":
        secrets.update(aws_key=PLATFORM_KEY, aws_secret=PLATFORM_SECRET)
    if role:
        secrets.update(assume_role_arn=FIXTURE_ARN, external_id=FIXTURE_EXTERNAL_ID)
    if secrets:
        fm.set_secrets(secrets)
    fm.save()
    return fm


def _stored(fm):
    from mojo.apps.fileman.models import FileManager

    return dict(FileManager.objects.get(pk=fm.pk).secrets)


def _post(opts, fm, payload):
    return opts.client.post(f"/api/fileman/manager/{fm.pk}", payload)


def _assert_stored(opts, fm, payload, expected, who):
    resp = _post(opts, fm, payload)
    assert_eq(resp.status_code, 200,
              f"{who} posting {sorted(payload)} must be accepted, got HTTP "
              f"{resp.status_code}: {resp.response}")
    stored = _stored(fm)
    for key, value in expected.items():
        assert_eq(stored.get(key), value,
                  f"{who} posting {sorted(payload)}: {key} must be stored as {value!r}, "
                  f"got {stored.get(key)!r}")


def _assert_refused(opts, fm, payload, who):
    before = _stored(fm)
    resp = _post(opts, fm, payload)
    after = _stored(fm)
    assert_eq(resp.status_code, 403,
              f"{who} posting {sorted(payload)} must be refused, got HTTP "
              f"{resp.status_code}; stored {sorted(after)}")
    assert_eq(after, before,
              f"a refused save must store nothing; {who} posting {sorted(payload)} "
              f"changed keys {sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}")


# ---------------------------------------------------------------------------
# A store on its own key: the role is the group's own business.
# ---------------------------------------------------------------------------

@th.django_unit_test("own key: a group file manager sets all four role values, by each field name")
def test_group_member_sets_role_on_own_key_store(opts):
    _login(opts, MEMBER)
    for form, payload in _forms(ROLE_VALUES):
        fm = _new_manager(opts, f"own_set_{form}", key="own")
        _assert_stored(opts, fm, payload, ROLE_VALUES, f"group file manager ({form})")


@th.django_unit_test("own key: a group file manager changes an existing role, by each field name")
def test_group_member_changes_role_on_own_key_store(opts):
    _login(opts, MEMBER)
    changed = {"assume_role_arn": POSTED_ARN, "external_id": POSTED_EXTERNAL_ID}
    for form, payload in _forms(changed):
        fm = _new_manager(opts, f"own_change_{form}", key="own", role=True)
        _assert_stored(opts, fm, payload, changed, f"group file manager ({form})")


@th.django_unit_test("own key: a global file admin sets the role, by each field name")
def test_files_admin_sets_role_on_own_key_store(opts):
    _login(opts, ADMIN)
    for form, payload in _forms(ROLE_VALUES):
        fm = _new_manager(opts, f"own_admin_{form}", key="own")
        _assert_stored(opts, fm, payload, ROLE_VALUES, f"global file admin ({form})")


@th.django_unit_test("own key: key and role posted together on a keyless store are stored")
def test_own_key_and_role_in_one_request(opts):
    _login(opts, MEMBER)
    values = {"aws_key": OWN_KEY, "aws_secret": OWN_SECRET, "assume_role_arn": POSTED_ARN}
    for form, payload in _forms(values):
        fm = _new_manager(opts, f"together_{form}", key=None)
        _assert_stored(opts, fm, payload, values, f"group file manager ({form})")


# ---------------------------------------------------------------------------
# A store on platform credentials: only a superuser may leave a role on it.
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("platform credentials: a group file manager cannot add a role to a keyless store")
def test_group_member_cannot_add_role_to_keyless_store(opts):
    _login(opts, MEMBER)
    for form, payload in _forms({"assume_role_arn": POSTED_ARN}):
        fm = _new_manager(opts, f"keyless_add_{form}", key=None)
        _assert_refused(opts, fm, payload, f"group file manager ({form})")


@th.tier("core")
@th.django_unit_test("platform credentials: a copy of the platform's key is not the store's own key")
def test_group_member_cannot_add_role_to_platform_key_store(opts):
    _login(opts, MEMBER)
    for form, payload in _forms({"assume_role_arn": POSTED_ARN}):
        fm = _new_manager(opts, f"platkey_add_{form}", key="platform")
        _assert_refused(opts, fm, payload, f"group file manager ({form})")


@th.django_unit_test("platform credentials: a global file admin cannot change a role or its external id")
def test_files_admin_cannot_change_role_on_platform_store(opts):
    _login(opts, ADMIN)
    for name, value in (("assume_role_arn", POSTED_ARN), ("external_id", POSTED_EXTERNAL_ID),
                        ("role_session_name", "rolegen-session"), ("assume_role_duration", 1800)):
        for form, payload in _forms({name: value}):
            fm = _new_manager(opts, f"plat_change_{name}_{form}", key="platform", role=True)
            _assert_refused(opts, fm, payload, f"global file admin ({form}, {name})")


@th.django_unit_test("platform credentials: a body mixing an ordinary field with the role is refused whole")
def test_mixed_body_refused_whole(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, ADMIN)
    fm = _new_manager(opts, "mixed", key="platform")
    payload = {"description": "rolegen mixed body", "secrets": {"assume_role_arn": POSTED_ARN},
               "settings": {"aws_region": "eu-west-1"}}
    _assert_refused(opts, fm, payload, "global file admin")
    assert_eq(FileManager.objects.get(pk=fm.pk).description, "",
              "the ordinary field in a refused body must not be stored either")


@th.tier("core")
@th.django_unit_test("platform credentials: the key cannot be taken away from a store that has a role")
def test_key_removed_from_role_store_refused(opts):
    _login(opts, MEMBER)
    for field in ("aws_key", "aws_secret"):
        for form, payload in _forms({field: None}):
            fm = _new_manager(opts, f"key_removed_{field}_{form}", key="own", role=True)
            _assert_refused(opts, fm, payload, f"group file manager ({form}, {field} removed)")


@th.django_unit_test("platform credentials: the key cannot be replaced by the platform's on a store that has a role")
def test_key_replaced_by_platform_key_refused(opts):
    _login(opts, MEMBER)
    values = {"aws_key": PLATFORM_KEY, "aws_secret": PLATFORM_SECRET}
    for form, payload in _forms(values):
        fm = _new_manager(opts, f"key_replaced_{form}", key="own", role=True)
        _assert_refused(opts, fm, payload, f"group file manager ({form})")


@th.django_unit_test("platform credentials: a role may be removed, and a store without a role may lose its key")
def test_safe_direction_allowed(opts):
    _login(opts, MEMBER)
    fm = _new_manager(opts, "role_removed", key="platform", role=True)
    resp = _post(opts, fm, {"assume_role_arn": None})
    assert_eq(resp.status_code, 200,
              f"removing a role leaves no role on platform credentials, got HTTP {resp.status_code}")
    assert_true("assume_role_arn" not in _stored(fm), "the removed role must be gone")

    fm = _new_manager(opts, "key_removed_no_role", key="own")
    resp = _post(opts, fm, {"secrets": {"aws_key": None, "aws_secret": None}})
    assert_eq(resp.status_code, 200,
              f"a store with no role may drop its key, got HTTP {resp.status_code}")
    assert_true("aws_key" not in _stored(fm), "the removed key must be gone")


# ---------------------------------------------------------------------------
# Create.
# ---------------------------------------------------------------------------

def _create(opts, name, values):
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name=name).delete()
    payload = {"name": name, "group": opts.group_id, "backend_type": "file",
               "backend_url": "file://", "is_public": False}
    payload.update(values)
    return opts.client.post("/api/fileman/manager", payload)


@th.tier("core")
@th.django_unit_test("create: a role with no key is refused and no store is created")
def test_create_with_role_and_no_key_refused(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    for form, values in _forms({"assume_role_arn": POSTED_ARN}):
        name = f"fm_rolegen_create_refused_{form}"
        resp = _create(opts, name, values)
        assert_eq(resp.status_code, 403,
                  f"create with a role and no key ({form}) must be refused, got HTTP {resp.status_code}")
        assert_true(not FileManager.objects.filter(name=name).exists(),
                    f"a refused create ({form}) must not leave a store behind")


@th.django_unit_test("create: a role with the store's own key is stored")
def test_create_with_role_and_own_key_allowed(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    values = {"aws_key": OWN_KEY, "aws_secret": OWN_SECRET, "assume_role_arn": POSTED_ARN}
    for form, body in _forms(values):
        name = f"fm_rolegen_create_allowed_{form}"
        resp = _create(opts, name, body)
        assert_eq(resp.status_code, 200,
                  f"create with a role and an own key ({form}) must be accepted, got HTTP "
                  f"{resp.status_code}: {resp.response}")
        created = FileManager.objects.filter(name=name).first()
        assert_true(created is not None, f"the accepted create ({form}) must leave a store")
        assert_eq(created.get_secret("assume_role_arn"), POSTED_ARN,
                  f"the created store ({form}) must carry the posted role")
        assert_eq(created.get_secret("aws_key"), OWN_KEY,
                  f"the created store ({form}) must keep its own key, not the platform default")


# ---------------------------------------------------------------------------
# Superuser, and what a file admin keeps.
# ---------------------------------------------------------------------------

@th.django_unit_test("superuser: sets and changes a role on platform credentials, by each field name")
def test_superuser_sets_role_on_platform_store(opts):
    _login(opts, SUPER)
    for form, payload in _forms(ROLE_VALUES):
        fm = _new_manager(opts, f"super_keyless_{form}", key=None)
        _assert_stored(opts, fm, payload, ROLE_VALUES, f"superuser ({form}, keyless)")
        fm = _new_manager(opts, f"super_platkey_{form}", key="platform", role=True)
        _assert_stored(opts, fm, payload, ROLE_VALUES, f"superuser ({form}, platform key)")


@th.django_unit_test("superuser: takes the key away from a store that has a role")
def test_superuser_removes_key_from_role_store(opts):
    _login(opts, SUPER)
    fm = _new_manager(opts, "super_key_removed", key="own", role=True)
    resp = _post(opts, fm, {"aws_key": None, "aws_secret": None})
    assert_eq(resp.status_code, 200,
              f"a superuser may move a role store onto platform credentials, got HTTP {resp.status_code}")
    stored = _stored(fm)
    assert_true("aws_key" not in stored, "the superuser's key removal must be stored")
    assert_eq(stored.get("assume_role_arn"), FIXTURE_ARN, "the role must be untouched")


@th.django_unit_test("file admin: other settings stay editable on a platform store that already has a role")
def test_file_admin_edits_other_settings_on_platform_role_store(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    fm = _new_manager(opts, "other_settings", key="platform", role=True)
    payload = {"description": "rolegen other settings", "aws_region": "eu-west-1",
               "secrets": {"upload_expires_in": 120},
               "settings": {"assume_role_arn": FIXTURE_ARN, "aws_key": PLATFORM_KEY}}
    _assert_stored(opts, fm, payload,
                   {"aws_region": "eu-west-1", "upload_expires_in": 120,
                    "assume_role_arn": FIXTURE_ARN, "external_id": FIXTURE_EXTERNAL_ID,
                    "aws_key": PLATFORM_KEY},
                   "group file manager")
    assert_eq(FileManager.objects.get(pk=fm.pk).description, "rolegen other settings",
              "an ordinary field must be stored on a store whose role and key the request left alone")


@th.django_unit_test("file admin: cloning a platform store that has a role still works")
def test_clone_platform_role_store(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    fm = _new_manager(opts, "clone_source", key="platform", role=True)
    FileManager.objects.filter(name=f"Clone of {fm.name}").delete()
    resp = _post(opts, fm, {"clone": True})
    assert_eq(resp.status_code, 200, f"clone must succeed, got HTTP {resp.status_code}: {resp.response}")
    clone = FileManager.objects.filter(name=f"Clone of {fm.name}").first()
    assert_true(clone is not None, "clone must create a store")
    assert_eq(clone.get_secret("assume_role_arn"), FIXTURE_ARN, "the clone must carry the source's role")
    assert_eq(clone.get_secret("aws_key"), PLATFORM_KEY, "the clone must carry the source's key")


@th.django_unit_test("file admin: a group's automatic store is still derived, role and key included")
def test_automatic_group_store_still_derived(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.fileman.models import FileManager
    from mojo.models.rest import ACTIVE_REQUEST

    # get_for_group() takes the first system default. A negative fixture id
    # keeps this row the one it finds without touching another test's row
    # (same device as 12_test_public_access_reconciliation.py).
    FileManager.objects.filter(pk=SYSTEM_DEFAULT_PK).delete()
    system = FileManager(
        pk=SYSTEM_DEFAULT_PK,
        name="fm_rolegen_system_default",
        backend_type="file",
        backend_url="file://",
        is_active=True,
        is_default=True,
    )
    system.save()
    system.set_secrets({"aws_key": PLATFORM_KEY, "aws_secret": PLATFORM_SECRET,
                        "assume_role_arn": FIXTURE_ARN})
    system.save()

    class _Request:
        user = User.objects.get(pk=opts.member_user_id)

    token = ACTIVE_REQUEST.set(_Request())
    try:
        derived = FileManager.get_for_group(group=Group.objects.get(pk=opts.group_id), use="rolegen")
    finally:
        ACTIVE_REQUEST.reset(token)
        FileManager.objects.filter(parent_id=SYSTEM_DEFAULT_PK).update(parent=None)
        system.delete()

    assert_true(derived is not None, "a group's automatic store must still be created for a non-superuser")
    stored = _stored(derived)
    assert_eq(stored.get("assume_role_arn"), FIXTURE_ARN, "the derived store must copy the system store's role")
    assert_eq(stored.get("aws_key"), PLATFORM_KEY, "the derived store must copy the system store's key")


@th.django_unit_test("the encrypted column is not a writable field")
def test_mojo_secrets_post_changes_nothing(opts):
    from mojo.apps.fileman.models import FileManager

    _login(opts, MEMBER)
    fm = _new_manager(opts, "raw_column", key="own", role=True)
    before_blob = FileManager.objects.get(pk=fm.pk).mojo_secrets
    before = _stored(fm)
    resp = _post(opts, fm, {"mojo_secrets": "posted-blob", "secret": "x", "setting": "y"})
    assert_eq(resp.status_code, 200,
              f"unwritable names are skipped, not an error, got HTTP {resp.status_code}: {resp.response}")
    assert_eq(FileManager.objects.get(pk=fm.pk).mojo_secrets, before_blob,
              "a posted mojo_secrets must not replace the encrypted column")
    assert_eq(_stored(fm), before, "the stored settings must be unchanged")
