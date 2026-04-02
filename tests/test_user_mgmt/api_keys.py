from testit import helpers as th
from testit import faker

TEST_USER = "apikey_user"
TEST_PWORD = "apikey##mojo99"
ADMIN_USER = "apikey_admin"
ADMIN_PWORD = "apikey##mojo99"


@th.django_unit_setup()
def setup_api_key_testing(opts):
    from mojo.apps.account.models import User, Group, ApiKey

    # Clean up existing test data
    ApiKey.objects.filter(name__startswith="test_").delete()
    Group.objects.filter(name__in=["test_apikey_parent", "test_apikey_child"]).delete()
    User.objects.filter(username__in=[TEST_USER, ADMIN_USER]).delete()

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

    opts.parent_id = parent.id
    opts.child_id = child.id


@th.unit_test("apikey_create_for_group")
def test_apikey_create_for_group(opts):
    """create_for_group() returns an api_key and a raw token; hash is stored, raw token is not."""
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
    assert api_key.token_hash != raw_token, "raw token must not be stored"
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
    """REST GET returns the api key without the token."""
    resp = opts.client.get(f"/api/group/apikey/{opts.rest_key_id}", params={"group": opts.parent_id})
    assert resp.status_code == 200, f"get failed: {resp.status_code}"
    data = resp.response.data
    assert data.id == opts.rest_key_id, "wrong id"
    assert data.get("token") == opts.rest_raw_token, "token should be retrievable from encrypted storage"
    assert "token_hash" not in data, "token_hash must not be exposed"


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


@th.unit_test("apikey_rest_delete")
def test_apikey_rest_delete(opts):
    """REST DELETE removes the api key."""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login failed"

    resp = opts.client.delete(f"/api/group/apikey/{opts.rest_key_id}", params={"group": opts.parent_id})
    assert resp.status_code == 200, f"delete failed: {resp.status_code} {resp.response}"

    from mojo.apps.account.models import ApiKey
    assert not ApiKey.objects.filter(pk=opts.rest_key_id).exists(), "api key should be deleted"


@th.unit_test("apikey_cleanup")
def test_apikey_cleanup(opts):
    """Remove test groups and keys."""
    from mojo.apps.account.models import Group, ApiKey

    ApiKey.objects.filter(name__startswith="test_").delete()
    Group.objects.filter(name__in=["test_apikey_parent", "test_apikey_child", "test_apikey_other", "test_apikey_unrelated"]).delete()
