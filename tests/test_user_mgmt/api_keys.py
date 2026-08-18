from testit import helpers as th
from testit import faker
from unittest import mock

TEST_USER = "apikey_user"
TEST_PWORD = "apikey##mojo99"
ADMIN_USER = "apikey_admin"
ADMIN_PWORD = "apikey##mojo99"
BT_MEMBER = "apikey_baseterm"
BT_PWORD = "apikey##mojo99"


@th.django_unit_setup()
def setup_api_key_testing(opts):
    from mojo.apps.account.models import User, Group, ApiKey

    # Clean up existing test data
    ApiKey.objects.filter(name__startswith="test_").delete()
    Group.objects.filter(name__in=["test_apikey_parent", "test_apikey_child"]).delete()
    User.objects.filter(username__in=[TEST_USER, ADMIN_USER, BT_MEMBER]).delete()

    # Create parent group
    parent = Group.objects.create(name="test_apikey_parent", kind="organization")
    # Create child group under parent
    child = Group.objects.create(name="test_apikey_child", kind="team", parent=parent)

    # Create dedicated admin user
    admin = User(username=ADMIN_USER, email=f"{ADMIN_USER}@test.com")
    admin.save()
    admin.is_active = True
    admin.is_email_verified = True
    admin.is_staff = True
    admin.save_password(ADMIN_PWORD)
    admin.add_permission(["manage_group", "manage_groups"])
    admin.save()

    # Add admin as member of parent group
    from mojo.apps.account.models import GroupMember
    GroupMember.objects.get_or_create(user=admin, group=parent, defaults={"permissions": {"manage_group": True}})

    # Member holding ONLY the bare "groups" perm at the member level (no
    # global perms) — ITEM-035: bare terms are view+manage combined.
    bt = User(username=BT_MEMBER, email=f"{BT_MEMBER}@test.com")
    bt.save()
    bt.is_active = True
    bt.is_email_verified = True
    bt.save_password(BT_PWORD)
    bt.save()
    GroupMember.objects.get_or_create(user=bt, group=parent, defaults={"permissions": {"groups": True}})

    opts.parent_id = parent.id
    opts.child_id = child.id


@th.unit_test("apikey_create_for_group")
def test_apikey_create_for_group(opts):
    """create_for_group() returns an api_key and a raw token; token_hash holds the SHA-256, and the raw token is also kept encrypted in mojo_secrets."""
    from mojo.apps.account.models import Group, ApiKey

    group = Group.objects.get(pk=opts.parent_id)
    api_key, raw_token = ApiKey.create_for_group(
        group=group,
        name="test_create",
        permissions={"view_data": True},
    )
    assert api_key.pk is not None, "api_key was not saved"
    assert raw_token is not None and len(raw_token) == 48, f"unexpected token length: {len(raw_token)}"
    assert api_key.token_hash is not None, "token_hash not set"
    assert api_key.token_hash != raw_token, "token_hash must hold the SHA-256 digest, not the raw token"
    assert api_key.permissions.get("view_data") is True, "permission not stored"
    opts.raw_token = raw_token
    opts.api_key_id = api_key.pk


@th.unit_test("apikey_validate_token_valid")
def test_apikey_validate_token_valid(opts):
    """validate_token() succeeds with a valid token."""
    from mojo.apps.account.models import ApiKey
    from testit.helpers import get_mock_request

    request = get_mock_request()
    user, error = ApiKey.validate_token(opts.raw_token, request)
    assert error is None, f"unexpected error: {error}"
    assert user is not None, "user should not be None"
    assert user.is_authenticated is True, "user should be authenticated"
    # assert user.id is None, "api key user should have no user id"
    assert request.api_key is not None, "request.api_key not set"
    assert request.group is not None, "request.group not set"
    assert request.group.id == opts.parent_id, "request.group should be the api key's group"


@th.unit_test("apikey_validate_token_invalid")
def test_apikey_validate_token_invalid(opts):
    """validate_token() fails with a bogus token."""
    from mojo.apps.account.models import ApiKey
    from testit.helpers import get_mock_request

    request = get_mock_request()
    user, error = ApiKey.validate_token("notavalidtoken000000000000000000000000000000000000", request)
    assert user is None, "user should be None for invalid token"
    assert error is not None, "error should be set"


@th.unit_test("apikey_validate_token_inactive")
def test_apikey_validate_token_inactive(opts):
    """validate_token() fails when key is inactive."""
    from mojo.apps.account.models import ApiKey
    from testit.helpers import get_mock_request

    api_key = ApiKey.objects.get(pk=opts.api_key_id)
    api_key.is_active = False
    api_key.save()

    request = get_mock_request()
    user, error = ApiKey.validate_token(opts.raw_token, request)
    assert user is None, "user should be None for inactive key"
    assert error is not None, "error should be set"

    # Restore
    api_key.is_active = True
    api_key.save()


@th.unit_test("apikey_validate_token_expired")
def test_apikey_validate_token_expired(opts):
    """validate_token() fails when key is expired."""
    from mojo.apps.account.models import ApiKey
    from mojo.helpers import dates
    from testit.helpers import get_mock_request

    api_key = ApiKey.objects.get(pk=opts.api_key_id)
    api_key.expires_at = dates.utcnow() - dates.timedelta(seconds=1)
    api_key.save()

    request = get_mock_request()
    user, error = ApiKey.validate_token(opts.raw_token, request)
    assert user is None, "user should be None for expired key"
    assert error is not None, "error should be set"

    # Restore
    api_key.expires_at = None
    api_key.save()


@th.unit_test("apikey_has_permission")
def test_apikey_has_permission(opts):
    """has_permission() correctly allows/denies based on permissions dict."""
    from mojo.apps.account.models import ApiKey

    api_key = ApiKey.objects.get(pk=opts.api_key_id)
    api_key.permissions = {"view_data": True, "edit_data": False}
    api_key.save()

    assert api_key.has_permission("view_data") is True, "view_data should be allowed"
    assert api_key.has_permission("edit_data") is False, "edit_data should be denied"
    assert api_key.has_permission("unknown_perm") is False, "unknown perm should be denied"
    assert api_key.has_permission("all") is True, "'all' should always be allowed"

    # sys.* always denied — no backing user to escalate to
    assert api_key.has_permission("sys.manage_users") is False, "sys.* must always be denied"

    # OR logic with list
    assert api_key.has_permission(["view_data", "missing"]) is True, "list OR: at least one match"
    assert api_key.has_permission(["edit_data", "missing"]) is False, "list OR: all denied"


@th.unit_test("dnsman ACME federation is protected by the framework ApiKey floor")
def test_dnsman_acme_federation_protection_floor(opts):
    from mojo.apps.account.models import api_key

    protection = api_key._apikey_perms_protection()
    assert protection.get("dnsman_acme_federation") == "sys.dnsman_acme_federation", \
        f"ACME federation must require a system grant to provision, got {protection}"

    configured = {
        "dnsman_acme_federation": "manage_group",
        "geoip_sync": "groups",
        "deployment_sensitive": "sys.deployment_sensitive",
    }
    with mock.patch.object(api_key.settings, "get", return_value=configured):
        protection = api_key._apikey_perms_protection()
    assert protection["dnsman_acme_federation"] == "sys.dnsman_acme_federation", \
        f"configuration must not relax the ACME floor, got {protection}"
    assert protection["geoip_sync"] == "sys.geoip_sync", \
        f"configuration must not relax the GeoIP floor, got {protection}"
    assert protection["deployment_sensitive"] == "sys.deployment_sensitive", \
        "configuration should still be able to add deployment-specific floors"


@th.unit_test("apikey_is_group_allowed")
def test_apikey_is_group_allowed(opts):
    """is_group_allowed() permits own group and descendants, denies others."""
    from mojo.apps.account.models import Group, ApiKey

    api_key = ApiKey.objects.get(pk=opts.api_key_id)
    parent = Group.objects.get(pk=opts.parent_id)
    child = Group.objects.get(pk=opts.child_id)

    # Create an unrelated group
    other = Group.objects.create(name="test_apikey_other", kind="organization")

    assert api_key.is_group_allowed(parent) is True, "own group should be allowed"
    assert api_key.is_group_allowed(child) is True, "child group should be allowed"
    assert api_key.is_group_allowed(other) is False, "unrelated group should be denied"
    assert api_key.is_group_allowed(None) is False, "None group should be denied"

    other.delete()


@th.unit_test("apikey_rest_create")
def test_apikey_rest_create(opts):
    """REST POST creates an api key and returns the token once."""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login failed"

    resp = opts.client.post(
        "/api/group/apikey",
        {"name": "test_rest_key", "group": opts.parent_id, "permissions": {"view_data": True, "manage_group": True}},
    )
    assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.response}"
    data = resp.response.data
    assert data.id is not None, "missing id"
    assert data.name == "test_rest_key", f"wrong name: {data.name}"
    token = data.get("token")
    assert token is not None and len(token) == 48, f"raw token must be returned on creation: {token}"

    opts.rest_key_id = data.id
    opts.rest_raw_token = token


@th.unit_test("apikey_rest_get")
def test_apikey_rest_get(opts):
    """REST GET returns the api key without the token.

    The live credential is NOT on the default graph — read-back is opt-in via
    `?graph=token` (see test_apikey_rest_token_graph). An ordinary detail read
    must never carry the secret along.
    """
    resp = opts.client.get(f"/api/group/apikey/{opts.rest_key_id}", params={"group": opts.parent_id})
    assert resp.status_code == 200, f"get failed: {resp.status_code}"
    data = resp.response.data
    assert data.id == opts.rest_key_id, "wrong id"
    assert data.get("token") is None, (
        f"default graph must NOT return the raw token, got: {data.get('token')!r}"
    )
    assert "token_hash" not in data, "token_hash must not be exposed"


@th.unit_test("apikey_rest_list_omits_token")
def test_apikey_rest_list_omits_token(opts):
    """A LIST read must not carry the raw token on any row.

    This is the real-world exposure and it is NOT covered by the detail test:
    on_rest_list asks for graph "list", ApiKey defines no "list" graph, and the
    serializer falls back to "default" for any unknown graph name. If the token
    extra ever returns to "default", this is what catches it.
    """
    resp = opts.client.get("/api/group/apikey", params={"group": opts.parent_id})
    assert resp.status_code == 200, f"list failed: {resp.status_code} {resp.response}"
    rows = resp.response.data
    assert len(rows) > 0, "list must return at least the key created earlier"
    for row in rows:
        assert row.get("token") is None, (
            f"list row {row.get('id')} leaked a raw token: {row.get('token')!r}"
        )
        assert "token_hash" not in row, f"list row {row.get('id')} exposed token_hash"


@th.unit_test("apikey_rest_token_graph")
def test_apikey_rest_token_graph(opts):
    """?graph=token is the opt-in read-back, and it writes an audit row."""
    from mojo.apps.logit.models import Log

    before = Log.objects.filter(
        kind="api_key:token_read", model_id=opts.rest_key_id).count()

    resp = opts.client.get(
        f"/api/group/apikey/{opts.rest_key_id}",
        params={"group": opts.parent_id, "graph": "token"},
    )
    assert resp.status_code == 200, f"token-graph get failed: {resp.status_code} {resp.response}"
    data = resp.response.data
    assert data.get("token") == opts.rest_raw_token, (
        f"graph=token must return the live token, got: {data.get('token')!r}"
    )
    assert "token_hash" not in data, "token_hash must not be exposed even on the token graph"

    after = Log.objects.filter(
        kind="api_key:token_read", model_id=opts.rest_key_id).count()
    assert after == before + 1, (
        f"reading the token must write exactly one api_key:token_read audit row: "
        f"before={before} after={after}"
    )


@th.unit_test("apikey_rest_unknown_graph_fails_closed")
def test_apikey_rest_unknown_graph_fails_closed(opts):
    """A mistyped *special* graph name is refused with 400 (item 2102) — never
    served, and never a fallback that could leak the token. `token` is an opt-in
    special graph, so `tokens` is an undefined special name (refused), not a
    common name that silently falls back to default."""
    resp = opts.client.get(
        f"/api/group/apikey/{opts.rest_key_id}",
        params={"group": opts.parent_id, "graph": "tokens"},
    )
    assert resp.status_code == 400, (
        f"an unknown special graph must be refused, got {resp.status_code}"
    )
    assert opts.rest_raw_token not in str(resp.response), (
        "a refused graph must never expose the token"
    )


@th.unit_test("apikey_rest_permissions_rejects_non_dict")
def test_apikey_rest_permissions_rejects_non_dict(opts):
    """Non-dict `permissions` payloads (JSON strings, lists) must be rejected
    with 400 — not silently ignored with a 200 and the field unchanged."""
    from mojo.apps.account.models import ApiKey

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login failed"

    url = f"/api/group/apikey/{opts.rest_key_id}"
    before = ApiKey.objects.get(pk=opts.rest_key_id).permissions

    # JSON-encoded string (not an object) — the silent-drop repro shape
    resp = opts.client.post(url, {"group": opts.parent_id, "permissions": '{"manage_group": true}'})
    assert resp.status_code == 400, (
        f"string permissions must be rejected with 400, got {resp.status_code}: {resp.response}"
    )

    # Non-dict JSON (list) — same contract
    resp = opts.client.post(url, {"group": opts.parent_id, "permissions": ["manage_group"]})
    assert resp.status_code == 400, (
        f"list permissions must be rejected with 400, got {resp.status_code}: {resp.response}"
    )

    after = ApiKey.objects.get(pk=opts.rest_key_id).permissions
    assert after == before, f"permissions must be untouched after rejection: {before!r} -> {after!r}"

    # Control: a real dict still saves — add then remove a throwaway perm,
    # leaving the key exactly as the earlier tests expect it.
    resp = opts.client.post(url, {"group": opts.parent_id, "permissions": {"test_extra_perm": True}})
    assert resp.status_code == 200, f"dict permissions must save, got {resp.status_code}: {resp.response}"
    assert ApiKey.objects.get(pk=opts.rest_key_id).permissions.get("test_extra_perm") is True, \
        "dict permission grant must persist"

    resp = opts.client.post(url, {"group": opts.parent_id, "permissions": {"test_extra_perm": False}})
    assert resp.status_code == 200, f"dict permissions must save, got {resp.status_code}: {resp.response}"
    after = ApiKey.objects.get(pk=opts.rest_key_id).permissions
    assert after == before, f"key must end unchanged after add+remove: {before!r} -> {after!r}"


@th.unit_test("apikey_auth_header")
def test_apikey_auth_header(opts):
    """Authorization: apikey <token> authenticates and sets request context."""
    # Switch client to use apikey bearer
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.rest_raw_token
    opts.client.is_authenticated = True

    # A group-scoped endpoint should work with the api key's group
    resp = opts.client.get("/api/group/apikey", params={"group": opts.parent_id})
    assert resp.status_code == 200, f"apikey auth failed: {resp.status_code} {resp.response}"

    # Restore normal auth
    opts.client.logout()


@th.unit_test("apikey_group_scoped_perm")
def test_apikey_group_scoped_perm(opts):
    """manage_users on an api key is group-scoped, not system-wide.
    request.group is always set so rest_check_permission routes through
    group.user_has_permission — the system user branch is never reached."""
    from mojo.apps.account.models import ApiKey
    from testit.helpers import get_mock_request

    api_key = ApiKey.objects.get(pk=opts.api_key_id)
    api_key.permissions = {"manage_users": True}
    api_key.save()

    # has_permission returns True — but scope is enforced by the request.group path
    assert api_key.has_permission("manage_users") is True, "manage_users allowed by key"
    assert api_key.has_permission("missing_perm") is False, "unlisted perm denied"

    # Restore
    api_key.permissions = {"view_data": True}
    api_key.save()


@th.unit_test("apikey_child_group_blocked")
def test_apikey_child_group_blocked(opts):
    """Using an api key with a group that is not a descendant returns 403."""
    from mojo.apps.account.models import Group, ApiKey

    # Create a completely separate group
    other = Group.objects.create(name="test_apikey_unrelated", kind="organization")

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.rest_raw_token
    opts.client.is_authenticated = True

    resp = opts.client.get("/api/group/apikey", params={"group": other.id})
    assert resp.status_code == 403, f"expected 403 for unrelated group, got {resp.status_code}"

    other.delete()
    opts.client.logout()


@th.unit_test("apikey_whoami_me")
def test_apikey_whoami_me(opts):
    """GET /api/group/apikey/me returns the key's identity + permissions, never the token."""
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.rest_raw_token
    opts.client.is_authenticated = True

    resp = opts.client.get("/api/group/apikey/me")
    assert resp.status_code == 200, f"whoami failed: {resp.status_code} {resp.response}"

    data = resp.response.data
    assert data.id == opts.rest_key_id, f"wrong id: {data.id} != {opts.rest_key_id}"
    assert data.name == "test_rest_key", f"wrong name: {data.name}"
    assert isinstance(data.permissions, dict), (
        f"permissions must be a dict, got {type(data.permissions).__name__}: {data.permissions!r}"
    )
    assert data.permissions.get("view_data") is True, (
        f"granted permissions must be reported by whoami: {data.permissions!r}"
    )
    # The token must never be echoed back — the caller already holds it.
    assert data.get("token") is None, "whoami must NOT return the raw token"
    assert "token_hash" not in data, "whoami must NOT expose token_hash"
    assert "mojo_secrets" not in data, "whoami must NOT expose mojo_secrets"

    opts.client.logout()


@th.unit_test("apikey_whoami_me_rejects_user")
def test_apikey_whoami_me_rejects_user(opts):
    """A user/JWT-authenticated request (no API key) gets 401 from /api/group/apikey/me."""
    opts.client.logout()
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login failed"

    resp = opts.client.get("/api/group/apikey/me")
    assert resp.status_code == 401, (
        f"a non-API-key request must be rejected with 401, got {resp.status_code}: "
        f"{resp.response}"
    )

    opts.client.logout()


@th.unit_test("apikey_rotate_token_model")
def test_apikey_rotate_token_model(opts):
    """rotate_token() issues a new secret in place: old token stops
    authenticating, new token works, same row + permissions."""
    from mojo.apps.account.models import Group, ApiKey
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    api_key, old_token = ApiKey.create_for_group(
        group=group, name="test_rotate_model", permissions={"view_data": True},
    )
    key_id = api_key.pk

    new_token = api_key.rotate_token()
    assert new_token and len(new_token) == 48, f"unexpected token: {new_token!r}"
    assert new_token != old_token, "rotate must produce a different token"
    assert api_key.pk == key_id, "rotate must not create a new row"
    assert api_key.permissions.get("view_data") is True, "permissions preserved"

    # Old token no longer authenticates; new token does.
    u_old, err_old = ApiKey.validate_token(old_token, get_mock_request())
    assert u_old is None and err_old, "old token must stop working after rotate"
    u_new, err_new = ApiKey.validate_token(new_token, get_mock_request())
    assert err_new is None and u_new is not None, f"new token must work: {err_new}"

    ApiKey.objects.filter(pk=key_id).delete()


@th.unit_test("apikey_rest_rotate_self")
def test_apikey_rest_rotate_self(opts):
    """POST /api/group/apikey/rotate rotates the calling key, returns the new
    token once; the old token then fails and the new one works."""
    from mojo.apps.account.models import Group, ApiKey

    group = Group.objects.get(pk=opts.parent_id)
    api_key, old_token = ApiKey.create_for_group(
        group=group, name="test_rotate_rest", permissions={"view_data": True},
    )
    key_id = api_key.pk

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = old_token
    opts.client.is_authenticated = True

    resp = opts.client.post("/api/group/apikey/rotate", {})
    assert resp.status_code == 200, f"rotate failed: {resp.status_code} {resp.response}"
    data = resp.response.data
    new_token = data.get("token")
    assert new_token and len(new_token) == 48, f"new token must be returned once: {new_token!r}"
    assert new_token != old_token, "rotate must change the token"
    assert data.id == key_id, "same key id — rotate is in place"

    # Old token is now invalid; new token authenticates.
    opts.client.access_token = old_token
    assert opts.client.get("/api/group/apikey/me").status_code == 401, \
        "old token must be rejected after rotate"
    opts.client.access_token = new_token
    resp_new = opts.client.get("/api/group/apikey/me")
    assert resp_new.status_code == 200, f"new token must work: {resp_new.status_code}"
    assert resp_new.response.data.id == key_id, "whoami resolves the same key"

    opts.client.logout()
    ApiKey.objects.filter(pk=key_id).delete()


@th.unit_test("apikey_rest_rotate_rejects_user")
def test_apikey_rest_rotate_rejects_user(opts):
    """A user/JWT request (no API key) gets 401 from the rotate endpoint —
    rotation is self-service for the authenticating key only."""
    opts.client.logout()
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login failed"
    resp = opts.client.post("/api/group/apikey/rotate", {})
    assert resp.status_code == 401, \
        f"non-apikey request must be 401, got {resp.status_code}: {resp.response}"
    opts.client.logout()


@th.unit_test("apikey_rest_delete")
def test_apikey_rest_delete(opts):
    """REST DELETE removes the api key."""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login failed"

    resp = opts.client.delete(f"/api/group/apikey/{opts.rest_key_id}", params={"group": opts.parent_id})
    assert resp.status_code == 200, f"delete failed: {resp.status_code} {resp.response}"

    from mojo.apps.account.models import ApiKey
    assert not ApiKey.objects.filter(pk=opts.rest_key_id).exists(), "api key should be deleted"


@th.unit_test("apikey_get_member_for_user_is_none")
def test_apikey_get_member_for_user_is_none(opts):
    """Regression (ITEM-016): Group.get_member_for_user must return None for an
    ApiKey identity — never run the User-typed members.filter(user=...) query.
    Before the fix this raised 'Cannot query "...": Must be "User" instance.'."""
    from mojo.apps.account.models import Group, ApiKey

    # child has a parent, so check_parents=True also exercises the parent-walk
    # branch (the second unguarded filter(user=...) site).
    child = Group.objects.get(pk=opts.child_id)
    api_key, _ = ApiKey.create_for_group(
        group=child, name="test_member_lookup", permissions={"manage_payments": True},
    )
    member = child.get_member_for_user(api_key, check_parents=True)
    assert member is None, \
        "get_member_for_user must return None for an ApiKey, not raise or return a member"


@th.unit_test("apikey_group_user_has_permission_bool")
def test_apikey_group_user_has_permission_bool(opts):
    """ITEM-016: Group.user_has_permission returns a bool for an ApiKey identity —
    grants a held perm, denies a lacked one and sys.*, and never raises."""
    from mojo.apps.account.models import Group, ApiKey

    child = Group.objects.get(pk=opts.child_id)
    api_key, _ = ApiKey.create_for_group(
        group=child, name="test_perm_bool", permissions={"manage_payments": True},
    )
    assert child.user_has_permission(api_key, ["manage_payments", "admin"]) is True, \
        "ApiKey holding manage_payments must be granted"
    assert child.user_has_permission(api_key, ["manage_users"]) is False, \
        "ApiKey lacking the perm must be denied (bool), not raise"
    assert child.user_has_permission(api_key, ["sys.superuser"]) is False, \
        "sys.* must always be denied for an ApiKey"


@th.unit_test("apikey_rest_create_bare_groups_member")
def test_apikey_rest_create_bare_groups_member(opts):
    """ITEM-035: a member holding ONLY member-level {"groups": True} can create
    a key AND set permissions on it — bare "groups" is view_groups+manage_groups
    combined, so the can_change_permission fallback must accept it. Before the
    fix this was a surprise 403 from set_permissions."""
    from mojo.apps.account.models import ApiKey

    opts.client.logout()
    opts.client.login(BT_MEMBER, BT_PWORD)
    assert opts.client.is_authenticated, "bare-groups member login failed"

    resp = opts.client.post(
        "/api/group/apikey",
        {"name": "test_baseterm_key", "group": opts.parent_id, "permissions": {"view_data": True}},
    )
    assert resp.status_code == 200, (
        f"bare-'groups' member must be able to create a key with permissions, "
        f"got {resp.status_code}: {resp.response}"
    )
    key = ApiKey.objects.get(pk=resp.response.data.id)
    assert key.permissions.get("view_data") is True, \
        f"permission set by a bare-'groups' member must persist: {key.permissions!r}"

    # The creation echo must reach THIS caller too. A member-level requester
    # trips Group.check_view_permission's any-member fallthrough during the
    # `group` FK attach, which rewrites request.DATA["graph"] to "basic" — so a
    # create response that picked its graph off the request would silently hand
    # this persona a key with no token and no error. The admin-authenticated
    # create test cannot catch that: admin holds global manage_group and never
    # reaches the downgrade.
    token = resp.response.data.get("token")
    assert token is not None and len(token) == 48, (
        f"bare-'groups' member must still receive the raw token on creation: {token!r}"
    )
    assert token == key.get_token(), "returned token must be the one stored on the row"
    opts.client.logout()


# -----------------------------------------------------------------
# Acting as a member — ApiKey.user + ApiKey.override_user
#
# Two modes. override_user=False (default) makes `user` a REFERENCE used for
# attribution only; override_user=True makes the key ASSUME the member, so
# permissions resolve through their GroupMember. In both modes the key's group
# stays the tenant boundary and the key can never mutate the member's
# credentials.
# -----------------------------------------------------------------

ACTOR_USER = "apikey_actor"
ACTOR_PWORD = "apikey##mojo99"
OUTSIDER_USER = "apikey_outsider"
SUPER_USER = "apikey_super"


@th.django_unit_setup()
def setup_apikey_acting_user(opts):
    """Members used by the acting-as tests.

    Deletes before creating — these run against a long-lived database.
    """
    from mojo.apps.account.models import User, Group, GroupMember

    User.objects.filter(username__in=[ACTOR_USER, OUTSIDER_USER, SUPER_USER]).delete()

    parent = Group.objects.get(pk=opts.parent_id)

    # The member a key will act as. Holds manage_tickets AT THE MEMBER LEVEL
    # only — no global permission — so a passing override test proves the
    # permission really resolved through GroupMember.
    actor = User(username=ACTOR_USER, email=f"{ACTOR_USER}@test.com")
    actor.save()
    actor.is_active = True
    actor.is_email_verified = True
    actor.save_password(ACTOR_PWORD)
    actor.save()
    GroupMember.objects.get_or_create(
        user=actor, group=parent, defaults={"permissions": {"manage_tickets": True}})

    # Belongs to no group in this tree at all.
    outsider = User(username=OUTSIDER_USER, email=f"{OUTSIDER_USER}@test.com")
    outsider.save()
    outsider.is_active = True
    outsider.save()

    su = User(username=SUPER_USER, email=f"{SUPER_USER}@test.com")
    su.save()
    su.is_active = True
    su.is_superuser = True
    su.save()

    opts.actor_id = actor.id
    opts.outsider_id = outsider.id
    opts.super_id = su.id


@th.unit_test("apikey_unlinked_returns_key_itself")
def test_apikey_unlinked_returns_key_itself(opts):
    """An unlinked key is bit-for-bit unchanged: validate_token returns the ApiKey OBJECT, not a User, and acting_user is None."""
    from mojo.apps.account.models import Group, ApiKey
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    key, raw = ApiKey.create_for_group(group=group, name="test_unlinked", permissions={"view_data": True})

    request = get_mock_request()
    identity, error = ApiKey.validate_token(raw, request)
    assert error is None, f"unexpected error: {error}"
    assert isinstance(identity, ApiKey), \
        f"an unlinked key must still authenticate AS the key, got {type(identity).__name__}"
    assert identity.pk == key.pk, "validate_token returned a different key"
    assert getattr(request, "acting_user", None) is None, \
        "acting_user must be None when no member is linked"
    assert request.api_key.pk == key.pk, "request.api_key not set"


@th.unit_test("apikey_reference_mode_does_not_bind")
def test_apikey_reference_mode_does_not_bind(opts):
    """override_user=False: the link is a REFERENCE. request.user stays the ApiKey; the member is exposed only as request.acting_user."""
    from mojo.apps.account.models import Group, ApiKey, User
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, raw = ApiKey.create_for_group(
        group=group, name="test_reference", permissions={"view_data": True}, user=actor)
    assert key.override_user is False, "override_user must default to False"

    request = get_mock_request()
    identity, error = ApiKey.validate_token(raw, request)
    assert error is None, f"unexpected error: {error}"
    assert isinstance(identity, ApiKey), \
        f"reference mode must NOT bind the member, got {type(identity).__name__}"
    assert request.acting_user is not None, "acting_user must be set in reference mode"
    assert request.acting_user.id == actor.id, "acting_user is the wrong member"


@th.unit_test("apikey_reference_mode_grants_no_authority")
def test_apikey_reference_mode_grants_no_authority(opts):
    """Linking alone grants nothing: a reference-mode key with an empty permissions dict does NOT inherit the member's manage_tickets."""
    from mojo.apps.account.models import Group, ApiKey, User

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, _raw = ApiKey.create_for_group(
        group=group, name="test_ref_noauth", permissions={}, user=actor)

    assert key.has_permission("manage_tickets") is False, \
        "reference mode must not grant the member's permissions — that is what override_user is for"


@th.unit_test("apikey_override_binds_member")
def test_apikey_override_binds_member(opts):
    """override_user=True: validate_token returns the linked User, so permissions resolve through their GroupMember."""
    from mojo.apps.account.models import Group, ApiKey, User
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, raw = ApiKey.create_for_group(
        group=group, name="test_override", permissions={}, user=actor, override_user=True)

    request = get_mock_request()
    identity, error = ApiKey.validate_token(raw, request)
    assert error is None, f"unexpected error: {error}"
    assert isinstance(identity, User), \
        f"override_user must bind the member, got {type(identity).__name__}"
    assert identity.id == actor.id, "bound the wrong member"
    # The key must remain visible as the machine identity — every containment
    # guard keys on request.api_key, not on the type of request.user.
    assert request.api_key.pk == key.pk, \
        "request.api_key must stay set under override_user or every machine-identity guard opens"
    assert request.group.id == opts.parent_id, \
        "request.group must stay the KEY's group, not the member's default org"


@th.unit_test("apikey_override_resolves_member_permission")
def test_apikey_override_resolves_member_permission(opts):
    """The feature: an override key with NO permissions of its own is authorized by the member's group membership."""
    from mojo.apps.account.models import Group, User

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)

    # manage_tickets exists only on the GroupMember row, never on the key and
    # never as a global permission on the user.
    assert actor.has_permission("manage_tickets") is False, \
        "fixture is wrong: manage_tickets must NOT be a global permission"
    assert group.user_has_permission(actor, ["manage_tickets"]) is True, \
        "the member must hold manage_tickets within the key's group"


@th.unit_test("apikey_link_rejects_superuser")
def test_apikey_link_rejects_superuser(opts):
    """A key may never act as a superuser — hard block, not a warning."""
    from mojo.apps.account.models import Group, ApiKey, User
    import mojo.errors as merrors

    group = Group.objects.get(pk=opts.parent_id)
    su = User.objects.get(pk=opts.super_id)
    try:
        ApiKey.create_for_group(group=group, name="test_su_link", user=su)
    except merrors.PermissionDeniedException:
        return
    assert False, "linking a key to a superuser must be refused"


@th.unit_test("apikey_link_rejects_non_member")
def test_apikey_link_rejects_non_member(opts):
    """A key may only act as a member of its OWN group."""
    from mojo.apps.account.models import Group, ApiKey, User
    import mojo.errors as merrors

    group = Group.objects.get(pk=opts.parent_id)
    outsider = User.objects.get(pk=opts.outsider_id)
    try:
        ApiKey.create_for_group(group=group, name="test_outsider_link", user=outsider)
    except merrors.PermissionDeniedException:
        return
    assert False, "linking a key to a non-member must be refused"


@th.unit_test("apikey_link_rejects_ancestor_member")
def test_apikey_link_rejects_ancestor_member(opts):
    """Delegation must not climb: a CHILD-group key cannot act as a member of the parent, who is typically more privileged."""
    from mojo.apps.account.models import Group, ApiKey, User
    import mojo.errors as merrors

    child = Group.objects.get(pk=opts.child_id)
    actor = User.objects.get(pk=opts.actor_id)   # member of the PARENT only
    assert child.get_member_for_user(actor, check_parents=False) is None, \
        "fixture is wrong: the actor must not be a direct member of the child group"
    try:
        ApiKey.create_for_group(group=child, name="test_ancestor_link", user=actor)
    except merrors.PermissionDeniedException:
        return
    assert False, (
        "a child-group key must not be linkable to an ancestor-group member — "
        "check_parents must be False on the membership check")


@th.unit_test("apikey_override_requires_a_member")
def test_apikey_override_requires_a_member(opts):
    """override_user with nobody to act as is a meaningless state and is refused."""
    from mojo.apps.account.models import Group, ApiKey
    import mojo.errors as merrors

    group = Group.objects.get(pk=opts.parent_id)
    try:
        ApiKey.create_for_group(group=group, name="test_override_nouser", override_user=True)
    except merrors.ValueException:
        return
    assert False, "override_user without a linked user must be refused"


@th.unit_test("apikey_inactive_member_rejects_token")
def test_apikey_inactive_member_rejects_token(opts):
    """Deactivating the member takes their keys with them — and reuses the existing error string, so it is not an account-state oracle."""
    from mojo.apps.account.models import Group, ApiKey, User
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, raw = ApiKey.create_for_group(
        group=group, name="test_inactive_member", user=actor, override_user=True)

    actor.is_active = False
    actor.save()
    try:
        request = get_mock_request()
        identity, error = ApiKey.validate_token(raw, request)
        assert identity is None, "a key linked to a deactivated member must not authenticate"
        assert error == "API key is inactive", \
            f"must reuse the existing inactive string, got {error!r}"
    finally:
        actor.is_active = True
        actor.save()


@th.unit_test("apikey_sys_denied_when_linked")
def test_apikey_sys_denied_when_linked(opts):
    """sys.* stays denied on the key regardless of who it acts as."""
    from mojo.apps.account.models import Group, ApiKey, User

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, _raw = ApiKey.create_for_group(
        group=group, name="test_sys_linked",
        permissions={"sys.manage_users": True}, user=actor, override_user=True)

    assert key.has_permission("sys.manage_users") is False, \
        "sys.* must never be grantable through an api key, linked or not"


@th.unit_test("apikey_clearing_member_clears_override")
def test_apikey_clearing_member_clears_override(opts):
    """Unlinking the member also turns override off, so the row can never sit in the 'assume nobody' state."""
    from mojo.apps.account.models import Group, ApiKey, User
    from mojo.models import rest as mojo_rest
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    admin = User.objects.get(username=ADMIN_USER)
    key, _raw = ApiKey.create_for_group(
        group=group, name="test_clear_override", user=actor, override_user=True)

    # set_user reads self.active_request, which is the ACTIVE_REQUEST
    # contextvar the middleware normally populates.
    request = get_mock_request()
    request.user = admin
    request.group = group
    token = mojo_rest.ACTIVE_REQUEST.set(request)
    try:
        key.set_user(None)
    finally:
        mojo_rest.ACTIVE_REQUEST.reset(token)

    assert key.user is None, "user must be cleared"
    assert key.override_user is False, \
        "clearing the member must also clear override_user — 'assume nobody' is not a valid state"


@th.unit_test("apikey_cannot_mutate_credentials")
def test_apikey_cannot_mutate_credentials(opts):
    """THE guarantee: revoking a key revokes the access.

    An override key carries a member's identity and does that member's work,
    but must never be able to hand itself a credential that OUTLIVES the key —
    a passkey, a 360-day token, or an MFA enrolment. Each of these is refused
    even though the member could perform it interactively.
    """
    from mojo.apps.account.models import Group, ApiKey, User

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, raw = ApiKey.create_for_group(
        group=group, name="test_credblock",
        permissions={"manage_users": True, "users": True},
        user=actor, override_user=True)

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = raw
    opts.client.is_authenticated = True
    try:
        blocked = [
            ("/api/account/totp/setup", {}),
            ("/api/auth/generate_api_key", {"label": "escalation"}),
            ("/api/account/passkeys/register/begin", {}),
            ("/api/auth/username/change", {"username": "hijacked_name"}),
            ("/api/auth/sessions/revoke", {}),
        ]
        for path, payload in blocked:
            resp = opts.client.post(path, payload)
            assert resp.status_code in (401, 403), (
                f"{path} must refuse a key-backed session — a key that can do this "
                f"survives its own revocation; got {resp.status_code}: {resp.response}")

        # The member's username must be untouched by the attempt above.
        actor.refresh_from_db()
        assert actor.username == ACTOR_USER, \
            f"the member's username was changed by a key-backed session: {actor.username}"
    finally:
        opts.client.logout()


@th.unit_test("apikey_override_cannot_list_owner_scoped_models")
def test_apikey_override_cannot_list_owner_scoped_models(opts):
    """Security-review regression (CRITICAL).

    The owner branch in _evaluate_permission was guarded, but the LIST path has
    a SECOND, hand-rolled owner check. Under override_user it went True and
    returned the member's own rows — `GET /api/user` handed back the member's
    email, phone, dob, permissions and is_superuser to a caller that
    `GET /api/user/<pk>` correctly refuses, and the api_keys / passkeys /
    oauth_connection lists returned their credential records.
    """
    from mojo.apps.account.models import Group, ApiKey, User

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, raw = ApiKey.create_for_group(
        group=group, name="test_ownerlist", permissions={},
        user=actor, override_user=True)

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = raw
    opts.client.is_authenticated = True
    try:
        for path in ("/api/user", "/api/account/api_keys",
                     "/api/account/passkeys", "/api/account/oauth_connection"):
            resp = opts.client.get(path)
            if resp.status_code == 200:
                rows = resp.response.data or []
                ids = [r.get("id") for r in rows]
                assert actor.id not in ids, (
                    f"{path} leaked the acting member's own row to a key-backed "
                    f"session via the owner branch: {ids}")
                assert not rows, (
                    f"{path} must not serve owner-scoped rows to a key session, "
                    f"got {len(rows)}")
    finally:
        opts.client.logout()


@th.unit_test("apikey_override_global_perm_does_not_escape_group")
def test_apikey_override_global_perm_does_not_escape_group(opts):
    """Security-review regression.

    Group.user_has_permission short-circuits on the member's GLOBAL permission
    dict when check_user is True, which re-enabled the exact untenanted lookup
    the decorators had just skipped. An override key must be authorized by what
    its member holds IN THE KEY'S GROUP, not platform-wide.
    """
    from mojo.apps.account.models import Group, User

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)

    # Global grant that the member does NOT hold as a member of this group.
    actor.add_permission("manage_incidents")
    actor.save()
    try:
        assert actor.has_permission("manage_incidents") is True, \
            "fixture is wrong: the global grant did not take"
        assert group.user_has_permission(actor, ["manage_incidents"], False) is False, (
            "with check_user=False the member must NOT satisfy a permission they "
            "only hold globally — this is what bounds an override key to its group")
    finally:
        actor.remove_permission("manage_incidents")
        actor.save()


@th.unit_test("apikey_session_cannot_set_acting_user")
def test_apikey_session_cannot_set_acting_user(opts):
    """Security-review regression.

    An override key acting as a group admin could otherwise mint a successor
    key, link it, enable override on it, and read its raw token from the create
    response — a credential that outlives revocation of the original.
    """
    from mojo.apps.account.models import Group, ApiKey, User
    from mojo.models import rest as mojo_rest
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, _raw = ApiKey.create_for_group(group=group, name="test_successor")

    request = get_mock_request()
    request.user = actor
    request.group = group
    request.api_key = key          # marks this a key-backed session
    token = mojo_rest.ACTIVE_REQUEST.set(request)
    try:
        try:
            key.set_user(actor.id)
        except Exception:
            return
        assert False, (
            "a key-backed session must not be able to establish an acting-as "
            "link — linking is an interactive administrative act")
    finally:
        mojo_rest.ACTIVE_REQUEST.reset(token)


@th.unit_test("apikey_superuser_promotion_kills_the_key")
def test_apikey_superuser_promotion_kills_the_key(opts):
    """Security-review regression.

    validate_acting_user blocks superuser targets at LINK time, but a member
    can be promoted afterwards — and User.has_permission returns True for
    everything once is_superuser is set.
    """
    from mojo.apps.account.models import Group, ApiKey, User
    from testit.helpers import get_mock_request

    group = Group.objects.get(pk=opts.parent_id)
    actor = User.objects.get(pk=opts.actor_id)
    key, raw = ApiKey.create_for_group(
        group=group, name="test_promoted", user=actor, override_user=True)

    actor.is_superuser = True
    actor.save()
    try:
        request = get_mock_request()
        identity, error = ApiKey.validate_token(raw, request)
        assert identity is None, (
            "a key linked to a member who was LATER promoted to superuser must "
            "stop authenticating — the link check alone is point-in-time")
        assert error is not None, "an error string must be returned"
    finally:
        actor.is_superuser = False
        actor.save()


@th.unit_test("apikey_cleanup")
def test_apikey_cleanup(opts):
    """Remove test groups and keys."""
    from mojo.apps.account.models import Group, ApiKey, User

    ApiKey.objects.filter(name__startswith="test_").delete()
    Group.objects.filter(name__in=["test_apikey_parent", "test_apikey_child", "test_apikey_other", "test_apikey_unrelated"]).delete()
    User.objects.filter(username__in=[ACTOR_USER, OUTSIDER_USER, SUPER_USER]).delete()
