from testit import helpers as th
from testit import faker
from unittest.mock import patch, MagicMock

TEST_USER = "push_user"
TEST_PWORD = "push##mojo99"
ADMIN_USER = "push_admin"
ADMIN_PWORD = "push##mojo99"


@th.django_unit_setup()
def setup_push_testing(opts):
    """
    Setup test data for push notification testing.
    """
    from mojo.apps.account.models import User, Group, RegisteredDevice, PushConfig, NotificationTemplate, NotificationDelivery

    # Clean up any existing test data
    RegisteredDevice.objects.filter(user__username__in=[TEST_USER, ADMIN_USER]).delete()
    NotificationTemplate.objects.filter(name__startswith='test_').delete()
    NotificationDelivery.objects.filter(user__username__in=[TEST_USER, ADMIN_USER]).delete()
    PushConfig.objects.filter(name__startswith='test_').delete()
    User.objects.filter(username__in=[TEST_USER, ADMIN_USER]).delete()

    # Create test organization
    test_org, _ = Group.objects.get_or_create(
        name='test_org_push',
        kind='organization'
    )

    # Create dedicated test user
    user = User(username=TEST_USER, email=f"{TEST_USER}@test.com")
    user.save()
    user.org = test_org
    user.is_active = True
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.add_permission("send_notifications")
    user.save()

    # Create dedicated admin user
    admin = User(username=ADMIN_USER, email=f"{ADMIN_USER}@test.com")
    admin.save()
    admin.is_active = True
    admin.is_email_verified = True
    admin.is_staff = True
    admin.save_password(ADMIN_PWORD)
    admin.add_permission(["manage_push_config", "manage_notifications"])
    admin.save()

    # Store IDs for later use
    opts.test_org_id = test_org.id
    opts.test_user_id = user.id
    opts.admin_user_id = admin.id


@th.django_unit_test()
def test_registered_device_creation(opts):
    """Test RegisteredDevice model creation and validation."""
    from mojo.apps.account.models import User, RegisteredDevice

    user = User.objects.get(username=TEST_USER)

    device = RegisteredDevice.objects.create(
        user=user,
        device_token="test_token_123",
        device_id="test_device_1",
        platform="ios",
        device_name="Test iPhone",
        app_version="1.0.0",
        push_preferences={"orders": True, "marketing": False}
    )

    assert device.user == user, f"Expected user {user}, got {device.user}"
    assert device.platform == "ios", f"Expected platform ios, got {device.platform}"
    assert device.push_enabled == True, "Device should be push enabled by default"
    assert device.push_preferences["orders"] == True, "Orders preference should be True"
    assert device.push_preferences["marketing"] == False, "Marketing preference should be False"

    opts.test_device_id = device.id


@th.django_unit_test()
def test_push_config_creation_and_encryption(opts):
    """Test PushConfig creation with test mode and FCM secrets encryption."""
    from mojo.apps.account.models import Group, PushConfig

    test_org = Group.objects.get(id=opts.test_org_id)

    # Create a separate config to test creation (not the setup one)
    config = PushConfig.objects.create(
        group=test_org,
        name="test_secrets_config",
        test_mode=True
    )

    # Test MojoSecrets with FCM v1 service account
    test_service_account = {
        "project_id": "test-project-123",
        "private_key": "dummy_key_for_testing",
        "client_email": "test@test-project.iam.gserviceaccount.com"
    }
    config.set_fcm_service_account(test_service_account)
    config.save()

    opts.test_config_id = config.id
    assert config.group == test_org, f"Expected group {test_org}, got {config.group}"
    assert config.test_mode == True, "Test mode should be True for this config"
    assert config.fcm_project_id == "test-project-123", "FCM project ID should be extracted from service account"

    # Test credential encryption/decryption via MojoSecrets
    decrypted_account = config.get_fcm_service_account()
    assert decrypted_account is not None, "Service account should be decrypted"
    assert decrypted_account['project_id'] == "test-project-123", "Service account should be decrypted correctly"

    opts.test_secrets_config_id = config.id


@th.django_unit_test()
def test_system_default_config(opts):
    """Test system-wide default push config with test mode."""
    from mojo.apps.account.models import PushConfig, User

    # Create system default config (no group) with test mode
    system_config = PushConfig.objects.create(
        group=None,
        name="test_system_default",
        test_mode=True
    )

    user = User.objects.get(username=TEST_USER)
    resolved_config = PushConfig.get_for_user(user)

    # Should get org config first (which has test_mode=True), then fall back to system
    assert resolved_config.group == user.org, "Should prefer org config over system config"
    assert resolved_config.test_mode == True, "Should have test mode enabled"


@th.django_unit_test()
def test_notification_template_creation(opts):
    """Test NotificationTemplate creation and rendering."""
    from mojo.apps.account.models import Group, NotificationTemplate

    test_org = Group.objects.get(id=opts.test_org_id)

    template = NotificationTemplate.objects.create(
        group=test_org,
        name="test_order_ready",
        title_template="Order Ready, {customer_name}!",
        body_template="Hi {customer_name}, your order #{order_id} is ready for pickup at {location}.",
        action_url="myapp://orders/{order_id}",
        category="orders",
        priority="normal",
        variables={
            "customer_name": "Customer's display name",
            "order_id": "Order number",
            "location": "Pickup location"
        }
    )

    # Test template rendering
    context = {
        "customer_name": "John Doe",
        "order_id": "12345",
        "location": "Main Street Store"
    }

    title, body, action_url, data = template.render(context)

    assert title == "Order Ready, John Doe!", f"Expected 'Order Ready, John Doe!', got '{title}'"
    assert "John Doe" in body, f"Body should contain customer name, got '{body}'"
    assert "12345" in body, f"Body should contain order ID, got '{body}'"
    assert action_url == "myapp://orders/12345", f"Expected 'myapp://orders/12345', got '{action_url}'"

    opts.test_template_id = template.id


@th.django_unit_test()
def test_push_service_test_mode(opts):
    """Test device.send() behavior with test mode enabled."""
    from mojo.apps.account.models import User, PushConfig

    user = User.objects.get(username=TEST_USER)

    # Verify config has test mode enabled
    config = PushConfig.get_for_user(user)
    assert config is not None, "User should have push config"
    assert config.test_mode == True, "Config should have test mode enabled"

    # Send via user.push_notification()
    results = user.push_notification(
        title="Test Mode Notification",
        body="This should succeed in test mode"
    )

    # Should create delivery records that succeed due to test mode
    assert len(results) > 0, "Should create delivery records"
    for result in results:
        assert result.status == 'sent', f"Expected status 'sent' in test mode, got '{result.status}'"
        assert result.platform_data.get('test_mode') == True, "Should have test mode data"


@th.django_unit_test()
def test_device_registration_api(opts):
    """Test device registration REST API endpoint."""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "Authentication failed"

    device_data = {
        "device_token": "api_test_token_456",
        "device_id": "api_test_device_2",
        "platform": "android",
        "device_name": "Test Android",
        "app_version": "2.0.0",
        "push_preferences": {"alerts": True, "marketing": True}
    }

    resp = opts.client.post("/api/account/devices/push/register", json=device_data)

    # Show detailed error info if request failed
    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response:
            if hasattr(resp.response, 'error'):
                error_details += f"\nError: {resp.response.error}"
            if hasattr(resp.response, 'data'):
                error_details += f"\nResponse data: {resp.response.data}"
        error_details += f"\nFull response: {resp}"
        assert False, error_details

    data = resp.response.data
    assert data.device_id == "api_test_device_2", f"Expected device_id 'api_test_device_2', got '{data.device_id}'"
    assert data.platform == "android", f"Expected platform 'android', got '{data.platform}'"
    assert data.push_enabled == True, "Device should be push enabled"
    assert bool(data.id) == True, "Device ID should be set"
    opts.api_device_id = data.id


@th.django_unit_test()
def test_device_list_api(opts):
    """Test listing registered devices via API."""
    # Should still be logged in from previous test
    assert opts.client.is_authenticated, "Should still be authenticated"

    resp = opts.client.get("/api/account/devices/push")

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    devices = resp.response.data
    assert len(devices) >= 2, f"Expected at least 2 devices, got {len(devices)}"

    # Check that we can see both devices we created
    device_ids = [d.device_id for d in devices]
    assert "test_device_1" in device_ids, "Should include test_device_1"
    assert "api_test_device_2" in device_ids, "Should include api_test_device_2"


@th.django_unit_test()
def test_notification_template_api(opts):
    """Test notification template management via API."""
    # Make sure we're logged in as admin for template management
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "Admin authentication failed"

    template_data = {
        "name": "test_api_template",
        "title_template": "Welcome {name}!",
        "body_template": "Hi {name}, welcome to our app!",
        "category": "onboarding",
        "priority": "normal",
        "variables": {
            "name": "User's display name"
        }
    }

    resp = opts.client.post("/api/account/devices/push/templates", json=template_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.name == "test_api_template", f"Expected name 'test_api_template', got '{data.name}'"
    assert data.category == "onboarding", f"Expected category 'onboarding', got '{data.category}'"

    opts.api_template_id = data.id


@th.django_unit_test()
def test_push_config_api(opts):
    """Test push configuration management via API with test mode."""
    # Make sure we're logged in as admin
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "Admin authentication failed"

    config_data = {
        "name": "test_api_config",
        "test_mode": True,
        "default_sound": "notification.wav"
    }

    resp = opts.client.post("/api/account/devices/push/config", json=config_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.name == "test_api_config", f"Expected name 'test_api_config', got '{data.name}'"
    assert data.test_mode == True, "Test mode should be enabled"
    # Note: fcm_project_id will be None until service account is configured via set_fcm_service_account()

    # Verify that sensitive fields are not exposed
    assert 'mojo_secrets' not in data, "MojoSecrets should not be exposed in API response"
    # Verify test mode field is present
    assert 'test_mode' in data, "Test mode should be in API response"


@th.django_unit_test()
def test_direct_notification_send_api(opts):
    """Test sending direct notifications via API with test mode."""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "User authentication failed"

    notification_data = {
        "title": "Test Direct Notification",
        "body": "This is a test notification sent directly via API in test mode",
        "category": "test",
        "action_url": "myapp://test/123"
    }

    resp = opts.client.post("/api/account/devices/push/send", json=notification_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if resp.error:
            error_details += f"\nError: {resp.error}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.success == True, "Notification send should be successful"
    assert data.sent_count >= 1, f"Should have sent notifications in test mode, got {data.sent_count}"
    assert data.failed_count == 0, f"Should have no failures in test mode, got {data.failed_count}"
    assert isinstance(data.deliveries, list), "Should return delivery records"

    # Verify test mode delivery
    if len(data.deliveries) > 0:
        delivery = data.deliveries[0]
        assert delivery.status == 'sent', f"Delivery should be sent in test mode, got '{delivery.status}'"


@th.django_unit_test()
def test_data_only_notification_send_api(opts):
    """Test sending silent (data-only) notifications via API."""
    # Should still be logged in from previous test
    assert opts.client.is_authenticated, "Should still be authenticated"

    # Silent notification with only data payload
    notification_data = {
        "data": {
            "action": "sync",
            "timestamp": 1234567890,
            "order_id": "API123"
        },
        "category": "system"
    }

    resp = opts.client.post("/api/account/devices/push/send", json=notification_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.success == True, "Data-only notification send should be successful"
    assert data.sent_count >= 1, f"Should have sent notifications, got {data.sent_count}"

    # Check that deliveries were created with data payload
    if len(data.deliveries) > 0:
        delivery = data.deliveries[0]
        assert delivery.status == 'sent', "Delivery should be sent in test mode"


@th.django_unit_test()
def test_notification_delivery_history_api(opts):
    """Test viewing notification delivery history via API."""
    # Should still be logged in from previous test
    assert opts.client.is_authenticated, "Should still be authenticated"

    resp = opts.client.get("/api/account/devices/push/deliveries")

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    deliveries = resp.response.data
    assert len(deliveries) >= 2, f"Expected at least 2 deliveries from previous tests, got {len(deliveries)}"

    # Check that we can see deliveries from our tests
    has_direct = any(d.title == "Test Direct Notification" for d in deliveries if d.title)
    has_data_only = any(d.category == "system" for d in deliveries)
    assert has_direct or has_data_only, "Should include notifications from previous tests"


@th.django_unit_test()
def test_push_statistics_api(opts):
    """Test push notification statistics endpoint."""
    # Should still be logged in from previous test
    # resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    # assert opts.client.is_authenticated, "Admin authentication failed"

    assert opts.client.is_authenticated, "Should still be authenticated"

    resp = opts.client.get("/api/account/devices/push/stats")

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    stats = resp.response.data
    assert 'total_sent' in stats, "Stats should include total_sent"
    assert 'total_failed' in stats, "Stats should include total_failed"
    assert 'total_pending' in stats, "Stats should include total_pending"
    assert 'registered_devices' in stats, "Stats should include registered_devices"
    assert 'enabled_devices' in stats, "Stats should include enabled_devices"

    assert stats.registered_devices >= 2, f"Should have at least 2 registered devices, got {stats.registered_devices}"


@th.django_unit_test()
def test_push_test_endpoint(opts):
    """Test push configuration test endpoint."""
    # Make sure we're logged in as admin for test endpoint
    # resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    # assert opts.client.is_authenticated, "Admin authentication failed"

    test_data = {
        "message": "This is a test notification from the test endpoint"
    }

    resp = opts.client.post("/api/account/devices/push/test", json=test_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.success == True, "Test notification should be successful"
    assert "test notifications sent" in data.message.lower(), "Should confirm test notifications were sent"


@th.django_unit_test()
def test_device_update_preferences_api(opts):
    """Test updating device push preferences via API."""
    # Make sure we're logged in as test user for device management
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "User authentication failed"

    update_data = {
        "push_enabled": True,
        "push_preferences": {
            "orders": True,
            "marketing": False,
            "alerts": True,
            "social": False
        }
    }

    assert opts.api_device_id is not None, "opts.api_device_id is None"
    resp = opts.client.put(f"/api/account/devices/push/{opts.api_device_id}", json=update_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.push_enabled == True, "Push should remain enabled"
    assert data.push_preferences.orders == True, "Orders preference should be True"
    assert data.push_preferences.marketing == False, "Marketing preference should be False"
    assert data.push_preferences.social == False, "Social preference should be False"


@th.django_unit_test()
def test_unauthorized_push_operations(opts):
    """Test that unauthorized users cannot perform restricted push operations."""
    # Login as test user to test permission restrictions
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "User authentication failed"

    # Remove send_notifications permission temporarily
    from mojo.apps.account.models import User
    user = User.objects.get(username=TEST_USER)
    user.remove_permission("send_notifications")
    user.save()

    # Try to send notification without permission
    notification_data = {
        "title": "Unauthorized Test",
        "body": "This should fail"
    }

    resp = opts.client.post("/api/account/devices/push/send", json=notification_data)

    if resp.status_code != 403:
        error_details = f"Expected status 403 for unauthorized send, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    # Try to access push config without permission
    resp = opts.client.get("/api/account/devices/push/config")
    if resp.status_code == 200:
        assert resp.response.count == 0, "Expected no devices to receive notification"
    elif resp.status_code != 403:
        error_details = f"Expected status 403 for unauthorized config access, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    # Restore permission for cleanup
    user.add_permission("send_notifications")
    user.save()


@th.django_unit_test()
def test_device_preferences_filtering(opts):
    """Test that device preferences are respected when sending notifications with test mode."""
    from mojo.apps.account.models import User, RegisteredDevice

    user = User.objects.get(username=TEST_USER)

    # Get a device and set specific preferences
    device = RegisteredDevice.objects.filter(user=user).first()
    device.push_preferences = {"orders": True, "marketing": False, "alerts": True}
    device.save()

    # Test that marketing notification should not be sent to this device
    results = user.push_notification(
        title="Marketing Test",
        body="This is a marketing message",
        category="marketing"
    )

    # With test mode enabled, notifications succeed but preference filtering still works
    # Device has marketing=False, so no deliveries should be created for marketing category
    assert len(results) == 0, "Should not send to devices with marketing disabled"

    # Test that orders notification should be sent (orders=True in preferences)
    results = user.push_notification(
        title="Order Test",
        body="Your order is ready",
        category="orders"
    )

    # Should have results since orders=True in preferences and test mode succeeds
    assert len(results) > 0, "Should send to devices with orders enabled"
    for result in results:
        assert result.status == 'sent', "Should succeed in test mode"


@th.django_unit_test()
def test_template_model_functionality(opts):
    """Test that NotificationTemplate model works correctly."""
    from mojo.apps.account.models import User, Group, NotificationTemplate

    user = User.objects.get(username=TEST_USER)
    test_org = Group.objects.get(id=opts.test_org_id)

    # Create and test a template
    template = NotificationTemplate.objects.create(
        group=test_org,
        name="test_render_template",
        title_template="Hello {name}!",
        body_template="Your order {order_id} is ready",
        category="test"
    )

    # Test template rendering
    context = {"name": "John", "order_id": "12345"}
    title, body, action_url, data = template.render(context)

    assert title == "Hello John!", f"Expected 'Hello John!', got '{title}'"
    assert "12345" in body, f"Expected order_id in body, got '{body}'"
    assert template.category == "test", "Template category should be 'test'"


@th.django_unit_test()
def test_notification_delivery_status_methods(opts):
    """Test NotificationDelivery status update methods."""
    from mojo.apps.account.models import User, RegisteredDevice, NotificationDelivery

    user = User.objects.get(username=TEST_USER)
    device = RegisteredDevice.objects.filter(user=user).first()

    delivery = NotificationDelivery.objects.create(
        user=user,
        device=device,
        title="Status Test",
        body="Testing status methods",
        category="test"
    )

    # Test marking as sent
    delivery.mark_sent()
    delivery.refresh_from_db()
    assert delivery.status == 'sent', f"Expected status 'sent', got '{delivery.status}'"
    assert delivery.sent_at is not None, "sent_at should be set"

    # Test marking as delivered
    delivery.mark_delivered()
    delivery.refresh_from_db()
    assert delivery.status == 'delivered', f"Expected status 'delivered', got '{delivery.status}'"
    assert delivery.delivered_at is not None, "delivered_at should be set"

    # Test marking as failed
    delivery.mark_failed("Test error message")
    delivery.refresh_from_db()
    assert delivery.status == 'failed', f"Expected status 'failed', got '{delivery.status}'"
    assert delivery.error_message == "Test error message", f"Expected error message 'Test error message', got '{delivery.error_message}'"


@th.django_unit_test()
def test_fcm_secrets_functionality(opts):
    """Test FCM service account encryption/decryption functionality."""
    from mojo.apps.account.models import PushConfig

    # Get our test config and verify secrets work
    test_config = PushConfig.objects.get(id=opts.test_secrets_config_id)

    # Verify the service account was set and can be retrieved
    service_account = test_config.get_fcm_service_account()
    assert service_account is not None, "Service account should be set"
    assert service_account['project_id'] == "test-project-123", f"Expected project_id 'test-project-123', got '{service_account.get('project_id')}'"

    # Test updating the service account
    updated_account = {
        "project_id": "updated-project-456",
        "private_key": "updated_dummy_key",
        "client_email": "updated@test-project.iam.gserviceaccount.com"
    }
    test_config.set_fcm_service_account(updated_account)
    test_config.save()

    # Verify the update worked
    retrieved_account = test_config.get_fcm_service_account()
    assert retrieved_account['project_id'] == "updated-project-456", f"Expected project_id 'updated-project-456', got '{retrieved_account.get('project_id')}'"

    # Verify the encrypted field exists but is not the plain text
    assert test_config.mojo_secrets is not None, "mojo_secrets field should contain encrypted data"
    assert "updated-project-456" not in test_config.mojo_secrets, "Plain text should not be in encrypted field"


@th.django_unit_test()
def test_comprehensive_test_mode_verification(opts):
    """Test comprehensive test mode functionality without external dependencies."""
    from mojo.apps.account.models import User, RegisteredDevice, PushConfig

    # Create a config with test mode enabled
    test_config = PushConfig.objects.create(
        name="comprehensive_test_config",
        test_mode=True
    )

    # Temporarily assign this config by creating a user without org
    temp_user = User.objects.create(username="temp_test_user", email="temp@test.com")
    temp_user.org = None
    temp_user.save()

    # Create devices for different platforms
    ios_device = RegisteredDevice.objects.create(
        user=temp_user,
        device_token="test_ios_token",
        device_id="test_ios_device",
        platform="ios",
        device_name="Test iOS Device"
    )

    android_device = RegisteredDevice.objects.create(
        user=temp_user,
        device_token="test_android_token",
        device_id="test_android_device",
        platform="android",
        device_name="Test Android Device"
    )

    web_device = RegisteredDevice.objects.create(
        user=temp_user,
        device_token="test_web_token",
        device_id="test_web_device",
        platform="web",
        device_name="Test Web Device"
    )

    # Override get_for_user to return our test config
    original_get_for_user = PushConfig.get_for_user
    PushConfig.get_for_user = lambda u: test_config

    try:
        # Test that all platforms work in test mode using simplified API
        results = temp_user.push_notification(
            title="Test Mode Verification",
            body="Testing all platforms in test mode"
        )

        # Should have 3 successful deliveries (one per device)
        assert len(results) == 3, f"Expected 3 deliveries, got {len(results)}"

        # All should be marked as sent in test mode
        for result in results:
            assert result.status == 'sent', f"Expected status 'sent', got '{result.status}'"
            assert result.platform_data.get('test_mode') == True, "Should have test mode flag"
            assert 'platform' in result.platform_data, "Should record the platform"
            assert 'device_name' in result.platform_data, "Should record device name"

        # Verify platform data for each device type
        ios_result = next(r for r in results if r.device.platform == 'ios')
        android_result = next(r for r in results if r.device.platform == 'android')
        web_result = next(r for r in results if r.device.platform == 'web')

        assert ios_result.platform_data['platform'] == 'ios'
        assert android_result.platform_data['platform'] == 'android'
        assert web_result.platform_data['platform'] == 'web'

    finally:
        # Restore original method
        PushConfig.get_for_user = original_get_for_user

        # Clean up temp data
        temp_user.delete()  # This will cascade delete the devices
        test_config.delete()


@th.django_unit_test()
def test_unregister_device_api(opts):
    """Test POST /api/account/devices/push/unregister deactivates a device."""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "User authentication failed"

    # Register a fresh device to unregister
    device_data = {
        "device_token": "unregister_test_token_789",
        "device_id": "unregister_test_device",
        "platform": "ios",
        "device_name": "Device to Unregister"
    }
    resp = opts.client.post("/api/account/devices/push/register", json=device_data)
    assert resp.status_code == 200, f"Register failed: {resp.status_code} {resp.response.data}"

    # Unregister it
    unregister_data = {
        "device_token": "unregister_test_token_789",
        "device_id": "unregister_test_device",
        "platform": "ios"
    }
    resp = opts.client.post("/api/account/devices/push/unregister", json=unregister_data)
    assert resp.status_code == 200, f"Unregister failed: {resp.status_code} {resp.response.data}"

    # Verify device is inactive in database
    from mojo.apps.account.models import RegisteredDevice
    device = RegisteredDevice.objects.get(device_id="unregister_test_device")
    assert device.is_active == False, f"Expected device to be inactive after unregister, got is_active={device.is_active}"


@th.django_unit_test()
def test_push_config_fcm_test_api(opts):
    """Test POST /api/account/devices/push/config/<pk>/test validates FCM credentials."""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "Admin authentication failed"

    from mojo.apps.account.models import PushConfig
    config = PushConfig.objects.filter(name="test_api_config").first()
    assert config is not None, "Expected test_api_config to exist (created in test_push_config_api)"

    resp = opts.client.post(f"/api/account/devices/push/config/{config.id}/test")
    assert resp.status_code == 200, f"Config test failed: {resp.status_code} {resp.response.data}"

    data = resp.response.data
    assert 'success' in data, "Response should include success field"
    assert 'message' in data, "Response should include message field"


@th.django_unit_test()
def test_send_to_device_service(opts):
    """Test services/push.send_to_device() sends to a specific device by pk."""
    from mojo.apps.account.models import User, RegisteredDevice
    from mojo.apps.account.services.push import send_to_device

    user = User.objects.get(username=TEST_USER)
    device = RegisteredDevice.objects.filter(user=user, is_active=True).first()
    assert device is not None, "Expected at least one active device for test user"

    delivery = send_to_device(
        device_id=device.id,
        title="Service Layer Test",
        body="Testing send_to_device service function",
        category="system",
        data={"test": True}
    )

    assert delivery is not None, "send_to_device should return a delivery record"
    assert delivery.status == 'sent', f"Expected status 'sent' in test mode, got '{delivery.status}'"
    assert delivery.device_id == device.id, f"Delivery should be linked to device {device.id}"


@th.django_unit_test()
def test_send_to_user_ids_api(opts):
    """Test POST /api/account/devices/push/send with user_ids sends to multiple users."""
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "Admin authentication failed"

    from mojo.apps.account.models import User
    test_user = User.objects.get(username=TEST_USER)
    admin_user = User.objects.get(username=ADMIN_USER)

    # Ensure admin has send_notifications permission
    admin_user.add_permission("send_notifications")
    admin_user.save()

    notification_data = {
        "title": "Multi-User Test",
        "body": "Sent to specific user IDs",
        "category": "system",
        "user_ids": [test_user.id, admin_user.id]
    }

    resp = opts.client.post("/api/account/devices/push/send", json=notification_data)
    assert resp.status_code == 200, f"Multi-user send failed: {resp.status_code} {resp.response.data}"

    data = resp.response.data
    assert data.success == True, "Multi-user send should succeed"
    assert data.sent_count >= 1, f"Should have sent to at least one device, got {data.sent_count}"


@th.django_unit_test()
def test_send_notification_missing_content_returns_400(opts):
    """Test POST /api/account/devices/push/send with no title, body, or data returns 400."""
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "User authentication failed"

    # Restore send_notifications permission (removed in test_unauthorized_push_operations)
    from mojo.apps.account.models import User
    user = User.objects.get(username=TEST_USER)
    user.add_permission("send_notifications")
    user.save()

    # Send with no title, body, or data — should be rejected
    notification_data = {
        "category": "system"
    }

    resp = opts.client.post("/api/account/devices/push/send", json=notification_data)
    assert resp.status_code == 400, f"Expected 400 for empty notification content, got {resp.status_code}"


@th.django_unit_test()
def test_cleanup_test_data(opts):
    """Clean up test data created during testing."""
    from mojo.apps.account.models import User, Group, RegisteredDevice, PushConfig, NotificationTemplate, NotificationDelivery

    # Clean up test data
    RegisteredDevice.objects.filter(user__username__in=[TEST_USER, ADMIN_USER]).delete()
    NotificationTemplate.objects.filter(name__startswith='test_').delete()
    NotificationDelivery.objects.filter(user__username__in=[TEST_USER, ADMIN_USER]).delete()
    PushConfig.objects.filter(name__startswith='test_').delete()
    Group.objects.filter(name__startswith='test_org').delete()

    # Reset user permissions and org assignments
    user = User.objects.get(username=TEST_USER)
    user.org = None
    user.remove_permission(["send_notifications", "manage_notifications", "manage_push_config"])
    user.save()

    admin = User.objects.get(username=ADMIN_USER)
    admin.remove_permission(["manage_push_config", "manage_notifications"])
    admin.save()

    # Verify cleanup
    remaining_devices = RegisteredDevice.objects.filter(user__username__in=[TEST_USER, ADMIN_USER]).count()
    remaining_templates = NotificationTemplate.objects.filter(name__startswith='test_').count()
    remaining_configs = PushConfig.objects.filter(name__startswith='test_').count()
    remaining_deliveries = NotificationDelivery.objects.filter(user__username__in=[TEST_USER, ADMIN_USER]).count()

    assert remaining_devices == 0, f"Expected 0 remaining devices, found {remaining_devices}"
    assert remaining_templates == 0, f"Expected 0 remaining templates, found {remaining_templates}"
    assert remaining_configs == 0, f"Expected 0 remaining configs, found {remaining_configs}"
    assert remaining_deliveries == 0, f"Expected 0 remaining deliveries, found {remaining_deliveries}"

    # Verify test mode configs are cleaned up
    test_mode_configs = PushConfig.objects.filter(test_mode=True).count()
    assert test_mode_configs == 0, f"Expected 0 test mode configs remaining, found {test_mode_configs}"
