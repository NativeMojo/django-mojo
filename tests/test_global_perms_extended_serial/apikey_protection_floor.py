"""The settings-file APIKEY_PERMS_PROTECTION map is a floor a database row
cannot remove or change (maestro item #7150).

Before the fix the map was read with settings.get, which returns a
platform-wide Setting row WHOLESALE in place of the settings-file value, and
reads a row that is not a JSON object as {}. So a row of `{}`, a row that
re-maps a protected permission to "manage_group", or a malformed row each let a
group admin put a permission the deployment's settings file protects on an API
key. Only the framework's four built-ins survived.

Every test sets the file map on the live server with th.server_settings (a
server reload) and writes the protected row — legal only in this serial,
opt-in package. Assertions go through opts.client only: the test process never
sees the server's file map.
"""
import uuid as _uuid
from testit import helpers as th

KEY = "APIKEY_PERMS_PROTECTION"
FILE_PERM = "itest_akx_file_protected"
ROW_PERM = "itest_akx_row_protected"
PLAIN_PERM = "itest_akx_unlisted"
# Requires a global perm nobody in this module holds → always refused.
NEVER = "sys.itest_akx_never_held_by_anyone"
FILE_MAP = {FILE_PERM: NEVER}
PWORD = "Akx##floor99"
# A list where a map belongs: the shape a settings file gets by mistake.
MALFORMED_FILE = [FILE_PERM]
MALFORMED_LOG = "APIKEY_PERMS_PROTECTION is malformed"


def _clear_row():
    """Delete every row for the key and its cache field, validators bypassed."""
    from mojo.apps.account.models.setting import Setting
    for row in Setting.objects.filter(key=KEY):
        row.remove_from_cache()
    Setting.objects.filter(key=KEY).delete()


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
def setup_apikey_protection_floor(opts):
    from mojo.apps.account.models import Group, GroupMember, ApiKey

    # Long-lived DB: clear anything a previous run left behind.
    _clear_row()

    suffix = _uuid.uuid4().hex[:8]
    # Group admin: member-level authority only, so the gate's global
    # short-circuit never applies to them.
    opts.admin_email = f"akx_admin_{suffix}@globalperms.test"
    admin = _make_user(opts.admin_email)
    # Global manager: returns at the short-circuit, before the map is consulted.
    opts.global_email = f"akx_global_{suffix}@globalperms.test"
    _make_user(opts.global_email, perms=["manage_groups"])
    # Platform admin for the /api/settings writes.
    opts.settings_admin_email = f"akx_settings_{suffix}@globalperms.test"
    _make_user(opts.settings_admin_email, perms=["manage_settings"])

    opts.group = Group.objects.create(name=f"akx_floor_{suffix}", kind="organization")
    member, _ = GroupMember.objects.get_or_create(user=admin, group=opts.group)
    member.permissions = {"manage_group": True, "manage_members": True}
    member.save()
    # Reference-mode key holding `groups`: request.user IS the key, and
    # `groups` is what lets a key provision another key at all.
    _key, opts.minter_token = ApiKey.create_for_group(
        opts.group, f"akx_minter_{suffix}", permissions={"groups": True})
    opts.minter_pk = _key.pk
    opts.suffix = suffix


def _login(opts, email):
    from mojo.decorators.limits import clear_rate_limits
    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(email, PWORD), \
        f"login failed for {email}: {opts.client.last_response.body}"


def _use_minter_key(opts):
    """Switch the test client to send `Authorization: apikey <token>`."""
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.minter_token
    opts.client.is_authenticated = True


def _use_key(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _error_log_size():
    """Where the server's error.log ends now. The server is another process,
    so its log file is the only place a test can see what it logged."""
    from mojo.helpers import paths
    path = paths.VAR_ROOT / "logs" / "error.log"
    return path.stat().st_size if path.exists() else 0


def _malformed_logged_since(offset):
    """How many malformed-map errors the server logged after `offset`."""
    from mojo.helpers import paths
    path = paths.VAR_ROOT / "logs" / "error.log"
    if not path.exists():
        return 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", "replace").count(MALFORMED_LOG)


def _change(opts, perm, grant):
    """Grant `perm` on a new key, or revoke it from a key that holds it.
    Returns (status, changed); removes only the key it made."""
    from mojo.apps.account.models import ApiKey
    name = f"akx_chg_{_uuid.uuid4().hex[:8]}"
    target = None
    try:
        if grant:
            resp = opts.client.post("/api/group/apikey", {
                "group": opts.group.pk, "name": name, "permissions": {perm: True}})
            changed = ApiKey.objects.filter(
                group=opts.group, name=name, permissions__contains={perm: True}).exists()
        else:
            # Made by a trusted internal call, so it holds the permission
            # whatever the map says.
            target, _tok = ApiKey.create_for_group(
                opts.group, name, permissions={perm: True})
            resp = opts.client.post(f"/api/group/apikey/{target.pk}", {
                "permissions": {perm: False}})
            target.refresh_from_db()
            changed = not target.permissions.get(perm)
        return resp.status_code, changed
    finally:
        ApiKey.objects.filter(group=opts.group, name=name).delete()


def _grant(opts, perm):
    """Create a key carrying `perm`. Returns (status, landed); leaves no key."""
    from mojo.apps.account.models import ApiKey
    name = f"akx_key_{_uuid.uuid4().hex[:8]}"
    try:
        resp = opts.client.post("/api/group/apikey", {
            "group": opts.group.pk, "name": name, "permissions": {perm: True}})
        landed = ApiKey.objects.filter(
            group=opts.group, permissions__contains={perm: True}).exclude(
            pk=opts.minter_pk).exists()
        return resp.status_code, landed
    finally:
        ApiKey.objects.filter(group=opts.group).exclude(pk=opts.minter_pk).delete()


def _assert_refused(opts, perm, why):
    status, landed = _grant(opts, perm)
    assert status == 403, \
        f"{why}: a key carrying {perm} must be refused, got {status}: {opts.client.last_response.body}"
    assert not landed, f"{why}: {perm} landed on a key despite the refusal"


def _assert_allowed(opts, perm, why):
    status, landed = _grant(opts, perm)
    assert status == 200, \
        f"{why}: a key carrying {perm} must be allowed, got {status}: {opts.client.last_response.body}"
    assert landed, f"{why}: {perm} must land on the new key"


def _malform_row():
    """Leave a row that arrived by SQL / a queryset update / before the write
    validator existed. resolve() reads Redis first and a queryset update does
    not touch it, so push the stored value explicitly."""
    from mojo.apps.account.models.setting import Setting
    row = Setting.set(KEY, {})
    Setting.objects.filter(pk=row.pk).update(value="not json")
    row.refresh_from_db()
    row.push_to_cache()


@th.django_unit_test("apikey protection floor: an empty-object row cannot remove a file-protected permission")
def test_empty_object_row_keeps_file_floor(opts):
    """(a) THE REGRESSION. On main 8b22829d the `{}` row replaces the file map
    whole and this grant is a 200."""
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(APIKEY_PERMS_PROTECTION=FILE_MAP):
            Setting.set(KEY, {})
            _login(opts, opts.admin_email)
            _assert_refused(opts, FILE_PERM, "row of {} against the file map")
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("apikey protection floor: a row cannot loosen a file-protected permission")
def test_weaker_row_keeps_file_floor(opts):
    """(b)"""
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(APIKEY_PERMS_PROTECTION=FILE_MAP):
            Setting.set(KEY, {FILE_PERM: "manage_group"})
            _login(opts, opts.admin_email)
            _assert_refused(opts, FILE_PERM, "row re-mapping the file key to manage_group")
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("apikey protection floor: a row adding a protected permission is enforced")
def test_row_addition_is_enforced(opts):
    """(c) for a group admin and for a key-backed session."""
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(APIKEY_PERMS_PROTECTION=FILE_MAP):
            Setting.set(KEY, {ROW_PERM: NEVER})
            _login(opts, opts.admin_email)
            _assert_refused(opts, ROW_PERM, "group admin, row-added permission")
            _assert_refused(opts, FILE_PERM, "group admin, file map beside a row addition")
            _assert_allowed(opts, PLAIN_PERM, "group admin, unlisted permission")
            _use_minter_key(opts)
            _assert_refused(opts, ROW_PERM, "key-backed session, row-added permission")
            _assert_refused(opts, FILE_PERM, "key-backed session, file-protected permission")
            _assert_allowed(opts, PLAIN_PERM, "key-backed session, unlisted permission")
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("apikey protection floor: a blank row adds nothing and keeps the file floor")
def test_blank_row_keeps_file_floor(opts):
    """(d)"""
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(APIKEY_PERMS_PROTECTION=FILE_MAP):
            _login(opts, opts.admin_email)
            for blank in ("", "  \n"):
                Setting.set(KEY, blank)
                why = f"blank row {blank!r}"
                _assert_allowed(opts, PLAIN_PERM, why)
                _assert_refused(opts, FILE_PERM, why)
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("apikey protection floor: a malformed row refuses every grant below a global manager")
def test_malformed_row_refuses_grants(opts):
    """(e)"""
    from mojo.apps.account.models import ApiKey
    _clear_row()
    try:
        with th.server_settings(APIKEY_PERMS_PROTECTION=FILE_MAP):
            _malform_row()

            _login(opts, opts.admin_email)
            status, landed = _grant(opts, PLAIN_PERM)
            assert status == 403 and not landed, (
                f"malformed row: a group admin must be refused an unlisted permission, "
                f"got {status}: {opts.client.last_response.body}")
            status, landed = _grant(opts, FILE_PERM)
            assert status == 403 and not landed, (
                f"malformed row: a group admin must be refused a file-protected permission, "
                f"got {status}: {opts.client.last_response.body}")
            # A key that already carries the permission, made by a trusted
            # internal call, for the save that changes no permission.
            unchanged, _tok = ApiKey.create_for_group(
                opts.group, f"akx_unchanged_{opts.suffix}", permissions={PLAIN_PERM: True})
            resp = opts.client.post(f"/api/group/apikey/{unchanged.pk}", {
                "name": "akx_renamed", "permissions": {PLAIN_PERM: True}})
            assert resp.status_code == 200, (
                f"malformed row: a save that changes no permission must still work, "
                f"got {resp.status_code}: {opts.client.last_response.body}")
            unchanged.refresh_from_db()
            assert unchanged.name == "akx_renamed" and unchanged.permissions == {PLAIN_PERM: True}, (
                f"the no-op save must rename the key and leave its permissions alone, "
                f"got {unchanged.name!r} {unchanged.permissions!r}")
            # It carries PLAIN_PERM; remove it so _grant's "landed" reads only new keys.
            unchanged.delete()

            _use_minter_key(opts)
            status, landed = _grant(opts, PLAIN_PERM)
            assert status == 403 and not landed, (
                f"malformed row: a key-backed session must be refused even an unlisted "
                f"permission, got {status}: {opts.client.last_response.body}")

            _login(opts, opts.global_email)
            _assert_allowed(opts, PLAIN_PERM, "malformed row, global manage_groups holder")
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=opts.group).exclude(pk=opts.minter_pk).delete()
        _clear_row()


@th.django_unit_test("apikey protection floor: a malformed settings-file value refuses and logs, with a valid row and with none")
def test_malformed_file_refuses_and_logs(opts):
    """(h) The file side of (e), and the error log on both refusal branches:
    the key-backed one and the one below a global manager. On main 8b22829d the
    file value is coerced to {} and every one of these changes is a 200."""
    from mojo.apps.account.models import ApiKey, GroupMember
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    # An override key ASSUMES its member, so request.user is a real global
    # manager — and the session is still key-backed, so it is refused. The
    # REST layer admits an override key on what its member holds in the group.
    acting = _make_user(
        f"akx_acting_{_uuid.uuid4().hex[:8]}@globalperms.test", perms=["manage_groups"])
    member, _ = GroupMember.objects.get_or_create(user=acting, group=opts.group)
    member.permissions = {"manage_group": True}
    member.save()
    override, override_token = ApiKey.create_for_group(
        opts.group, f"akx_override_{opts.suffix}", permissions={"groups": True},
        user=acting, override_user=True)
    actors = (
        ("a group admin", lambda: _login(opts, opts.admin_email), False),
        ("a global manage_groups holder", lambda: _login(opts, opts.global_email), True),
        ("a reference key", lambda: _use_minter_key(opts), False),
        ("an override key acting as a global manager",
         lambda: _use_key(opts, override_token), False),
    )
    try:
        with th.server_settings(APIKEY_PERMS_PROTECTION=MALFORMED_FILE):
            for row_label, row in (("no row", None), ("a valid row", {ROW_PERM: NEVER})):
                _clear_row()
                if row is not None:
                    Setting.set(KEY, row)
                for who, become, allowed in actors:
                    become()
                    for grant in (True, False):
                        why = (f"malformed file value with {row_label}: {who} "
                               f"{'granting' if grant else 'revoking'} an unlisted permission")
                        mark = _error_log_size()
                        status, changed = _change(opts, PLAIN_PERM, grant)
                        logged = _malformed_logged_since(mark)
                        if allowed:
                            assert status == 200 and changed, (
                                f"{why} must still work, got {status}: "
                                f"{opts.client.last_response.body}")
                            assert logged == 0, \
                                f"{why} is allowed and must log no malformed-map error, found {logged}"
                        else:
                            assert status == 403 and not changed, (
                                f"{why} must be refused, got {status}: "
                                f"{opts.client.last_response.body}")
                            assert logged == 1, \
                                f"{why} must log exactly one malformed-map error, found {logged}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=opts.group).exclude(pk=opts.minter_pk).delete()
        _clear_row()


@th.django_unit_test("apikey protection floor: every write path refuses a malformed, group-scoped or secret value")
def test_writes_refuse_bad_values(opts):
    """(f)"""
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    good = '{"%s": "%s"}' % (ROW_PERM, NEVER)

    def _no_stored_value(why):
        # Setting.set creates a blank row before it validates; a blank row
        # reads as nothing configured. Anything non-blank is a failure.
        left = [row.value for row in Setting.objects.filter(key=KEY) if (row.value or "").strip()]
        assert not left, f"{why}: no non-blank row may be left, found {left!r}"

    _login(opts, opts.settings_admin_email)
    try:
        for payload in ("not json", "null", '["x"]', '{"a": ""}'):
            resp = opts.client.post("/api/settings", {"key": KEY, "value": payload})
            assert resp.status_code == 400, (
                f"{KEY}={payload!r} must be refused at write time, "
                f"got {resp.status_code}: {opts.client.last_response.body}")
            assert not Setting.objects.filter(key=KEY).exists(), \
                f"refused write of {KEY}={payload!r} must not persist a row"
        resp = opts.client.post("/api/settings", {
            "key": KEY, "value": good, "group": opts.group.pk})
        assert resp.status_code == 400, (
            f"a group-scoped {KEY} row must be refused, "
            f"got {resp.status_code}: {opts.client.last_response.body}")
        assert not Setting.objects.filter(key=KEY).exists(), \
            "a refused group-scoped write must not persist a row"
        resp = opts.client.post("/api/settings", {"key": KEY, "value": good, "is_secret": True})
        assert resp.status_code == 400, (
            f"a secret {KEY} row must be refused, "
            f"got {resp.status_code}: {opts.client.last_response.body}")
        assert not Setting.objects.filter(key=KEY).exists(), \
            "a refused secret write must not persist a row"

        resp = opts.client.post("/api/settings", {"key": KEY, "value": good})
        assert resp.status_code == 200, (
            f"a valid map must still save, got {resp.status_code}: "
            f"{opts.client.last_response.body}")
        assert Setting.objects.filter(key=KEY, group=None).exists(), \
            "a valid map must persist a row"
        _clear_row()

        for bad in ("not json", "null", ["x"], {"a": ""}, 5):
            refused = False
            try:
                Setting.set(KEY, bad)
            except Exception:
                refused = True
            assert refused, f"Setting.set of {bad!r} must be refused"
            _no_stored_value(f"Setting.set of {bad!r}")
            _clear_row()
        for label, kwargs in {
            "group-scoped": {"group": opts.group},
            "secret": {"is_secret": True},
        }.items():
            refused = False
            try:
                Setting.set(KEY, {ROW_PERM: NEVER}, **kwargs)
            except Exception:
                refused = True
            assert refused, f"a {label} Setting.set must be refused"
            _no_stored_value(f"{label} Setting.set")
            _clear_row()
        for bad in (None, 5, True, ["x"], {"a": []}, "not json"):
            refused = False
            try:
                Setting(key=KEY, value=bad).save()
            except Exception:
                refused = True
            assert refused, f"a direct save of {bad!r} must be refused"
            assert not Setting.objects.filter(key=KEY).exists(), \
                f"a refused direct save of {bad!r} must not persist a row"
    finally:
        opts.client.logout()
        _clear_row()


@th.django_unit_test("apikey protection floor: the file and a row together cannot relax a built-in")
def test_builtin_survives_file_and_row(opts):
    """(g) Passes on main too: it guards the built-ins, it is not a regression test."""
    from mojo.apps.account.models.setting import Setting
    _clear_row()
    try:
        with th.server_settings(APIKEY_PERMS_PROTECTION={"geoip_sync": "manage_group"}):
            Setting.set(KEY, {"geoip_sync": "manage_group"})
            _login(opts, opts.admin_email)
            _assert_refused(opts, "geoip_sync", "file and row both re-mapping geoip_sync")
    finally:
        opts.client.logout()
        _clear_row()
