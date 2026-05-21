"""Regression tests for FileManager owner-field auto-stamping.

FileManager supports user-, group-, and system-scoped managers. The generic
REST create path in mojo/models/rest.py auto-stamps ``CREATED_BY_OWNER_FIELD``
(default ``"user"``) with the caller whenever the body omits it. For
FileManager that would force every REST-created manager to be user-owned and
break group/system managers. ``FileManager.RestMeta`` opts out with
``CREATED_BY_OWNER_FIELD = None`` — these tests pin that behavior.

Exercises the create-time save path directly (``on_rest_save``) the same way
tests/test_models/owner_stamp.py does. The ``file`` backend is used so
``on_rest_saved`` -> ``backend.make_path_public()`` is a no-op.
"""
import objict
from testit import helpers as th


FM_OWNER_USER = "fm_owner_stamp_user"
FM_OWNER_PWORD = "fmowner##mojo99"


@th.django_unit_setup()
def setup_fm_owner_stamp(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.fileman.models import FileManager

    # Tests share a long-lived db — clear leftovers before creating.
    FileManager.objects.filter(name__startswith="fm_ostmp").delete()
    Group.objects.filter(name="fm_ostmp_group").delete()
    User.objects.filter(username=FM_OWNER_USER).delete()

    user = User(username=FM_OWNER_USER, email=f"{FM_OWNER_USER}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(FM_OWNER_PWORD)
    # view_users lets an explicit user-id FK assignment clear
    # on_rest_save_related_field's VIEW_PERMS gate on the User model.
    user.add_permission(["view_fileman", "manage_files", "view_users"])
    user.save()
    opts.user = user


def _build_request(user, data=None, group=None):
    """Synthetic request rich enough for FileManager.on_rest_save."""
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = group
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/fileman/manager"
    req.META = {}
    req.api_key = None
    return req


def _create_fm(name, data, user, group=None):
    """Run a new FileManager through the create-time REST save path."""
    from mojo.apps.fileman.models import FileManager
    data = dict(data)
    data.setdefault("name", name)
    data.setdefault("backend_type", "file")
    data.setdefault("backend_url", "file://")
    req = _build_request(user, data=data, group=group)
    fm = FileManager()
    fm.on_rest_save(req, req.DATA)
    return fm


def _cleanup(name):
    from mojo.apps.fileman.models import FileManager
    FileManager.objects.filter(name=name).delete()


@th.django_unit_test("FileManager: body omits user -> user stays None (not auto-stamped)")
def test_fm_omits_user_stays_none(opts):
    name = "fm_ostmp_omit"
    try:
        fm = _create_fm(name, {}, opts.user)
        assert fm.user_id is None, (
            f"Expected user=None when body omits user (CREATED_BY_OWNER_FIELD "
            f"opt-out), got {fm.user_id} — framework auto-stamped the caller"
        )
    finally:
        _cleanup(name)


@th.django_unit_test("FileManager: body user: null -> user stays None")
def test_fm_null_user_stays_none(opts):
    name = "fm_ostmp_null"
    try:
        fm = _create_fm(name, {"user": None}, opts.user)
        assert fm.user_id is None, (
            f"Expected user=None when body sends user: null, got {fm.user_id}"
        )
    finally:
        _cleanup(name)


@th.django_unit_test("FileManager: body user: <id> -> explicit owner is honored")
def test_fm_explicit_user_is_honored(opts):
    name = "fm_ostmp_explicit"
    try:
        fm = _create_fm(name, {"user": opts.user.id}, opts.user)
        assert fm.user_id == opts.user.id, (
            f"Expected user={opts.user.id} when body provides an explicit user "
            f"id, got {fm.user_id}"
        )
    finally:
        _cleanup(name)


@th.django_unit_test("FileManager: group still auto-fills from request.group")
def test_fm_group_still_auto_stamps(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name="fm_ostmp_group").delete()
    group = Group.objects.create(name="fm_ostmp_group")
    name = "fm_ostmp_group_mgr"
    try:
        fm = _create_fm(name, {}, opts.user, group=group)
        assert fm.group_id == group.id, (
            f"Expected group={group.id} auto-filled from request.group, "
            f"got {fm.group_id}"
        )
        assert fm.user_id is None, (
            f"Expected user=None for a group-scoped manager, got {fm.user_id}"
        )
    finally:
        _cleanup(name)
        Group.objects.filter(name="fm_ostmp_group").delete()


@th.django_unit_setup()
def cleanup_fm_owner_stamp(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name__startswith="fm_ostmp").delete()
    Group.objects.filter(name="fm_ostmp_group").delete()
    User.objects.filter(username=FM_OWNER_USER).delete()
