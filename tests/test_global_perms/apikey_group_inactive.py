"""ITEM-037 regression — an ApiKey whose group is deactivated loses group-scoped
access at request time (runtime check, so reactivating the group instantly
restores it — the key is never mutated).

Root cause: `ApiKey.validate_token` set `request.group = api_key.group` with no
`group.is_active` check, so a no-`group=`-param request kept full access to a
deactivated tenant's data. Fix strips group context (fail closed at model
security, matching ITEM-025's "inactive == no group context") rather than a hard
401 — the geoip federation path (requires_global_perms, allow_api_keys) ignores
request.group and must keep working for an inactive-group fleet peer.

Two choke points:
  1. validate_token → request.group = active-group-or-None (list path).
  2. mojo/models/rest.py api_key branch is_active gate (detail/instance re-bind).

Style mirrors tests/test_global_perms/apikey_groupless.py.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_global_perms._helpers import use_apikey


def _mk_group(parent=None):
    from mojo.apps.account.models import Group
    tag = _uuid.uuid4().hex[:8]
    kind = "team" if parent is not None else "organization"
    return Group.objects.create(name=f"ak_ia_{tag}", kind=kind, parent=parent)


@th.django_unit_setup()
def setup_apikey_group_inactive(opts):
    from mojo.apps.account.models import ApiKey, Group

    # Clean slate on the long-lived DB.
    ApiKey.objects.filter(name__startswith="ak_ia_test_").delete()
    Group.objects.filter(name__startswith="ak_ia_").delete()


@th.django_unit_test("validate_token: active group sets context, inactive strips it (still authenticates)")
def test_validate_token_strips_inactive_group(opts):
    from mojo.apps.account.models import ApiKey
    from testit.helpers import get_mock_request

    group = _mk_group()
    key, token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_vt", permissions={"groups": True})
    try:
        # Active group → context granted.
        req = get_mock_request()
        user, err = ApiKey.validate_token(token, req)
        assert err is None, f"active-group token must validate, got error: {err}"
        assert user is not None, "active-group token must return the key identity"
        assert req.group is not None and req.group.pk == group.pk, \
            f"active group must set request.group, got {req.group!r}"

        # Deactivate the tenant.
        group.is_active = False
        group.save()

        # Inactive group → key STILL authenticates (federation path must survive),
        # but group context is stripped.
        req2 = get_mock_request()
        user2, err2 = ApiKey.validate_token(token, req2)
        assert err2 is None, \
            f"inactive group must NOT reject the token (strip-context, not 401): {err2}"
        assert user2 is not None and user2.is_authenticated, \
            "key must still authenticate when its group is inactive"
        assert req2.group is None, \
            f"inactive group must strip request.group to None, got {req2.group!r}"
    finally:
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()


@th.django_unit_test("list: no-group= request against a deactivated tenant is denied")
def test_apikey_list_denied_when_group_inactive(opts):
    from mojo.apps.account.models import ApiKey

    group = _mk_group()
    key, token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_list", permissions={"groups": True})
    try:
        # Active control — a group-scoped read succeeds (Branch A untouched).
        # /api/group/apikey has no global scope (unlike Setting), so it's the
        # decisive target: pre-fix an inactive-group key would list the tenant's
        # own keys; post-fix it must be denied outright.
        use_apikey(opts, token)
        resp = opts.client.get("/api/group/apikey")
        assert resp.status_code == 200, \
            f"active-group key must still list its group's keys, got {resp.status_code}: {opts.client.last_response.body}"
        opts.client.logout()

        # Deactivate → the same no-param request is now denied.
        group.is_active = False
        group.save()
        use_apikey(opts, token)
        resp = opts.client.get("/api/group/apikey")
        assert resp.status_code in (401, 403), \
            f"inactive-group key must be denied (no group= param), got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()


@th.django_unit_test("detail: instance re-bind cannot revive an inactive group's context")
def test_apikey_detail_denied_when_group_inactive(opts):
    """The decisive re-bind proof: a DETAIL read of a row owned by the inactive
    group re-binds request.group from the instance (mojo/models/rest.py) — the
    is_active gate in the api_key branch must still fail it closed. The key reads
    its OWN record, so no extra fixture is needed."""
    from mojo.apps.account.models import ApiKey

    group = _mk_group()
    key, token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_detail", permissions={"groups": True})
    try:
        # Active control — the key reads its own record.
        use_apikey(opts, token)
        resp = opts.client.get(f"/api/group/apikey/{key.pk}")
        assert resp.status_code == 200, \
            f"active-group key must read its own record, got {resp.status_code}: {opts.client.last_response.body}"
        opts.client.logout()

        # Deactivate → detail read (which re-binds request.group from the
        # instance) must now be denied, not 200.
        group.is_active = False
        group.save()
        use_apikey(opts, token)
        resp = opts.client.get(f"/api/group/apikey/{key.pk}")
        assert resp.status_code in (401, 403, 404), \
            f"inactive-group key must be denied on detail re-bind, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()


@th.django_unit_test("reactivation instantly restores a key (no key mutation)")
def test_apikey_restored_on_group_reactivation(opts):
    from mojo.apps.account.models import ApiKey

    group = _mk_group()
    key, token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_react", permissions={"groups": True})
    try:
        # Deactivate → denied.
        group.is_active = False
        group.save()
        use_apikey(opts, token)
        resp = opts.client.get("/api/group/apikey")
        assert resp.status_code in (401, 403), \
            f"deactivated tenant's key must be denied, got {resp.status_code}: {opts.client.last_response.body}"
        opts.client.logout()

        # Reactivate → the SAME token works again immediately (key untouched).
        group.is_active = True
        group.save()
        use_apikey(opts, token)
        resp = opts.client.get("/api/group/apikey")
        assert resp.status_code == 200, \
            f"reactivating the group must instantly restore the key, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()


@th.django_unit_test("active child group under an active parent key is not over-restricted")
def test_apikey_active_child_still_reachable(opts):
    """The fix gates the RESOLVED group per-request; an active child reached via
    explicit group=<child id> with a parent key must still work."""
    from mojo.apps.account.models import ApiKey

    parent = _mk_group()
    child = _mk_group(parent=parent)
    key, token = ApiKey.create_for_group(
        group=parent, name="ak_ia_test_child", permissions={"groups": True})
    try:
        use_apikey(opts, token)
        resp = opts.client.get("/api/group/apikey", params={"group": child.pk})
        assert resp.status_code == 200, \
            f"parent key must still reach an ACTIVE child group, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(pk=key.pk).delete()
        child.delete()
        parent.delete()


@th.django_unit_test("group row: inactive group's key cannot read or self-reactivate its own Group")
def test_group_self_access_denied_when_inactive(opts):
    """Post-build review gap A: Group.check_view/edit_permission gate an ApiKey
    via is_group_allowed (hierarchy-only) and run BEFORE the rest.py is_active
    gate — so an inactive-group key could still GET/PUT /api/group/<own pk>,
    including flipping is_active back (self-reversible suspension)."""
    from mojo.apps.account.models import ApiKey, Group

    group = _mk_group()
    key, token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_selfgrp", permissions={"groups": True})
    try:
        # Active control — the key reads its own Group row.
        use_apikey(opts, token)
        resp = opts.client.get(f"/api/group/{group.pk}")
        assert resp.status_code == 200, \
            f"active-group key must read its own group row, got {resp.status_code}: {opts.client.last_response.body}"
        opts.client.logout()

        group.is_active = False
        group.save()

        # Read of the Group row itself must now be denied.
        use_apikey(opts, token)
        resp = opts.client.get(f"/api/group/{group.pk}")
        assert resp.status_code in (401, 403, 404), \
            f"inactive-group key must not read its own group row, got {resp.status_code}: {opts.client.last_response.body}"

        # The escalation: a suspended tenant's key must NOT be able to
        # reactivate its own group.
        resp = opts.client.post(f"/api/group/{group.pk}", {"is_active": True})
        assert resp.status_code in (401, 403, 404), \
            f"inactive-group key must not write its own group row, got {resp.status_code}: {opts.client.last_response.body}"
        group.refresh_from_db()
        assert group.is_active is False, \
            "SECURITY: a suspended tenant's key reactivated its own group"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()


@th.django_unit_test("requires_perms: a key is trusted only within an ACTIVE group context")
def test_requires_perms_denies_key_without_active_group(opts):
    """Post-build review gap B (in-process — the decorator short-circuits on
    request.user.has_permission BEFORE any group consideration, so a deactivated
    tenant's key kept passing plain @md.requires_perms endpoints, e.g. sms/send).
    validate_token strips request.group for an inactive group; the decorator must
    treat that as no-context and fail closed for a non-User identity."""
    import mojo.errors
    from objict import objict
    from mojo.apps.account.models import ApiKey
    from mojo.decorators.auth import requires_perms, requires_group_perms

    PERM = "itest_ak37_perm"

    @requires_perms(PERM)
    def dummy_perms_view(request):
        return "ran"

    @requires_group_perms(PERM)
    def dummy_group_perms_view(request):
        return "ran"

    group = _mk_group()
    key, _token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_reqperms", permissions={PERM: True})
    key.is_authenticated = True
    try:
        # Control: ACTIVE group context → the key's perm dict authorizes.
        req = objict(user=key, api_key=key, group=group, DATA=objict())
        assert dummy_perms_view(req) == "ran", \
            "active-group key with the perm must pass requires_perms"
        assert dummy_group_perms_view(req) == "ran", \
            "active-group key with the perm must pass requires_group_perms"

        # Inactive group → validate_token yields request.group=None; the key's
        # self-claimed perm must no longer be trusted.
        group.is_active = False
        group.save()
        req2 = objict(user=key, api_key=key, group=None, DATA=objict())
        for view, name in ((dummy_perms_view, "requires_perms"),
                           (dummy_group_perms_view, "requires_group_perms")):
            try:
                result = view(req2)
                assert False, \
                    f"a groupless-context key must not pass {name}, but the view ran: {result!r}"
            except mojo.errors.PermissionDeniedException:
                pass  # fail-closed deny is correct
    finally:
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()


@th.django_unit_test("gate above hooks: a naive instance hook cannot bypass the inactive-group gate")
def test_inactive_group_gate_runs_before_instance_hooks(opts):
    """DM-045 item 1 — the inactive-group invariant must be STRUCTURAL, not
    per-hook convention. A group-scoped model's instance hook that grants via
    bare ``request.api_key.has_permission(perms)`` (the pre-DM-037 Group shape,
    and the obvious pattern for a future model to copy) runs BEFORE the
    api_key branch's is_active gate — without the pre-hook gate this reopens
    the self-reversible-suspension class on detail GET/save/delete."""
    from objict import objict
    from mojo.apps.account.models import ApiKey

    group = _mk_group()
    key, _token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_hookgate", permissions={"groups": True})
    key.is_authenticated = True
    try:
        # ApiKey itself is group-scoped (has a `group` FK) — attach the naive
        # hook as an INSTANCE attribute so the model class stays clean.
        inst = key
        inst.check_view_permission = (
            lambda perms, request: request.api_key.has_permission(perms))

        # Control: active group → the naive hook may grant.
        req = objict(user=key, api_key=key, group=group, DATA=objict())
        allowed, denial = ApiKey._evaluate_permission(req, "VIEW_PERMS", instance=inst)
        assert allowed is True, \
            f"control: active-group key + naive hook must be allowed, got {denial!r}"

        # Deactivate the tenant → the gate must fire BEFORE the hook.
        group.is_active = False
        group.save()
        req2 = objict(user=key, api_key=key, group=None, DATA=objict())
        allowed2, denial2 = ApiKey._evaluate_permission(req2, "VIEW_PERMS", instance=inst)
        assert allowed2 is False, \
            "SECURITY: a naive instance hook granted an api_key access to a row " \
            "owned by an INACTIVE group (hook ran before the is_active gate)"
        assert denial2 is not None and denial2.branch == "api_key.group_inactive", \
            f"denial must come from the pre-hook inactive-group gate, got {denial2!r}"
    finally:
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()


@th.django_unit_test("machine identity without request.api_key fails closed (never the USER branch)")
def test_unregistered_machine_identity_denied(opts):
    """DM-045 item 2 — a custom AUTH_BEARER_HANDLERS identity (mojo/middleware/
    auth.py) that neither defines the ``is_request_user`` marker nor sets
    ``request.api_key`` must be denied outright: routing it to the USER branches
    would authorize its self-claimed has_permission with no group confinement
    and no inactive-group gating (the DM-019/DM-037 protections).

    The fakes are plain classes ON PURPOSE — objict answers hasattr() True for
    every name, which would make a fake look like a real User to the marker
    predicate."""
    from objict import objict
    from mojo.apps.account.models import ApiKey, Group

    class FakeMachineIdentity:
        # deliberately NO is_request_user marker and NO request.api_key
        is_authenticated = True
        username = "custom-bearer:1"

        def has_permission(self, perms):
            return True  # self-claims everything — must never be consulted

    class FakeUserLike(FakeMachineIdentity):
        def is_request_user(self, request=None):
            return True

    machine = FakeMachineIdentity()

    # (a) a groupless model (Group has no group FK / GROUP_FIELD).
    req = objict(user=machine, group=None, DATA=objict())
    allowed, denial = Group._evaluate_permission(req, "VIEW_PERMS")
    assert allowed is False, \
        "SECURITY: an unregistered machine identity reached the USER branch of a groupless model"
    assert denial is not None and denial.branch == "non_user_no_api_key", \
        f"denial must come from the machine-identity guard, got {denial!r}"

    # (b) a group-scoped model with no group context.
    req2 = objict(user=machine, group=None, DATA=objict())
    allowed2, denial2 = ApiKey._evaluate_permission(req2, "VIEW_PERMS")
    assert allowed2 is False, \
        "SECURITY: an unregistered machine identity reached the USER branch of a group-scoped model"
    assert denial2 is not None and denial2.branch == "non_user_no_api_key", \
        f"denial must come from the machine-identity guard, got {denial2!r}"

    # Control: an identity carrying the is_request_user marker still reaches
    # the normal user fallthrough (user.has_permission).
    req3 = objict(user=FakeUserLike(), group=None, DATA=objict())
    allowed3, denial3 = Group._evaluate_permission(req3, "VIEW_PERMS")
    assert allowed3 is True, \
        f"control: a User-marked identity must keep its user.has_permission path, got {denial3!r}"


@th.django_unit_test("guard: ALLOW_API_KEY_GLOBAL is refused on a group-scoped model (fail closed)")
def test_allow_api_key_global_guard_on_group_scoped_model(opts):
    """Secondary hardening: a group-scoped model must never grant an api_key
    global (groupless) access even if it sets RestMeta.ALLOW_API_KEY_GLOBAL=True.
    Tested in-process (opts.client hits a separate server that cannot see a
    monkeypatched RestMeta) via _evaluate_permission directly."""
    from objict import objict
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.account.models.setting import Setting

    group = Group.objects.create(name=f"ak_ia_{_uuid.uuid4().hex[:8]}", kind="organization")
    key, _token = ApiKey.create_for_group(
        group=group, name="ak_ia_test_guard", permissions={"groups": True, "manage_settings": True})
    key.is_authenticated = True

    # api_key identity with NO active group context (the groupless branch).
    req = objict(user=key, api_key=key, group=None, DATA=objict())

    had_attr = "ALLOW_API_KEY_GLOBAL" in Setting.RestMeta.__dict__
    try:
        # Baseline: without the flag, a group-scoped model already denies here.
        allowed, denial = Setting._evaluate_permission(req, "VIEW_PERMS")
        assert allowed is False, \
            "baseline: a group-scoped model must deny a groupless api_key"

        # Dangerous misconfiguration: the flag must be REFUSED (fail closed),
        # not honored, because Setting has a group FK.
        Setting.RestMeta.ALLOW_API_KEY_GLOBAL = True
        allowed2, denial2 = Setting._evaluate_permission(req, "VIEW_PERMS")
        assert allowed2 is False, \
            "guard must refuse ALLOW_API_KEY_GLOBAL on a group-scoped model (fail closed)"
        assert denial2 is not None and denial2.branch == "api_key.groupless_denied", \
            f"denial must come from the groupless-deny branch, got {denial2!r}"
    finally:
        if not had_attr and "ALLOW_API_KEY_GLOBAL" in Setting.RestMeta.__dict__:
            delattr(Setting.RestMeta, "ALLOW_API_KEY_GLOBAL")
        ApiKey.objects.filter(pk=key.pk).delete()
        group.delete()
