from testit import helpers as th
from testit import faker

# Use the same test users as other tests
TEST_USER = "testit"
TEST_PWORD = "testit##mojo"
ADMIN_USER = "tadmin"
ADMIN_PWORD = "testit##mojo"


@th.django_unit_setup()
def setup_metrics_permissions_testing(opts):
    """
    Setup test data for metrics permissions testing.
    """
    from mojo.apps.account.models import User
    from mojo.apps import metrics

    # Clean up any existing test permissions
    test_accounts = ["test_account_1", "test_account_2", "group_123"]
    for account in test_accounts:
        metrics.set_view_perms(account, None)
        metrics.set_write_perms(account, None)

    # Give admin manage_metrics permission
    admin = User.objects.get(username=ADMIN_USER)
    admin.add_permission("manage_metrics")
    admin.save()

    # Ensure test user doesn't have manage_metrics permission
    user = User.objects.get(username=TEST_USER)
    user.remove_permission("manage_metrics")
    user.save()

    opts.admin_user_id = admin.id
    opts.test_user_id = user.id


@th.django_unit_test()
def test_unauthorized_access(opts):
    """Test that non-admin users cannot manage permissions."""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "User authentication failed"

    # Try to get permissions without admin rights
    resp = opts.client.get("/api/metrics/permissions", params={"account": "test_account"})

    if resp.status_code != 403:
        error_details = f"Expected status 403, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    # Try to set view permissions without admin rights
    resp = opts.client.post("/api/metrics/permissions/view", json={
        "account": "test_account",
        "permissions": ["view_metrics"]
    })

    if resp.status_code != 403:
        error_details = f"Expected status 403 for unauthorized permission setting, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details


@th.django_unit_test()
def test_set_view_permissions(opts):
    """Test setting view permissions for an account."""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "Admin authentication failed"

    # Test setting single permission as string
    resp = opts.client.post("/api/metrics/permissions/view", json={
        "account": "test_account_1",
        "permissions": "view_metrics"
    })

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.account == "test_account_1", f"Expected account 'test_account_1', got '{data.account}'"
    assert data.view_permissions == "view_metrics", f"Expected 'view_metrics', got '{data.view_permissions}'"
    assert data.action == "set", f"Expected action 'set', got '{data.action}'"

    # Test setting multiple permissions as array
    resp = opts.client.post("/api/metrics/permissions/view", json={
        "account": "test_account_2",
        "permissions": ["view_metrics", "view_analytics"]
    })

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.account == "test_account_2", f"Expected account 'test_account_2', got '{data.account}'"
    assert isinstance(data.view_permissions, list), "Permissions should be returned as list"
    assert "view_metrics" in data.view_permissions, "Should contain 'view_metrics'"
    assert "view_analytics" in data.view_permissions, "Should contain 'view_analytics'"


@th.django_unit_test()
def test_set_write_permissions(opts):
    """Test setting write permissions for an account."""
    # Should still be logged in as admin from previous test
    assert opts.client.is_authenticated, "Should still be authenticated as admin"

    resp = opts.client.post("/api/metrics/permissions/write", json={
        "account": "test_account_1",
        "permissions": ["write_metrics", "admin_metrics"]
    })

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.account == "test_account_1", f"Expected account 'test_account_1', got '{data.account}'"
    assert isinstance(data.write_permissions, list), "Permissions should be returned as list"
    assert "write_metrics" in data.write_permissions, "Should contain 'write_metrics'"
    assert "admin_metrics" in data.write_permissions, "Should contain 'admin_metrics'"


@th.django_unit_test()
def test_get_permissions(opts):
    """Test getting permissions for an account."""
    # Should still be logged in as admin from previous test
    assert opts.client.is_authenticated, "Should still be authenticated as admin"

    resp = opts.client.get("/api/metrics/permissions", params={"account": "test_account_1"})

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.account == "test_account_1", f"Expected account 'test_account_1', got '{data.account}'"
    assert data.view_permissions == "view_metrics", "Should have view_metrics permission"
    assert isinstance(data.write_permissions, list), "Write permissions should be list"
    assert "write_metrics" in data.write_permissions, "Should contain 'write_metrics'"
    assert "admin_metrics" in data.write_permissions, "Should contain 'admin_metrics'"


@th.django_unit_test()
def test_update_permissions_with_put(opts):
    """Test updating permissions using PUT method."""
    # Should still be logged in as admin from previous test
    assert opts.client.is_authenticated, "Should still be authenticated as admin"

    # Update view permissions using PUT
    resp = opts.client.put("/api/metrics/permissions/view", json={
        "account": "test_account_1",
        "permissions": ["view_metrics", "view_reports", "view_analytics"]
    })

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.account == "test_account_1", f"Expected account 'test_account_1', got '{data.account}'"
    assert isinstance(data.view_permissions, list), "Permissions should be list"
    assert len(data.view_permissions) == 3, f"Expected 3 permissions, got {len(data.view_permissions)}"
    assert "view_reports" in data.view_permissions, "Should contain new 'view_reports' permission"


@th.django_unit_test()
def test_set_null_permissions(opts):
    """Test setting permissions to null (removing them)."""
    # Should still be logged in as admin from previous test
    assert opts.client.is_authenticated, "Should still be authenticated as admin"

    # Set permissions to null to remove them
    resp = opts.client.post("/api/metrics/permissions/view", json={
        "account": "test_account_2",
        "permissions": None
    })

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.view_permissions is None, "View permissions should be null"

    # Verify permissions were actually removed
    resp = opts.client.get("/api/metrics/permissions", params={"account": "test_account_2"})
    if resp.status_code == 200:
        data = resp.response.data
        assert data.view_permissions is None, "View permissions should be None after removal"


@th.django_unit_test()
def test_delete_all_permissions(opts):
    """Test deleting all permissions for an account."""
    # Should still be logged in as admin from previous test
    assert opts.client.is_authenticated, "Should still be authenticated as admin"

    # Delete all permissions for test_account_1
    resp = opts.client.delete("/api/metrics/permissions", json={
        "account": "test_account_1"
    })

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.account == "test_account_1", f"Expected account 'test_account_1', got '{data.account}'"
    assert data.action == "deleted", f"Expected action 'deleted', got '{data.action}'"

    # Verify all permissions were removed
    resp = opts.client.get("/api/metrics/permissions", params={"account": "test_account_1"})
    if resp.status_code == 200:
        data = resp.response.data
        assert data.view_permissions is None, "View permissions should be None after deletion"
        assert data.write_permissions is None, "Write permissions should be None after deletion"


@th.django_unit_test()
def test_list_accounts_with_permissions(opts):
    """Test listing accounts with permissions configured."""
    # Should still be logged in as admin from previous test
    assert opts.client.is_authenticated, "Should still be authenticated as admin"

    # First set up some test permissions
    opts.client.post("/api/metrics/permissions/view", json={
        "account": "group_123",
        "permissions": "view_group_metrics"
    })

    opts.client.post("/api/metrics/permissions/write", json={
        "account": "group_123",
        "permissions": ["write_group_metrics"]
    })

    # List accounts with permissions
    resp = opts.client.get("/api/metrics/permissions/accounts")

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert 'accounts' in data, "Response should contain accounts list"
    assert 'count' in data, "Response should contain count"
    assert isinstance(data.accounts, list), "Accounts should be a list"

    # Find our test account
    group_account = None
    for account in data.accounts:
        if account['account'] == 'group_123':
            group_account = account
            break

    assert group_account is not None, "Should find group_123 account in results"
    assert group_account['view_permissions'] == "view_group_metrics", "Should have correct view permissions"
    assert "write_group_metrics" in group_account['write_permissions'], "Should have correct write permissions"


@th.django_unit_test()
def test_missing_required_parameters(opts):
    """Test API responses when required parameters are missing."""
    # Should still be logged in as admin from previous test
    assert opts.client.is_authenticated, "Should still be authenticated as admin"

    # Test missing account parameter
    resp = opts.client.get("/api/metrics/permissions")
    assert resp.status_code == 400, f"Expected status 400 for missing account parameter, got {resp.status_code}"

    # Test missing permissions parameter
    resp = opts.client.post("/api/metrics/permissions/view", json={
        "account": "test_account"
    })
    assert resp.status_code == 400, f"Expected status 400 for missing permissions parameter, got {resp.status_code}"


@th.django_unit_test()
def test_superuser_access(opts):
    """Test that superuser can manage permissions even without manage_metrics permission."""
    from mojo.apps.account.models import User

    # Login as admin and remove manage_metrics permission, but keep superuser status
    admin = User.objects.get(username=ADMIN_USER)
    admin.remove_permission("manage_metrics")
    admin.is_superuser = True
    admin.save()

    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "Admin authentication failed"

    # Should still be able to set permissions as superuser
    resp = opts.client.post("/api/metrics/permissions/view", json={
        "account": "superuser_test",
        "permissions": "view_metrics"
    })

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.account == "superuser_test", "Should be able to set permissions as superuser"

    # Restore manage_metrics permission for cleanup
    admin.add_permission("manage_metrics")
    admin.save()


@th.django_unit_test()
def test_cleanup_permissions_data(opts):
    """Clean up test permissions data."""
    from mojo.apps.account.models import User
    from mojo.apps import metrics

    # Clean up test permissions
    test_accounts = ["test_account_1", "test_account_2", "group_123", "superuser_test"]
    for account in test_accounts:
        metrics.set_view_perms(account, None)
        metrics.set_write_perms(account, None)

    # Reset user permissions
    admin = User.objects.get(username=ADMIN_USER)
    admin.remove_permission("manage_metrics")
    admin.save()

    user = User.objects.get(username=TEST_USER)
    user.remove_permission("manage_metrics")  # Ensure it's not set
    user.save()

    # Verify cleanup
    remaining_accounts = metrics.get_accounts_with_permissions()
    test_account_names = {acc['account'] for acc in remaining_accounts if acc['account'] in test_accounts}

    assert len(test_account_names) == 0, f"Expected 0 remaining test accounts, found: {test_account_names}"
