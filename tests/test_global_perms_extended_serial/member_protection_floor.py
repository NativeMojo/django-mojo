"""The settings-file MEMBER_PERMS_PROTECTION map is a floor a database row
cannot remove or loosen (maestro item #7148).

Before the fix the map was read with settings.get, which returns a
platform-wide Setting row WHOLESALE in place of the settings-file value, and
reads a malformed or blank row as {}. So a row of `{}`, a row that re-maps a
protected permission to "member", a blank row or a malformed row each let a
group manager grant a permission the deployment's settings file protects.

Every test sets the file map on the live server with th.server_settings (a
server reload) and writes the protected row — legal only in this serial,
opt-in package. Assertions go through opts.client only: the test process never
sees the server's file map.
"""
import uuid as _uuid
from testit import helpers as th

KEY = "MEMBER_PERMS_PROTECTION"
FILE_PERM = "itest_gpx_file_protected"
ROW_PERM = "itest_gpx_row_protected"
PLAIN_PERM = "itest_gpx_unlisted"
# Requires a global perm nobody in this module holds → always refused.
NEVER = "sys.itest_gpx_never_held_by_anyone"
FILE_MAP = {FILE_PERM: NEVER}
PWORD = "Gpx##floor99"


def _clear_row():
    """Delete the platform-wide row and its cache field, validators bypassed."""
    from mojo.apps.account.models.setting import Setting
    for row in Setting.objects.filter(key=KEY, group=None):
        row.remove_from_cache()
    Setting.objects.filter(key=KEY, group=None).delete()


def _make_user(email, perms=None):
    from mojo.apps.account.models import User
    user = User.objects.create_user(username=email, email=email, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    if perms:
        user.add_permission(list(perms))
        user.save()
    return user


@th.django_unit_setup()
def setup_member_protection_floor(opts):
    from mojo.apps.account.models import Group, GroupMember

    # Long-lived DB: clear anything a previous run left behind.
    _clear_row()

    suffix = _uuid.uuid4().hex[:8]
    # Group manager: member-level authority only, so the gate's global
    # short-circuit never applies to them.
    opts.manager_email = f"gpx_manager_{suffix}@globalperms.test"
    manager = _make_user(opts.manager_email)
    opts.target_email = f"gpx_target_{suffix}@globalperms.test"
    target = _make_user(opts.target_email)
    # Platform admin for the /api/settings write.
    opts.settings_admin_email = f"gpx_settings_{suffix}@globalperms.test"
    _make_user(opts.settings_admin_email, perms=["manage_settings"])

    opts.group = Group.objects.create(name=f"gpx_floor_{suffix}", kind="organization")
    mm, _ = GroupMember.objects.get_or_create(user=manager, group=opts.group)
    mm.permissions = {"manage_group": True, "manage_members": True}
    mm.save()
    tm, _ = GroupMember.objects.get_or_create(user=target, group=opts.group)
    tm.permissions = {}
    tm.save()
    opts.target_member_id = tm.pk
    opts.suffix = suffix


def _login(opts, email):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(email, PWORD), \
        f"login failed for {email}: {opts.client.last_response.body}"


def _member_save(opts, perm):
    """Grant `perm` on the target member row. Returns (status, landed)."""
    from mojo.apps.account.models import GroupMember
    GroupMember.objects.filter(pk=opts.target_member_id).update(permissions={})
    resp = opts.client.post(f"/api/group/member/{opts.target_member_id}", {
        "permissions": {perm: True}})
    tm = GroupMember.objects.get(pk=opts.target_member_id)
    return resp.status_code, bool(tm.permissions.get(perm))


def _invite(opts, perm):
    """Invite a fresh user with `perm`. Returns (status, landed)."""
    from mojo.apps.account.models import User, GroupMember
    email = f"gpx_invitee_{_uuid.uuid4().hex[:8]}@globalperms.test"
    try:
        resp = opts.client.post("/api/group/member/invite", {
            "group": opts.group.pk,
            "email": email,
            "permissions": {perm: True},
        })
        landed = False
        invitee = User.objects.filter(email=email).first()
        if invitee is not None:
            m = GroupMember.objects.filter(user=invitee, group=opts.group).first()
            landed = bool(m is not None and m.permissions.get(perm))
        return resp.status_code, landed
    finally:
        invitee = User.objects.filter(email=email).first()
        if invitee is not None:
            GroupMember.objects.filter(user=invitee).delete()
            invitee.delete()


def _assert_refused(opts, perm, why):
    for label, call in (("invite", _invite), ("member save", _member_save)):
        status, landed = call(opts, perm)
        assert status == 403, \
            f"{why}: {label} granting {perm} must be refused, got {status}: {opts.client.last_response.body}"
        assert not landed, f"{why}: {perm} landed on the member despite the refused {label}"


def _assert_allowed(opts, perm, why):
    for label, call in (("invite", _invite), ("member save", _member_save)):
        status, landed = call(opts, perm)
        assert status == 200, \
            f"{why}: {label} granting {perm} must be allowed, got {status}: {opts.client.last_response.body}"
        assert landed, f"{why}: {perm} must land on the member after the {label}"


@th.django_unit_test("member protection floor: an empty-object row cannot remove a file-protected permission")
def test_empty_object_row_keeps_file_floor(opts):
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(MEMBER_PERMS_PROTECTION=FILE_MAP):
            Setting.set(KEY, {})
            _login(opts, opts.manager_email)
            _assert_refused(opts, FILE_PERM, "row of {} against the file map")
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("member protection floor: a row cannot loosen a file-protected permission")
def test_weaker_row_keeps_file_floor(opts):
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(MEMBER_PERMS_PROTECTION=FILE_MAP):
            Setting.set(KEY, {FILE_PERM: "member"})
            _login(opts, opts.manager_email)
            _assert_refused(opts, FILE_PERM, "row re-mapping the file key to 'member'")
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("member protection floor: a malformed row refuses member-level grants")
def test_malformed_row_refuses_grants(opts):
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(MEMBER_PERMS_PROTECTION=FILE_MAP):
            # A row that arrived by SQL / a queryset update / before the write
            # validator existed. resolve() reads Redis first and a queryset
            # update does not touch it, so push the stored value explicitly.
            row = Setting.set(KEY, {})
            Setting.objects.filter(pk=row.pk).update(value="not json")
            row.refresh_from_db()
            row.push_to_cache()
            _login(opts, opts.manager_email)
            _assert_refused(opts, PLAIN_PERM, "malformed row, unlisted permission")
            _assert_refused(opts, FILE_PERM, "malformed row, file-protected permission")
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("member protection floor: /api/settings refuses a malformed map")
def test_settings_api_refuses_malformed_map(opts):
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    _login(opts, opts.settings_admin_email)
    try:
        for payload in ("not json", '["a"]', '{"a": ""}', '{"a": []}', '{"a": 5}',
                        "null", "5", '"a"', "true"):
            resp = opts.client.post("/api/settings", {"key": KEY, "value": payload})
            assert resp.status_code == 400, (
                f"{KEY}={payload!r} must be refused at write time, "
                f"got {resp.status_code}: {opts.client.last_response.body}")
            assert not Setting.objects.filter(key=KEY, group=None).exists(), \
                f"refused write of {KEY}={payload!r} must not persist a row"
        resp = opts.client.post("/api/settings", {
            "key": KEY, "value": '{"%s": "%s"}' % (ROW_PERM, NEVER)})
        assert resp.status_code == 200, (
            f"a valid map must still save, got {resp.status_code}: "
            f"{opts.client.last_response.body}")
        assert Setting.objects.filter(key=KEY, group=None).exists(), \
            "a valid map must persist a row"
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("member protection floor: a row adding a protected permission is enforced")
def test_row_addition_is_enforced(opts):
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(MEMBER_PERMS_PROTECTION=FILE_MAP):
            Setting.set(KEY, {ROW_PERM: NEVER})
            _login(opts, opts.manager_email)
            _assert_refused(opts, ROW_PERM, "row adding a protected permission")
            _assert_refused(opts, FILE_PERM, "file map beside a row addition")
            _assert_allowed(opts, PLAIN_PERM, "unlisted permission beside a row addition")
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("member protection floor: a blank row adds nothing and keeps the file floor")
def test_blank_row_keeps_file_floor(opts):
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(MEMBER_PERMS_PROTECTION=FILE_MAP):
            _login(opts, opts.manager_email)
            for blank in ("", "  \n"):
                Setting.set(KEY, blank)
                why = f"blank row {blank!r}"
                _assert_allowed(opts, PLAIN_PERM, why)
                _assert_refused(opts, FILE_PERM, why)
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("member protection floor: a direct model save of a dict is stored as JSON and enforced")
def test_direct_dict_save_is_stored_as_json(opts):
    """Setting(value={...}).save() bypasses set_value. The dict passed the
    validator but was persisted as its Python repr (single quotes), which the
    reader treats as malformed once the cache entry is gone — refusing every
    member-level change platform-wide."""
    import json
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(MEMBER_PERMS_PROTECTION=FILE_MAP):
            row = Setting(key=KEY, value={ROW_PERM: NEVER})
            row.save()
            stored = Setting.objects.get(pk=row.pk).value
            assert json.loads(stored) == {ROW_PERM: NEVER}, \
                f"a dict saved directly must be persisted as JSON, got {stored!r}"
            # Read from the database, not from a cache entry a writer pushed.
            row.remove_from_cache()
            _login(opts, opts.manager_email)
            _assert_allowed(opts, PLAIN_PERM, "direct dict save, unlisted permission")
            _assert_refused(opts, ROW_PERM, "direct dict save, row-protected permission")
            _assert_refused(opts, FILE_PERM, "direct dict save, file-protected permission")

            for bad in (None, 5, True, ["a"], {"a": []}):
                _clear_row()
                refused = False
                try:
                    Setting(key=KEY, value=bad).save()
                except Exception:
                    refused = True
                assert refused, f"a direct save of {bad!r} must be refused"
                assert not Setting.objects.filter(key=KEY, group=None).exists(), \
                    f"a refused direct save of {bad!r} must not persist a row"
    finally:
        opts.client.logout()
        _clear_row()
