"""ITEM-019 regression — a group-scoped ApiKey must not reach GROUPLESS
(platform-global) uses_model_security models cross-tenant.

Root cause: a group admin can set any permission on a key they mint, and
`_evaluate_permission`'s ApiKey branch (mojo/models/rest.py:288) grants on the
key's self-claimed perms when the request is not confined to a group (the model
has no `group` FK, so `on_rest_list` cannot group-filter). The fix makes that
branch deny keys by default (RestMeta `ALLOW_API_KEY_GLOBAL` opt-in, off), and
gates ApiKey.permissions assignment (APIKEY_PERMS_PROTECTION).

Style mirrors tests/test_global_perms/apikey_gate.py (ITEM-018).
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_global_perms._helpers import use_apikey


# Broad perm set covering every groupless endpoint's VIEW/SAVE perms.
BROAD_PERMS = {
    "users": True, "manage_users": True, "view_users": True,
    "security": True, "manage_security": True, "view_security": True,
    "jobs": True, "manage_jobs": True, "view_jobs": True,
    "files": True, "manage_files": True, "view_fileman": True,
    "groups": True, "manage_groups": True, "manage_group": True,
    "view_scheduled_tasks": True, "manage_scheduled_tasks": True,
}

# Groupless uses_model_security endpoints (list paths). The decision is made in
# _evaluate_permission before any handler work, so a bogus detail pk is fine.
GROUPLESS_LIST_ENDPOINTS = [
    "/api/user",
    "/api/system/geoip",
    "/api/account/logins",
    "/api/account/api_keys",
    # NOTE: /api/group (list) is intentionally NOT here — Group's custom list
    # handler confines a key to its OWN groups (ApiKey.get_groups), so a 200
    # there is correct, not a leak. Cross-tenant Group access (detail by pk) is
    # covered separately in test_apikey_group_detail_confined.
    "/api/jobs/job",
    "/api/jobs/event",
    "/api/jobs/scheduled_task",
    "/api/account/bouncer/device",
    "/api/account/bouncer/signature",
    # NOTE: /api/fileman/rendition is intentionally NOT here — FileRendition is
    # group-scoped through RestMeta.GROUP_FIELD="original_file__group" (it has
    # no direct `group` FK). Now that the permission layer honors GROUP_FIELD,
    # a group-scoped key reaches only its OWN group's renditions (on_rest_list
    # filters original_file__group=key.group), which is correct, not a leak —
    # so a 200 there is expected. Cross-tenant confinement is asserted in
    # tests/test_fileman/9_test_rendition_group_field.py.
]


@th.django_unit_setup()
def setup_apikey_groupless(opts):
    from mojo.apps.account.models import User, Group, ApiKey

    ApiKey.objects.filter(name__startswith="gpless_test_").delete()
    Group.objects.filter(name__startswith="gpless_grp_").delete()

    # A uniquely-named victim user in NO relation to the key's group. Its email
    # must never appear in a key's /api/user response.
    tag = _uuid.uuid4().hex[:10]
    opts.victim_email = f"gpless_victim_{tag}@globalperms.test"
    victim = User.objects.create_user(username=opts.victim_email, email=opts.victim_email, password="Gpless##99")
    opts.victim_id = victim.pk

    group = Group.objects.create(name=f"gpless_grp_{tag}", kind="organization")
    _, opts.token = ApiKey.create_for_group(
        group=group, name="gpless_test_key", permissions=dict(BROAD_PERMS))
    opts.group_id = group.pk


@th.django_unit_test("groupless: apikey with broad perms is denied on every groupless model")
def test_apikey_denied_on_groupless_models(opts):
    use_apikey(opts, opts.token)
    tested = 0
    failures = []
    try:
        for path in GROUPLESS_LIST_ENDPOINTS:
            resp = opts.client.get(path)
            code = resp.status_code
            if code == 404:
                continue  # endpoint/app not routed in this testproject
            tested += 1
            if code not in (401, 403):
                failures.append(f"{path} -> {code}: {str(opts.client.last_response.body)[:160]}")
    finally:
        opts.client.logout()
    assert not failures, \
        "apikey reached a groupless model (cross-tenant NOT closed):\n" + "\n".join(failures)
    assert tested >= 6, f"too few groupless endpoints exercised ({tested}) — routing regression?"


@th.django_unit_test("groupless: apikey /api/user leaks NO other-tenant user data")
def test_apikey_user_body_no_leak(opts):
    """The decisive check — status AND body. A denied request must not contain
    the victim's email, and a targeted ?email= lookup must also be denied."""
    v = opts.victim_email
    use_apikey(opts, opts.token)
    try:
        resp = opts.client.get("/api/user")
        body = str(opts.client.last_response.body)
        assert resp.status_code in (401, 403), \
            f"apikey list /api/user must be denied, got {resp.status_code}: {body[:200]}"
        assert v not in body, f"SECURITY: victim email leaked in /api/user body: {body[:300]}"

        resp = opts.client.get(f"/api/user?email={v}")
        body = str(opts.client.last_response.body)
        assert resp.status_code in (401, 403), \
            f"apikey targeted /api/user?email lookup must be denied, got {resp.status_code}"
        assert v not in body, f"SECURITY: victim email leaked in targeted lookup: {body[:300]}"

        # Detail-by-pk: User.check_edit_permission is consulted for VIEW too, so
        # this path bypasses the list gate — it must ALSO deny the key.
        resp = opts.client.get(f"/api/user/{opts.victim_id}")
        body = str(opts.client.last_response.body)
        assert resp.status_code in (401, 403, 404), \
            f"apikey GET /api/user/<pk> must be denied, got {resp.status_code}: {body[:200]}"
        assert v not in body, f"SECURITY: victim email leaked in detail-by-pk: {body[:300]}"
    finally:
        opts.client.logout()


@th.django_unit_test("groupless: apikey with NO group perms cannot WRITE its own group")
def test_apikey_cannot_write_own_group_without_perm(opts):
    """Group.check_view_permission gates SAVE too — a key must hold the perm, not
    just be confined to its own group, to write it (else a zero-perm key could
    rewrite auth_config/geofence)."""
    from mojo.apps.account.models import Group, ApiKey
    grp = Group.objects.create(name=f"gpless_wgrp_{_uuid.uuid4().hex[:8]}", kind="organization")
    _, token = ApiKey.create_for_group(group=grp, name="gpless_test_noperm", permissions={})  # zero grants
    try:
        use_apikey(opts, token)
        resp = opts.client.post(f"/api/group/{grp.pk}", {"metadata": {"geofence": {"country": {"in": ["US"]}}}})
        assert resp.status_code in (401, 403), \
            f"zero-perm key must NOT write its own group, got {resp.status_code}: {opts.client.last_response.body}"
        grp.refresh_from_db()
        assert not (grp.metadata or {}).get("geofence"), \
            "SECURITY: zero-perm key wrote group.metadata.geofence"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=grp).delete()
        grp.delete()


@th.django_unit_test("groupless: apikey cannot read ANOTHER group's detail, only its own")
def test_apikey_group_detail_confined(opts):
    """Group has a custom check_view_permission — a key must be confined to its
    own group (detail by pk), never read another tenant's group."""
    from mojo.apps.account.models import Group, ApiKey
    victim = Group.objects.create(name=f"gpless_othergrp_{_uuid.uuid4().hex[:8]}", kind="organization")
    own = Group.objects.create(name=f"gpless_owngrp_{_uuid.uuid4().hex[:8]}", kind="organization")
    _, token = ApiKey.create_for_group(group=own, name="gpless_test_grpdetail", permissions={"groups": True})
    try:
        use_apikey(opts, token)
        resp = opts.client.get(f"/api/group/{victim.pk}")
        assert resp.status_code in (401, 403), \
            f"key must NOT read another group's detail, got {resp.status_code}: {str(opts.client.last_response.body)[:200]}"
        assert victim.name not in str(opts.client.last_response.body), \
            "SECURITY: another tenant's group name leaked to a key"
        # Its OWN group detail is fine.
        resp = opts.client.get(f"/api/group/{own.pk}")
        assert resp.status_code == 200, \
            f"key must still read its OWN group, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=own).delete()
        victim.delete()
        own.delete()


@th.django_unit_test("groupless: apikey STILL reaches its own group's group-scoped models")
def test_apikey_group_scoped_still_works(opts):
    """Fix must not over-restrict: a key with a group-scoped perm still reads a
    group-FK model (Setting), confined to its group (Branch A untouched)."""
    from mojo.apps.account.models import Group, ApiKey
    grp = Group.objects.create(name=f"gpless_ok_{_uuid.uuid4().hex[:8]}", kind="organization")
    _, token = ApiKey.create_for_group(group=grp, name="gpless_test_ok", permissions={"groups": True})
    try:
        use_apikey(opts, token)
        resp = opts.client.get("/api/settings")
        assert resp.status_code == 200, \
            f"key with 'groups' must still read its group's settings (Branch A), got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        ApiKey.objects.filter(group=grp).delete()
        grp.delete()


@th.django_unit_test("groupless: APIKEY_PERMS_PROTECTION gates key-permission assignment")
def test_apikey_perms_protection(opts):
    """A group admin (member manage_group, no global) cannot assign a protected
    perm to a key they create; an unlisted perm still lands. Also proves the
    DB-backed map is read with kind='dict' (a 403, not a 500)."""
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey
    from mojo.apps.account.models.setting import Setting
    from mojo.decorators.limits import clear_rate_limits

    PROT = "itest_ak_prot"
    Setting.remove("APIKEY_PERMS_PROTECTION")
    Setting.set("APIKEY_PERMS_PROTECTION", {PROT: "sys.itest_never_held"})  # stored as JSON string

    tag = _uuid.uuid4().hex[:8]
    email = f"gpless_admin_{tag}@globalperms.test"
    pw = "Gpless##adm99"
    admin = User.objects.create_user(username=email, email=email, password=pw)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    grp = Group.objects.create(name=f"gpless_prot_{tag}", kind="organization")
    m, _ = GroupMember.objects.get_or_create(user=admin, group=grp)
    m.permissions = {"manage_group": True}  # NO global manage_users/manage_groups
    m.save()

    try:
        clear_rate_limits(ip="127.0.0.1", key="login")
        assert opts.client.login(email, pw), f"admin login failed: {opts.client.last_response.body}"

        # Protected perm → denied.
        resp = opts.client.post("/api/group/apikey", {
            "group": grp.pk, "name": "k_protected", "permissions": {PROT: True}})
        assert resp.status_code == 403, \
            f"assigning a protected perm to a key must be denied, got {resp.status_code}: {opts.client.last_response.body}"
        assert not ApiKey.objects.filter(group=grp, permissions__contains={PROT: True}).exists(), \
            "protected perm must not have landed on any key"

        # Unlisted perm → allowed.
        resp = opts.client.post("/api/group/apikey", {
            "group": grp.pk, "name": "k_ok", "permissions": {"some_group_perm": True}})
        assert resp.status_code == 200, \
            f"unlisted perm must still be assignable, got {resp.status_code}: {opts.client.last_response.body}"
        key = ApiKey.objects.filter(group=grp, name="k_ok").first()
        assert key is not None and key.permissions.get("some_group_perm") is True, \
            f"unlisted perm must land, got {key.permissions if key else None}"
    finally:
        opts.client.logout()
        Setting.remove("APIKEY_PERMS_PROTECTION")
        ApiKey.objects.filter(group=grp).delete()
        GroupMember.objects.filter(group=grp).delete()
        grp.delete()
        admin.delete()


@th.django_unit_test("groupless: assistant memory tier check does not 500 on an ApiKey identity")
def test_memory_tier_check_apikey_no_crash(opts):
    """_can_read_tier/_can_write_tier used `user.is_superuser` — an ApiKey has no
    such attribute. Must be a clean bool, not an AttributeError/500."""
    try:
        from mojo.apps.assistant.services.memory import _can_read_tier, _can_write_tier
    except ImportError:
        return  # assistant app not installed in this testproject
    from mojo.apps.account.models import Group, ApiKey
    grp = Group.objects.create(name=f"gpless_mem_{_uuid.uuid4().hex[:8]}", kind="organization")
    key, _ = ApiKey.create_for_group(group=grp, name="gpless_test_mem", permissions={"assistant": True})
    try:
        for fn in (_can_read_tier, _can_write_tier):
            for tier in ("global", "user", "group"):
                result = fn(tier, key, group=grp)  # must not raise
                assert isinstance(result, bool), f"{fn.__name__}({tier}) must return bool, got {result!r}"
    finally:
        ApiKey.objects.filter(group=grp).delete()
        grp.delete()
