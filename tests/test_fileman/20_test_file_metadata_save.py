"""Saving a file's metadata over the API (#7391).

`File.set_metadata(key, value)` is a Python helper whose name is also the REST
save hook for the `metadata` field. These tests cover both uses: the API save
(validated, then merged by the framework's own JSON-field save) and the
unchanged two-argument Python call.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


OWNER = "mdsave_owner"
OTHER = "mdsave_other"
MEMBER = "mdsave_member"
SUPER = "mdsave_super"
PASSWORD = "mdsave-pass-99!"


def _login(opts, username):
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _new_file(opts, name, metadata=None, group=False, upload_status="completed"):
    """A file owned by OWNER (and by the test group when group=True)."""
    from mojo.apps.fileman.models import File

    File.objects.filter(filename=f"mdsave_{name}.csv").delete()
    return File.objects.create(
        filename=f"mdsave_{name}.csv",
        file_manager_id=opts.fm_id,
        user_id=opts.owner_id,
        group_id=opts.group_id if group else None,
        content_type="text/csv",
        category="csv",
        file_size=100,
        upload_status=upload_status,
        storage_file_path=f"/tmp/mdsave_files/mdsave_{name}.csv",
        storage_filename=f"mdsave_{name}.csv",
        upload_token=f"mdsave_tok_{name}",
        metadata=metadata if metadata is not None else {"source": "mdsave"},
    )


def _save(opts, f, payload):
    return opts.client.post(f"/api/fileman/file/{f.pk}", payload)


def _stored(f):
    from mojo.apps.fileman.models import File

    return File.objects.get(pk=f.pk)


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_metadata_save(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import File, FileManager

    File.objects.filter(filename__startswith="mdsave_").delete()
    FileManager.objects.filter(name="mdsave_fm").delete()
    GroupMember.objects.filter(group__name="mdsave_group").delete()
    Group.objects.filter(name="mdsave_group").delete()
    User.objects.filter(username__in=[OWNER, OTHER, MEMBER, SUPER]).delete()

    def make_user(username):
        user = User(username=username, email=f"{username}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PASSWORD)
        user.save()
        return user

    owner = make_user(OWNER)
    make_user(OTHER)
    member_user = make_user(MEMBER)
    superuser = make_user(SUPER)
    superuser.is_superuser = True
    superuser.save()

    group = Group(name="mdsave_group")
    group.save()
    member = group.add_member(member_user)
    member.add_permission("files")
    member.save()

    fm = FileManager.objects.create(
        name="mdsave_fm",
        backend_type="file",
        backend_url="filesystem:///tmp/mdsave_files",
        is_active=True,
        user=owner,
    )

    opts.owner_id = owner.id
    opts.group_id = group.id
    opts.fm_id = fm.id


@th.django_unit_test()
def test_owner_saves_metadata(opts):
    """The file's owner saves metadata over the API: 200 and the value is stored."""
    f = _new_file(opts, "owner_saves")
    _login(opts, OWNER)
    resp = _save(opts, f, {"metadata": {"color": "blue"}})
    assert_eq(resp.status_code, 200, f"owner metadata save must succeed: {resp.response}")
    assert_eq(_stored(f).metadata.get("color"), "blue",
              "the saved metadata value must be in the database")


@th.django_unit_test()
def test_metadata_save_merges_and_null_removes(opts):
    """Keys not in the request are kept; a key sent as null is removed."""
    f = _new_file(opts, "merges", metadata={"keep": "1", "drop": "2"})
    _login(opts, OWNER)
    resp = _save(opts, f, {"metadata": {"add": "3", "drop": None}})
    assert_eq(resp.status_code, 200, f"merge save must succeed: {resp.response}")
    stored = _stored(f).metadata
    assert_eq(stored.get("keep"), "1", "a key not in the request must be kept")
    assert_eq(stored.get("add"), "3", "a new key must be added")
    assert_true("drop" not in stored, f"a key sent as null must be removed, got: {stored}")


@th.django_unit_test()
def test_group_files_member_saves_metadata(opts):
    """A group member holding `files` saves metadata on the group's file."""
    f = _new_file(opts, "member_saves", group=True)
    _login(opts, MEMBER)
    resp = _save(opts, f, {"metadata": {"reviewed": True}})
    assert_eq(resp.status_code, 200, f"group files member save must succeed: {resp.response}")
    assert_eq(_stored(f).metadata.get("reviewed"), True,
              "the member's metadata value must be in the database")


@th.django_unit_test()
def test_stranger_cannot_save_metadata(opts):
    """A caller with no right to the file gets 403 and nothing is stored."""
    f = _new_file(opts, "stranger")
    _login(opts, OTHER)
    resp = _save(opts, f, {"metadata": {"color": "red"}})
    assert_eq(resp.status_code, 403, f"a stranger must be denied: {resp.response}")
    assert_true("color" not in _stored(f).metadata, "a denied save must store nothing")


@th.django_unit_test()
def test_aware_expires_at_is_stored(opts):
    """An expiry with a timezone is stored, with +00:00 and with Z."""
    _login(opts, OWNER)
    for name, value in (("aware_offset", "2999-01-01T00:00:00+00:00"),
                        ("aware_z", "2999-01-01T00:00:00Z")):
        f = _new_file(opts, name)
        resp = _save(opts, f, {"metadata": {"expires_at": value}})
        assert_eq(resp.status_code, 200, f"expires_at {value} must be accepted: {resp.response}")
        assert_eq(_stored(f).metadata.get("expires_at"), value,
                  f"expires_at {value} must be stored as sent")


@th.django_unit_test()
def test_bad_expires_at_is_refused(opts):
    """An expiry the clean-up cannot rely on is a 400 and nothing of the request is stored."""
    _login(opts, OWNER)
    naive = "2026-10-01T00:00:00"
    payloads = (
        ("no_timezone", {"metadata": {"expires_at": naive}}),
        ("date_alone", {"metadata": {"expires_at": "2026-10-01"}}),
        ("free_text", {"metadata": {"expires_at": "next friday"}}),
        ("number", {"metadata": {"expires_at": 12345}}),
        ("dotted_key", {"metadata.expires_at": "2026-10-01"}),
        ("replace", {"metadata": {"__replace": True, "expires_at": "2026-10-01"}}),
        ("json_string", {"metadata": '{"expires_at": "2026-10-01"}'}),
    )
    for name, payload in payloads:
        f = _new_file(opts, f"bad_{name}")
        payload = dict(payload, filename=f"mdsave_renamed_{name}.csv")
        resp = _save(opts, f, payload)
        assert_eq(resp.status_code, 400, f"{name}: a bad expires_at must be a 400: {resp.response}")
        stored = _stored(f)
        assert_eq(stored.filename, f"mdsave_bad_{name}.csv",
                  f"{name}: the filename in a refused request must not be saved")
        assert_eq(stored.metadata, {"source": "mdsave"},
                  f"{name}: a refused request must leave metadata unchanged")


@th.django_unit_test()
def test_non_object_metadata_is_refused(opts):
    """Metadata that is not an object is a 400 and nothing is stored."""
    _login(opts, OWNER)
    for name, value in (("list", ["a", "b"]), ("bare_string", "hello")):
        f = _new_file(opts, f"nonobject_{name}")
        resp = _save(opts, f, {"metadata": value})
        assert_eq(resp.status_code, 400, f"{name}: non-object metadata must be a 400: {resp.response}")
        assert_eq(_stored(f).metadata, {"source": "mdsave"},
                  f"{name}: a refused request must leave metadata unchanged")


@th.django_unit_test()
def test_protected_key_keeps_framework_rule(opts):
    """The `protected` root key is refused for the owner and allowed for a superuser."""
    f = _new_file(opts, "protected")
    _login(opts, OWNER)
    resp = _save(opts, f, {"metadata": {"protected": {"tier": "gold"}}})
    assert_eq(resp.status_code, 403, f"the owner must not write the protected key: {resp.response}")
    assert_true("protected" not in _stored(f).metadata, "a refused protected write must store nothing")

    _login(opts, SUPER)
    resp = _save(opts, f, {"metadata": {"protected": {"tier": "gold"}}})
    assert_eq(resp.status_code, 200, f"a superuser may write the protected key: {resp.response}")
    assert_eq(_stored(f).metadata.get("protected"), {"tier": "gold"},
              "the superuser's protected value must be stored")


@th.django_unit_test()
def test_metadata_with_action_in_same_request(opts):
    """Metadata and an action in one request: both take effect."""
    from mojo.apps.fileman.models import File

    f = _new_file(opts, "with_action", upload_status=File.PENDING)
    _login(opts, OWNER)
    resp = _save(opts, f, {"metadata": {"color": "green"}, "action": "mark_as_uploading"})
    assert_eq(resp.status_code, 200, f"metadata with an action must succeed: {resp.response}")
    stored = _stored(f)
    assert_eq(stored.metadata.get("color"), "green", "the metadata must be stored")
    assert_eq(stored.upload_status, File.UPLOADING, "the action must have run")


@th.django_unit_test()
def test_python_set_metadata_unchanged(opts):
    """The two-argument Python helper sets one key, and mark_as_failed still uses it."""
    f = _new_file(opts, "python_helper")
    f.set_metadata("width", 1920)
    assert_eq(f.metadata.get("width"), 1920, "set_metadata(key, value) must set that key")
    assert_eq(f.metadata.get("source"), "mdsave", "set_metadata(key, value) must keep other keys")

    f.mark_as_failed("disk full", commit=True)
    assert_eq(_stored(f).metadata.get("error_message"), "disk full",
              "mark_as_failed must store error_message through set_metadata")


@th.django_unit_test()
def test_api_expiry_is_cleaned_up(opts):
    """A past expiry saved over the API is read by the clean-up, which deletes the file."""
    from objict import objict
    from mojo.apps.fileman.asyncjobs import cleanup_expired_files
    from mojo.apps.fileman.models import File

    f = _new_file(opts, "api_expiry")
    _login(opts, OWNER)
    resp = _save(opts, f, {"metadata": {"expires_at": "2020-01-01T00:00:00+00:00"}})
    assert_eq(resp.status_code, 200, f"a past expiry with a timezone must be accepted: {resp.response}")

    cleanup_expired_files(objict(payload={}))
    assert_true(not File.objects.filter(pk=f.pk).exists(),
                "the clean-up must delete a file whose API-saved expiry has passed")


@th.django_unit_test()
def test_bad_expires_at_is_refused_on_create(opts):
    """A create that carries a bad expires_at is a 400 and no file row is created."""
    from mojo.apps.fileman.models import File

    File.objects.filter(filename="mdsave_created.csv").delete()
    # A plain owner cannot create a File row over this endpoint (403), so the
    # create path is exercised as a superuser.
    _login(opts, SUPER)
    resp = opts.client.post("/api/fileman/file", {
        "filename": "mdsave_created.csv",
        "file_manager": opts.fm_id,
        "metadata": {"expires_at": "2026-10-01"},
    })
    assert_eq(resp.status_code, 400, f"a create with a bad expires_at must be a 400: {resp.response}")
    assert_true(not File.objects.filter(filename="mdsave_created.csv").exists(),
                "a refused create must not leave a file row")
