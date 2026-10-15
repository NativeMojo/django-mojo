"""Maestro item #6363 — two group guards read a group API key's own list.

Changing `metadata.geofence_strict`, disabling or reactivating a group, and
writing `metadata.protected` asked `request.user.has_permission(...)`. For a
group API key in reference mode that is the key's own list, which a
member-level manager fills in; for a key or group token acting as a member it
is that member's global list. Now:

- the posture change and disable/reactivate need a signed-in person holding the
  global permission (`Group._has_global_permission`, the `requires_global_perms`
  bar);
- `admin_compliance` and `admin_verify` are on the API-key protection floor, so
  a member-level manager can no longer put them on a key;
- a key or group token acting as a member never writes protected settings.

Fixtures are made by ORM, with their own groups, members and keys; no shared
production code is patched.
"""
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "bug"

PASSWORD = "Ggk##guard99"
PREFIX = "ggk_"
PROTECTED = {"plan": "original"}

USERS = ("mgr", "sec", "verify", "global_mg", "super", "plain")


def _email(name):
    return f"{PREFIX}{name}@account.test"


def _mk_user(name, **attrs):
    from mojo.apps.account.models import User
    email = _email(name)
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    for key, value in attrs.items():
        setattr(user, key, value)
    user.save()
    return user


def _member(group, user, *perms):
    from mojo.apps.account.models import GroupMember
    group.add_member(user)
    member = GroupMember.objects.get(group=group, user=user)
    for perm in perms:
        member.add_permission(perm)
    return member


def _user(name):
    from mojo.apps.account.models import User
    return User.objects.get(email=_email(name))


def _restore(opts):
    """Put every fixture group back: active, with its original metadata."""
    from mojo.apps.account.models import Group
    for pk, metadata in opts.shape.items():
        Group.objects.filter(pk=pk).update(is_active=True, metadata=metadata)


def _login(opts, name):
    opts.client.logout()
    ok = opts.client.login(_email(name), PASSWORD)
    assert ok, f"login failed for {name}: {opts.client.last_response.body}"


def _use_key(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _key(group, name, **kwargs):
    from mojo.apps.account.models import ApiKey
    _, token = ApiKey.create_for_group(group=group, name=f"{PREFIX}{name}", **kwargs)
    return token


def _metadata(pk):
    from mojo.apps.account.models import Group
    return Group.objects.filter(pk=pk).values_list("metadata", flat=True).first() or {}


def _is_active(pk):
    from mojo.apps.account.models import Group
    return Group.objects.filter(pk=pk).values_list("is_active", flat=True).first()


def _post(opts, pk, payload, **kwargs):
    return opts.client.post(f"/api/group/{pk}", payload, **kwargs)


def _posture_refused(opts, pk, what, value=True):
    before = _metadata(pk).get("geofence_strict")
    resp = _post(opts, pk, {"metadata": {"geofence_strict": value}})
    assert resp.status_code == 403, \
        f"{what} must not change geofence_strict, got {resp.status_code}: {opts.client.last_response.body}"
    after = _metadata(pk).get("geofence_strict")
    assert after == before, f"SECURITY: {what} changed geofence_strict from {before!r} to {after!r}"


def _protected_refused(opts, pk, what, **kwargs):
    resp = _post(opts, pk, {"metadata": {"protected": {"plan": "stolen"}}}, **kwargs)
    assert resp.status_code == 403, \
        f"{what} must not write metadata.protected, got {resp.status_code}: {opts.client.last_response.body}"
    stored = _metadata(pk).get("protected")
    assert stored == PROTECTED, f"SECURITY: {what} changed metadata.protected to {stored!r}"


@th.django_unit_setup()
def setup_group_global_guard_keys(opts):
    from mojo.apps.account.models import ApiKey, Group, User

    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__startswith=PREFIX).delete()
    for _ in range(3):  # children first: parent is a cascading foreign key
        Group.objects.filter(name__startswith=PREFIX, groups__isnull=True).delete()

    def group(name, parent=None):
        return Group.objects.create(
            name=f"{PREFIX}{name}", kind="organization", is_active=True,
            parent=parent, metadata={"motto": name, "protected": dict(PROTECTED)})

    top = group("top")
    child = group("child", top)
    opts.ids = {"top": top.pk, "child": child.pk}
    opts.shape = {g.pk: dict(g.metadata) for g in (top, child)}

    # A member-level manager of the tenant: no global permission at all.
    _member(top, _mk_user("mgr"), "manage_group", "manage_members")
    # Members who hold a global permission themselves, so a key can act as them.
    # sec can also save the group as a member, so its posture change reaches the guard.
    sec = _mk_user("sec")
    sec.add_permission("security")
    _member(top, sec, "manage_group")
    verify = _mk_user("verify")
    verify.add_permission("admin_verify")
    _member(top, verify)
    global_mg = _mk_user("global_mg")
    global_mg.add_permission("manage_groups")
    _member(top, global_mg)
    _member(top, _mk_user("super", is_superuser=True))
    _mk_user("plain")


# ---------------------------------------------------------------------------
# N1, N2, N3: the strict geofence posture
# ---------------------------------------------------------------------------

@th.django_unit_test("#6363 N1: a key a member-level manager mints with security or manage_geofence cannot change the posture")
def test_minted_key_cannot_change_posture(opts):
    from mojo.apps.account.models import ApiKey
    _restore(opts)
    top = opts.ids["top"]
    try:
        for perm in ("security", "manage_geofence"):
            name = f"{PREFIX}minted_{perm}_{_uuid.uuid4().hex[:6]}"
            _login(opts, "mgr")
            resp = opts.client.post("/api/group/apikey", {
                "group": top, "name": name,
                "permissions": {"manage_group": True, perm: True}})
            assert resp.status_code == 200, \
                f"the manager must still be able to mint a key carrying {perm}, " \
                f"got {resp.status_code}: {opts.client.last_response.body}"
            key = ApiKey.objects.get(name=name)
            _use_key(opts, key.get_token())
            resp = _post(opts, top, {"metadata": {"motto": f"minted {perm}"}})
            assert resp.status_code == 200, \
                f"the key must still save its own group, got {resp.status_code}: {opts.client.last_response.body}"
            _posture_refused(opts, top, f"a minted key carrying {perm}")
    finally:
        opts.client.logout()
        ApiKey.objects.filter(name__startswith=PREFIX).delete()


@th.django_unit_test("#6363 N2: a key acting as a member who holds global security cannot change the posture")
def test_member_acting_key_cannot_change_posture(opts):
    from mojo.apps.account.models import ApiKey
    _restore(opts)
    top = opts.ids["top"]
    try:
        # Set up the way #8034 still allows: by trusted code, not by the manager.
        token = _key(opts_group(top), "acts_as_sec", permissions={"manage_group": True},
                     user=_user("sec"), override_user=True)
        _use_key(opts, token)
        _posture_refused(opts, top, "a key acting as a global security holder")

        # What #8034 guarantees: the manager cannot make a key act as them.
        _login(opts, "mgr")
        name = f"{PREFIX}mgr_override"
        resp = opts.client.post("/api/group/apikey", {
            "group": top, "name": name, "user": _user("sec").pk, "override_user": True})
        assert resp.status_code in (400, 403), \
            f"a member-level manager must not make a key act as another member, " \
            f"got {resp.status_code}: {opts.client.last_response.body}"
        assert not ApiKey.objects.filter(name=name, override_user=True).exists(), \
            "SECURITY: a key acting as another member exists after the refusal"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(name__startswith=PREFIX).delete()


@th.django_unit_test("#6363 N3: a person with global security can still set, change and clear the posture")
def test_person_with_security_changes_posture(opts):
    _restore(opts)
    top = opts.ids["top"]
    _login(opts, "sec")
    try:
        for value in (True, False, None):
            resp = _post(opts, top, {"metadata": {"geofence_strict": value}})
            assert resp.status_code == 200, \
                f"a global security holder must set geofence_strict={value!r}, " \
                f"got {resp.status_code}: {opts.client.last_response.body}"
            assert _metadata(top).get("geofence_strict") == value, \
                f"geofence_strict={value!r} did not persist: {_metadata(top)!r}"
    finally:
        opts.client.logout()
        _restore(opts)


def opts_group(pk):
    from mojo.apps.account.models import Group
    return Group.objects.get(pk=pk)


# ---------------------------------------------------------------------------
# N4: disable and reactivate
# ---------------------------------------------------------------------------

@th.django_unit_test("#6363 N4: a parent group's key carrying manage_groups cannot disable or reactivate a child")
def test_parent_key_cannot_disable_or_reactivate(opts):
    from mojo.apps.account.models import ApiKey, Group
    _restore(opts)
    top, child = opts.ids["top"], opts.ids["child"]
    try:
        _use_key(opts, _key(opts_group(top), "parent_mg", permissions={"manage_groups": True}))
        resp = _post(opts, child, {"metadata": {"motto": "parent key"}})
        assert resp.status_code == 200, \
            f"the parent's key must still save the child, got {resp.status_code}: {opts.client.last_response.body}"

        resp = _post(opts, child, {"disable": {"reason": "admin", "note": "key"}})
        assert resp.status_code == 403, \
            f"a group key must not disable a group, got {resp.status_code}: {opts.client.last_response.body}"
        assert _is_active(child) is True, "SECURITY: a group key disabled the child group"

        Group.objects.filter(pk=child).update(is_active=False)
        resp = _post(opts, child, {"reactivate": {"note": "key"}})
        assert resp.status_code == 403, \
            f"a group key must not reactivate a group, got {resp.status_code}: {opts.client.last_response.body}"
        assert _is_active(child) is False, "SECURITY: a group key reactivated the child group"

        # A person with global manage_groups still can.
        _restore(opts)
        _login(opts, "global_mg")
        resp = _post(opts, child, {"disable": {"reason": "admin", "note": "staff"}})
        assert resp.status_code == 200 and _is_active(child) is False, \
            f"a global manage_groups holder must disable, got {resp.status_code}: {opts.client.last_response.body}"
        resp = _post(opts, child, {"reactivate": {"note": "staff"}})
        assert resp.status_code == 200 and _is_active(child) is True, \
            f"a global manage_groups holder must reactivate, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(name__startswith=PREFIX).delete()
        _restore(opts)


# ---------------------------------------------------------------------------
# N5: protected settings
# ---------------------------------------------------------------------------

@th.django_unit_test("#6363 N5: only staff put admin_verify or admin_compliance on a key, and member-acting keys never write protected settings")
def test_protected_settings_keys(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.account.services import group_token
    _restore(opts)
    top = opts.ids["top"]
    try:
        # A member-level manager can no longer grant either permission.
        for perm in ("admin_verify", "admin_compliance"):
            _login(opts, "mgr")
            name = f"{PREFIX}mgr_{perm}"
            resp = opts.client.post("/api/group/apikey", {
                "group": top, "name": name, "permissions": {perm: True}})
            assert resp.status_code == 403, \
                f"a member-level manager must not grant {perm}, got {resp.status_code}: {opts.client.last_response.body}"
            assert not ApiKey.objects.filter(name=name).exists(), \
                f"SECURITY: a key carrying {perm} exists after the refusal"

        # Keys and a group token acting as a member are refused, even as a superuser.
        acting = (
            ("a key acting as a global admin_verify holder", "verify"),
            ("a key acting as a superuser", "super"),
        )
        for what, name in acting:
            _use_key(opts, _key(opts_group(top), f"acts_as_{name}",
                                permissions={"manage_group": True},
                                user=_user(name), override_user=True))
            _protected_refused(opts, top, what)
        opts.client.logout()
        token = group_token.mint(_user("super"), opts_group(top))
        _protected_refused(opts, top, "a group token acting as a superuser",
                           headers={"Authorization": f"grouptoken {token}"})

        # Staff can still grant it, and a staff-made key still writes (option 1).
        _login(opts, "global_mg")
        name = f"{PREFIX}staff_verify"
        resp = opts.client.post("/api/group/apikey", {
            "group": top, "name": name, "permissions": {"admin_verify": True}})
        assert resp.status_code == 200, \
            f"a global manage_groups holder must grant admin_verify, got {resp.status_code}: {opts.client.last_response.body}"
        _use_key(opts, ApiKey.objects.get(name=name).get_token())
        resp = _post(opts, top, {"metadata": {"protected": {"plan": "staff"}}})
        assert resp.status_code == 200, \
            f"a staff-made key carrying admin_verify must still write, got {resp.status_code}: {opts.client.last_response.body}"
        assert _metadata(top).get("protected", {}).get("plan") == "staff", \
            f"the staff key's protected write did not persist: {_metadata(top)!r}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(name__startswith=PREFIX).delete()
        _restore(opts)


# ---------------------------------------------------------------------------
# N6: the shared test on its own
# ---------------------------------------------------------------------------

@th.django_unit_test("#6363 N6: _has_global_permission passes only a signed-in person's own global grant")
def test_has_global_permission_cases(opts):
    from objict import objict
    from mojo.apps.account.models import ApiKey
    from mojo.models import rest as mojo_rest
    group = opts_group(opts.ids["top"])
    perms = ["manage_geofence", "security"]
    sec, plain = _user("sec"), _user("plain")
    try:
        _key(group, "n6_group", permissions={"security": True})
        _key(group, "n6_acting", permissions={}, user=sec, override_user=True)
        group_key = ApiKey.objects.get(name=f"{PREFIX}n6_group")
        acting_key = ApiKey.objects.get(name=f"{PREFIX}n6_acting")

        def request(user, **extra):
            fields = dict(user=user, api_key=None, group_token=None, user_api_key=None)
            fields.update(extra)
            return objict(**fields)

        cases = (
            ("no request", None, False),
            ("a group key carrying security", request(group_key, api_key=group_key), False),
            ("a key acting as a security holder", request(sec, api_key=acting_key), False),
            ("a group token for a security holder",
             request(sec, group_token=objict(override_user=True)), False),
            ("a person's own API key", request(sec, user_api_key=objict(pk=1, label="")), True),
            ("a person without the permission", request(plain), False),
            ("a person with it", request(sec), True),
            ("a superuser", request(_user("super")), True),
        )
        for what, req, expected in cases:
            token = mojo_rest.ACTIVE_REQUEST.set(req)
            try:
                got = group._has_global_permission(perms)
            finally:
                mojo_rest.ACTIVE_REQUEST.reset(token)
            assert bool(got) is expected, f"{what}: _has_global_permission gave {got!r}, expected {expected!r}"
    finally:
        ApiKey.objects.filter(name__startswith=PREFIX).delete()
