"""Regression tests for FileManager owner-field auto-stamping and system scope.

FileManager supports user-, group-, and system-scoped managers. The generic
REST create path in mojo/models/rest.py auto-stamps ``CREATED_BY_OWNER_FIELD``
(default ``"user"``) with the caller whenever the body omits it. For
FileManager that would force every REST-created manager to be user-owned and
break group/system managers, so ``FileManager.RestMeta`` opts out with
``CREATED_BY_OWNER_FIELD = None``.

A FileManager with no user and no group is *system-scoped* — it can become the
system default that ``get_for_user`` / ``get_for_group`` derive every other
manager from. ``on_rest_pre_save`` therefore restricts system-scoped REST
creation to superusers.

Exercises the create-time save path directly (``on_rest_save``) the way
tests/test_models/owner_stamp.py does, but binds ``ACTIVE_REQUEST`` so the
superuser guard can resolve the actor. The ``file`` backend is used so
``on_rest_saved`` -> ``backend.make_path_public()`` is a no-op.
"""
import objict
from testit import helpers as th


FM_OWNER_USER = "fm_owner_stamp_user"
FM_OWNER_SUPER = "fm_owner_stamp_super"


@th.django_unit_setup()
def setup_fm_owner_stamp(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.fileman.models import FileManager

    # Tests share a long-lived db — clear leftovers before creating.
    FileManager.objects.filter(name__startswith="fm_ostmp").delete()
    Group.objects.filter(name="fm_ostmp_group").delete()
    User.objects.filter(username__in=[FM_OWNER_USER, FM_OWNER_SUPER]).delete()

    user = User(username=FM_OWNER_USER, email=f"{FM_OWNER_USER}@example.com")
    user.save()
    user.is_email_verified = True
    # view_users lets an explicit user-id FK assignment clear
    # on_rest_save_related_field's VIEW_PERMS gate on the User model.
    user.add_permission(["view_fileman", "manage_files", "view_users"])
    user.save()
    opts.user = user

    superuser = User(username=FM_OWNER_SUPER, email=f"{FM_OWNER_SUPER}@example.com")
    superuser.is_superuser = True
    superuser.save()
    opts.super = superuser


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
    """Run a new FileManager through the create-time REST save path.

    Binds ACTIVE_REQUEST so on_rest_pre_save's `active_user` resolves — this is
    what the middleware does on a real request.
    """
    from mojo.apps.fileman.models import FileManager
    from mojo.models.rest import ACTIVE_REQUEST
    data = dict(data)
    data.setdefault("name", name)
    data.setdefault("backend_type", "file")
    data.setdefault("backend_url", "file://")
    req = _build_request(user, data=data, group=group)
    fm = FileManager()
    token = ACTIVE_REQUEST.set(req)
    try:
        fm.on_rest_save(req, req.DATA)
    finally:
        ACTIVE_REQUEST.reset(token)
    return fm


def _cleanup(name):
    from mojo.apps.fileman.models import FileManager
    FileManager.objects.filter(name=name).delete()


# ---------------------------------------------------------------------------
# Owner auto-stamp opt-out — user is never stamped to the caller.
# ---------------------------------------------------------------------------

@th.django_unit_test("FileManager: group manager omits user -> user stays None")
def test_fm_group_omits_user_stays_none(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name="fm_ostmp_group").delete()
    group = Group.objects.create(name="fm_ostmp_group")
    name = "fm_ostmp_omit"
    try:
        fm = _create_fm(name, {}, opts.user, group=group)
        assert fm.user_id is None, (
            f"Expected user=None when body omits user (CREATED_BY_OWNER_FIELD "
            f"opt-out), got {fm.user_id} — framework auto-stamped the caller"
        )
        assert fm.group_id == group.id, (
            f"Expected group={group.id} auto-filled from request.group, "
            f"got {fm.group_id}"
        )
    finally:
        _cleanup(name)
        Group.objects.filter(name="fm_ostmp_group").delete()


@th.django_unit_test("FileManager: group manager with user: null -> user stays None")
def test_fm_group_null_user_stays_none(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name="fm_ostmp_group").delete()
    group = Group.objects.create(name="fm_ostmp_group")
    name = "fm_ostmp_null"
    try:
        fm = _create_fm(name, {"user": None}, opts.user, group=group)
        assert fm.user_id is None, (
            f"Expected user=None when body sends user: null, got {fm.user_id}"
        )
    finally:
        _cleanup(name)
        Group.objects.filter(name="fm_ostmp_group").delete()


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


# ---------------------------------------------------------------------------
# System scope (user=None AND group=None) — superuser-only via REST.
# ---------------------------------------------------------------------------

@th.django_unit_test("FileManager: system-scope create blocked for non-superuser")
def test_fm_system_scope_blocked_for_regular_user(opts):
    from mojo import errors as me

    name = "fm_ostmp_sysblock"
    raised = False
    try:
        _create_fm(name, {}, opts.user)
    except me.PermissionDeniedException:
        raised = True
    finally:
        _cleanup(name)
    assert raised, (
        "Expected PermissionDeniedException when a non-superuser creates a "
        "system-scoped FileManager (user=None, group=None)"
    )


@th.django_unit_test("FileManager: system-scope create allowed for superuser")
def test_fm_system_scope_allowed_for_superuser(opts):
    name = "fm_ostmp_sysok"
    try:
        fm = _create_fm(name, {}, opts.super)
        assert fm.pk is not None, "system-scoped manager should be saved"
        assert fm.user_id is None, (
            f"Expected user=None for a system-scoped manager, got {fm.user_id}"
        )
        assert fm.group_id is None, (
            f"Expected group=None for a system-scoped manager, got {fm.group_id}"
        )
    finally:
        _cleanup(name)


@th.django_unit_setup()
def cleanup_fm_owner_stamp(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(name__startswith="fm_ostmp").delete()
    Group.objects.filter(name="fm_ostmp_group").delete()
    User.objects.filter(username__in=[FM_OWNER_USER, FM_OWNER_SUPER]).delete()
