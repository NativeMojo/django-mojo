"""Tests for create-time owner-field auto-stamping in mojo/models/rest.py.

The framework auto-assigns ``CREATED_BY_OWNER_FIELD`` (default ``"user"``) to
``request.user`` when a record is created — but only when the body did not
already provide a value. This mirrors the long-standing ``group`` behavior.

Uses ``shortlink.ShortLink`` as the unit-under-test host because it has a
nullable ``user`` FK, no ``on_rest_created`` / ``on_rest_saved`` hooks that
rely on ``active_request``, and permissive ``manage_shortlinks`` perms.
"""
import objict
from testit import helpers as th


OWNER_ADMIN_EMAIL = "owner_stamp_admin@test.com"
OWNER_OTHER_EMAIL = "owner_stamp_other@test.com"


@th.django_unit_setup()
def setup_owner_stamp(opts):
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink

    # Clean up leftovers from prior runs — tests share a long-lived db.
    ShortLink.objects.filter(code__startswith="ostmp").delete()
    User.objects.filter(email__in=[OWNER_ADMIN_EMAIL, OWNER_OTHER_EMAIL]).delete()

    opts.admin = User.objects.create_user(
        username=OWNER_ADMIN_EMAIL, email=OWNER_ADMIN_EMAIL, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    # view_users is required so the admin can assign ShortLink.user by pk to
    # another account — on_rest_save_related_field gates FK assignment with
    # VIEW_PERMS on the related model (User.VIEW_PERMS).
    for perm in ["view_admin", "manage_shortlinks", "view_users"]:
        opts.admin.add_permission(perm)

    opts.other = User.objects.create_user(
        username=OWNER_OTHER_EMAIL, email=OWNER_OTHER_EMAIL, password="pass123",
    )
    opts.other.is_email_verified = True
    opts.other.save()


def _build_request(user, data=None):
    """Synthetic request just rich enough for on_rest_save / rest_check_permission."""
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(data or {})
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/shortlink/shortlink"
    req.META = {}
    req.api_key = None
    return req


def _make_and_save(code, data, user):
    """Instantiate a ShortLink and run it through the create-time save path."""
    from mojo.apps.shortlink.models import ShortLink
    data = dict(data)
    data.setdefault("code", code)
    data.setdefault("url", "https://example.com/")
    req = _build_request(user, data=data)
    instance = ShortLink()
    instance.on_rest_save(req, req.DATA)
    return instance


def _cleanup(code):
    from mojo.apps.shortlink.models import ShortLink
    ShortLink.objects.filter(code=code).delete()


# ---------------------------------------------------------------------------
# Default owner field (CREATED_BY_OWNER_FIELD = "user")
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_body_omits_user_auto_stamps_caller(opts):
    """Body without `user` → framework auto-stamps request.user. Self-signup path."""
    code = "ostmp1"
    try:
        link = _make_and_save(code, {}, opts.admin)
        assert link.user_id == opts.admin.id, (
            f"Expected user={opts.admin.id} (caller) when body omits user, got {link.user_id}"
        )
    finally:
        _cleanup(code)


@th.django_unit_test()
def test_body_user_equals_caller_preserves_value(opts):
    """Body `user: <caller.id>` → kept (same id, no observable change)."""
    code = "ostmp2"
    try:
        link = _make_and_save(code, {"user": opts.admin.id}, opts.admin)
        assert link.user_id == opts.admin.id, (
            f"Expected user={opts.admin.id} when body explicitly names caller, got {link.user_id}"
        )
    finally:
        _cleanup(code)


@th.django_unit_test()
def test_body_user_other_wins_over_auto_stamp(opts):
    """Body `user: <other.id>` → framework respects body. Core new behavior."""
    code = "ostmp3"
    try:
        link = _make_and_save(code, {"user": opts.other.id}, opts.admin)
        assert link.user_id == opts.other.id, (
            f"Expected user={opts.other.id} from body, got {link.user_id} "
            f"(framework still clobbering with request.user={opts.admin.id}?)"
        )
    finally:
        _cleanup(code)


@th.django_unit_test()
def test_body_user_null_falls_back_to_caller(opts):
    """Body `user: null` → coerced to None → auto-stamp kicks in → caller wins."""
    code = "ostmp4"
    try:
        link = _make_and_save(code, {"user": None}, opts.admin)
        assert link.user_id == opts.admin.id, (
            f"Expected user={opts.admin.id} when body user is null, got {link.user_id}"
        )
    finally:
        _cleanup(code)


@th.django_unit_test()
def test_body_user_zero_falls_back_to_caller(opts):
    """Body `user: 0` → coerced to None by on_rest_save_related_field → caller wins."""
    code = "ostmp5"
    try:
        link = _make_and_save(code, {"user": 0}, opts.admin)
        assert link.user_id == opts.admin.id, (
            f"Expected user={opts.admin.id} when body user is 0, got {link.user_id}"
        )
    finally:
        _cleanup(code)


# ---------------------------------------------------------------------------
# Opt-out: CREATED_BY_OWNER_FIELD = None
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_owner_field_none_skips_auto_stamp(opts):
    """CREATED_BY_OWNER_FIELD = None → no auto-stamp at all. Body wins (or stays None)."""
    from mojo.apps.shortlink.models import ShortLink

    code = "ostmp6"
    original = getattr(ShortLink.RestMeta, "CREATED_BY_OWNER_FIELD", "__MISSING__")
    setattr(ShortLink.RestMeta, "CREATED_BY_OWNER_FIELD", None)
    try:
        # With the opt-out, omitting user should leave it None — no auto-stamp.
        link = _make_and_save(code, {}, opts.admin)
        assert link.user_id is None, (
            f"Expected user=None when CREATED_BY_OWNER_FIELD=None and body omits user, got {link.user_id}"
        )
        _cleanup(code)

        # And body-provided user should be preserved.
        link = _make_and_save(code, {"user": opts.other.id}, opts.admin)
        assert link.user_id == opts.other.id, (
            f"Expected user={opts.other.id} when CREATED_BY_OWNER_FIELD=None and body provides user, got {link.user_id}"
        )
    finally:
        if original == "__MISSING__":
            delattr(ShortLink.RestMeta, "CREATED_BY_OWNER_FIELD")
        else:
            setattr(ShortLink.RestMeta, "CREATED_BY_OWNER_FIELD", original)
        _cleanup(code)


# ---------------------------------------------------------------------------
# Update path: owner field is never auto-stamped on update (UPDATED_BY_OWNER_FIELD
# governs `modified_by`, not `user`). Body-provided user on update goes through
# unchanged by the create-time block.
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_update_body_user_is_respected(opts):
    """PUT body `user: <other>` on existing row is applied by the field loop;
    the create-time auto-stamp block never runs on update."""
    code = "ostmp7"
    try:
        link = _make_and_save(code, {"user": opts.admin.id}, opts.admin)
        assert link.user_id == opts.admin.id, (
            f"Setup precondition: initial user should be admin, got {link.user_id}"
        )

        req = _build_request(opts.admin, data={"user": opts.other.id})
        req.method = "PUT"
        link.on_rest_save(req, req.DATA)
        link.refresh_from_db()
        assert link.user_id == opts.other.id, (
            f"Update should keep body user={opts.other.id}, got {link.user_id}"
        )
    finally:
        _cleanup(code)


# ---------------------------------------------------------------------------
# Group behavior unchanged — regression guard. Same "only if unset" logic
# now applies to both fields; this pins the group half.
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_group_still_auto_fills_when_omitted(opts):
    """Body omits group, request.group is set → auto-filled. Unchanged behavior."""
    from mojo.apps.account.models import Group

    Group.objects.filter(name="ostmp_group").delete()
    group = Group.objects.create(name="ostmp_group")
    code = "ostmp8"
    try:
        req = _build_request(opts.admin, data={"code": code, "url": "https://example.com/"})
        req.group = group
        from mojo.apps.shortlink.models import ShortLink
        instance = ShortLink()
        instance.on_rest_save(req, req.DATA)
        assert instance.group_id == group.id, (
            f"Expected group={group.id} auto-filled when body omits group, got {instance.group_id}"
        )
    finally:
        _cleanup(code)
        Group.objects.filter(name="ostmp_group").delete()
