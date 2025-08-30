from testit import helpers as th
from testit import faker
from unittest.mock import patch, MagicMock

# Use the same test users as other account tests
TEST_USER = "testit"
TEST_PWORD = "testit##mojo"
ADMIN_USER = "tadmin"
ADMIN_PWORD = "testit##mojo"


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
    # Create test organization
    test_org, _ = Group.objects.get_or_create(
        name='test_org_push',
        kind='organization'
    )

    # Assign test user to organization
    user = User.objects.get(username=TEST_USER)
    user.org = test_org
    user.add_permission("send_notifications")
    user.save()

    # Give admin push config permissions
    admin = User.objects.get(username=ADMIN_USER)
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
        test_mode=True,  # Test production-like config
        fcm_enabled=True,
        apns_enabled=False,  # Test both for completeness
        apns_key_id="TEST123",
        apns_team_id="TEAM123",
        apns_bundle_id="com.test.app"
    )

    # Test MojoSecrets with FCM (primary)
    config.set_fcm_server_key("test_fcm_server_key_12345")
    config.save()

    # Test MojoSecrets with APNS (secondary)
    config.set_apns_key_file("-----BEGIN PRIVATE KEY-----\nTEST_KEY_DATA\n-----END PRIVATE KEY-----")
    config.save()

    opts.test_config_id = config.id
    assert config.group == test_org, f"Expected group {test_org}, got {config.group}"
    assert config.fcm_enabled == True, "FCM should be enabled (primary)"
    assert config.apns_enabled == False, "APNS should be enabled (secondary)"
    assert config.test_mode == True, "Test mode should be False for this config"

    # Test credential encryption/decryption via MojoSecrets
    decrypted_fcm = config.get_fcm_server_key()
    assert decrypted_fcm == "test_fcm_server_key_12345", "FCM key should be decrypted correctly"

    decrypted_apns = config.get_apns_key_file()
    assert "TEST_KEY_DATA" in decrypted_apns, "APNS key should be decrypted correctly"

    opts.test_secrets_config_id = config.id


@th.django_unit_test()
def test_system_default_config(opts):
    """Test system-wide default push config with test mode."""
    from mojo.apps.account.models import PushConfig, User

    # Create system default config (no group) with test mode
    system_config = PushConfig.objects.create(
        group=None,
        name="test_system_default",
        test_mode=True,  # Test mode for system default
        fcm_enabled=True,
        apns_enabled=False  # FCM is primary
    )

    user = User.objects.get(username=TEST_USER)
    resolved_config = PushConfig.get_for_user(user)

    # Should get org config first (which has test_mode=True), then fall back to system
    assert resolved_config.group == user.org, "Should prefer org config over system config"
    assert resolved_config.test_mode == True, "Should have test mode enabled"
    assert resolved_config.fcm_enabled == True, "FCM should be enabled"


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

    title, body, action_url = template.render(context)

    assert title == "Order Ready, John Doe!", f"Expected 'Order Ready, John Doe!', got '{title}'"
    assert "John Doe" in body, f"Body should contain customer name, got '{body}'"
    assert "12345" in body, f"Body should contain order ID, got '{body}'"
    assert action_url == "myapp://orders/12345", f"Expected 'myapp://orders/12345', got '{action_url}'"

    opts.test_template_id = template.id


@th.django_unit_test()
def test_push_service_test_mode(opts):
    """Test PushNotificationService behavior with test mode enabled."""
    from mojo.apps.account.models import User
    from mojo.apps.account.services.push import PushNotificationService

    user = User.objects.get(username=TEST_USER)
    service = PushNotificationService(user)

    # Should have config with test mode enabled
    assert service.config is not None, "Service should have push config"
    assert service.config.test_mode == True, "Config should have test mode enabled"

    results = service.send_notification(
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
        "test_mode": True,  # Enable test mode
        "fcm_enabled": True,  # FCM is primary
        "apns_enabled": False,  # APNS is secondary
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
    assert data.fcm_enabled == True, "FCM should be enabled (primary)"
    assert data.apns_enabled == False, "APNS should be disabled (secondary)"

    # Verify that sensitive fields are not exposed
    assert 'mojo_secrets' not in data, "MojoSecrets should not be exposed in API response"
    # Verify new fields are present
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
    assert data.sent_count >= 1, "Should have sent notifications in test mode"
    assert data.failed_count == 0, "Should have no failures in test mode"
    assert isinstance(data.deliveries, list), "Should return delivery records"

    # Verify test mode delivery
    if len(data.deliveries) > 0:
        delivery = data.deliveries[0]
        assert delivery.status == 'sent', "Delivery should be sent in test mode"


@th.django_unit_test()
def test_templated_notification_send_api(opts):
    """Test sending templated notifications via API."""
    # Should still be logged in from previous test
    assert opts.client.is_authenticated, "Should still be authenticated"

    notification_data = {
        "template": "test_order_ready",
        "context": {
            "customer_name": "API Test User",
            "order_id": "API123",
            "location": "API Test Store"
        }
    }

    resp = opts.client.post("/api/account/devices/push/send", json=notification_data)

    if resp.status_code != 200:
        error_details = f"Expected status 200, got {resp.status_code}"
        if hasattr(resp, 'response') and resp.response and hasattr(resp.response, 'error'):
            error_details += f"\nError: {resp.response.error}"
        assert False, error_details

    data = resp.response.data
    assert data.success == True, "Templated notification send should be successful"

    # Check that deliveries were created with rendered content
    if len(data.deliveries) > 0:
        delivery = data.deliveries[0]
        assert "API Test User" in delivery.title, "Title should contain rendered customer name"


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
    titles = [d.title for d in deliveries]
    assert any("Test Direct Notification" in title for title in titles), "Should include direct notification"
    assert any("API Test User" in title for title in titles), "Should include templated notification"


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

    if resp.status_code != 403:
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
    from mojo.apps.account.services.push import PushNotificationService

    user = User.objects.get(username=TEST_USER)
    service = PushNotificationService(user)

    # Get a device and set specific preferences
    device = RegisteredDevice.objects.filter(user=user).first()
    device.push_preferences = {"orders": True, "marketing": False, "alerts": True}
    device.save()

    # Test that marketing notification should not be sent to this device
    results = service.send_notification(
        title="Marketing Test",
        body="This is a marketing message",
        category="marketing"
    )

    # With test mode enabled, notifications succeed but preference filtering still works
    # Device has marketing=False, so no deliveries should be created for marketing category
    assert len(results) == 0, "Should not send to devices with marketing disabled"

    # Test that orders notification should be sent (orders=True in preferences)
    results = service.send_notification(
        title="Order Test",
        body="Your order is ready",
        category="orders"
    )

    # Should have results since orders=True in preferences and test mode succeeds
    assert len(results) > 0, "Should send to devices with orders enabled"
    for result in results:
        assert result.status == 'sent', "Should succeed in test mode"


@th.django_unit_test()
def test_template_resolution_priority(opts):
    """Test that organization templates take priority over system templates."""
    from mojo.apps.account.models import User, Group, NotificationTemplate
    from mojo.apps.account.services.push import PushNotificationService

    user = User.objects.get(username=TEST_USER)
    test_org = Group.objects.get(id=opts.test_org_id)

    # Create system template
    system_template = NotificationTemplate.objects.create(
        group=None,
        name="test_priority_template",
        title_template="System: {message}",
        body_template="This is from the system template",
        category="test"
    )

    # Create org template with same name
    org_template = NotificationTemplate.objects.create(
        group=test_org,
        name="test_priority_template",
        title_template="Org: {message}",
        body_template="This is from the org template",
        category="test"
    )

    service = PushNotificationService(user)
    template = service._get_template("test_priority_template")

    assert template.id == org_template.id, "Should prefer org template over system template"
    assert "Org:" in template.title_template, "Should get org template content"


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
    """Test FCM server key encryption/decryption functionality."""
    from mojo.apps.account.models import PushConfig

    # Get our test config and verify secrets work
    test_config = PushConfig.objects.get(id=opts.test_config_id)

    # Verify the FCM key was set and can be retrieved
    fcm_key = test_config.get_fcm_server_key()
    assert fcm_key == "test_fcm_server_key_for_testing", f"Expected FCM key 'test_fcm_server_key_for_testing', got '{fcm_key}'"

    # Test updating the key
    test_config.set_fcm_server_key("updated_fcm_key_12345")
    test_config.save()

    # Verify the update worked
    updated_key = test_config.get_fcm_server_key()
    assert updated_key == "updated_fcm_key_12345", f"Expected updated key 'updated_fcm_key_12345', got '{updated_key}'"

    # Verify the encrypted field exists but is not the plain text
    assert test_config.mojo_secrets is not None, "mojo_secrets field should contain encrypted data"
    assert "updated_fcm_key_12345" not in test_config.mojo_secrets, "Plain text key should not be in encrypted field"


@th.django_unit_test()
def test_comprehensive_test_mode_verification(opts):
    """Test comprehensive test mode functionality without external dependencies."""
    from mojo.apps.account.models import User, RegisteredDevice, PushConfig
    from mojo.apps.account.services.push import PushNotificationService

    user = User.objects.get(username=TEST_USER)

    # Create a config with test mode enabled
    test_config = PushConfig.objects.create(
        name="comprehensive_test_config",
        test_mode=True,
        fcm_enabled=False,  # Disabled to ensure test mode takes priority
        apns_enabled=False
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
        service = PushNotificationService(temp_user)

        # Test that all platforms work in test mode
        results = service.send_notification(
            title="Test Mode Verification",
            body="Testing all platforms in test mode"
        )

        # Should have 3 successful deliveries (one per device)
        assert len(results) == 3, f"Expected 3 deliveries, got {len(results)}"

        # All should be marked as sent in test mode
        for result in results:
            assert result.status == 'sent', f"Expected status 'sent', got '{result.status}'"
            assert result.platform_data.get('test_mode') == True, "Should have test mode flag"
            assert 'fake_delivery' in result.platform_data, "Should have fake delivery indicator"
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
